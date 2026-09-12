// TurnController.swift — one voice turn, in this process.
//
// The state machine docs/SPEC.md §5 describes, wired to AudioCapture (the mic),
// WSClient (the socket) and ChunkPlayback (the speaker). Option+Tab starts a
// turn; Escape or a second Option+Tab barges; hand-over sends user.handover and
// then listens. Nothing spawns.
//
// Fail closed with a named reason, always: every exit that is not a completed
// turn names why — health_probe_failed, mic_permission_denied, ws_unauthorized,
// tts_unavailable — and puts that name in the log and a plain-English line on
// the capsule. A turn that quietly does nothing is the failure mode this file
// exists to remove.

import AVFoundation
import Foundation

public enum TurnEngine: String {
    case native
    case cli

    /// `turn_engine` in config.yaml, `PET_TALK_TURN_ENGINE` in the environment,
    /// `--turn-engine` on the command line. Native is the default; cli is the
    /// unchanged fallback path, kept working, not kept as a silent catch.
    public static func resolve(config: String?, argument: String?) -> TurnEngine {
        if let a = argument?.lowercased(), let e = TurnEngine(rawValue: a) { return e }
        let env = (ProcessInfo.processInfo.environment["PET_TALK_TURN_ENGINE"] ?? "").lowercased()
        if let e = TurnEngine(rawValue: env) { return e }
        if let c = config?.lowercased(), let e = TurnEngine(rawValue: c) { return e }
        return .native
    }
}

public enum TurnState: String {
    case idle, probing, listening, thinking, speaking, error
}

/// What the self-test prints and the tests assert on. Every number is measured
/// in this process, on this run — nothing here is a remembered constant.
public struct TurnMetrics {
    public var pressToUserStartMs: Double = -1
    public var pressToFirstChunkMs: Double = -1
    public var userChunksSent = 0
    public var userStopBytes = 0
    /// Trailing silence the VAD actually accumulated before ending the turn.
    public var vadTrailingSilenceMs: Double = -1
    /// Press to `user.stop`.
    public var pressToUserStopMs: Double = -1
    public var bargeToSilenceMs: Double = -1
    public var chunksReceived = 0
    public var chunksPlayed = 0
    public var chunksDiscarded = 0
    public var playedAfterBarge = 0
    public var beats = 0
    public var states: [String] = []
    public var framesSent: [String] = []
    public var framesReceived: [String] = []
    public var tokenSource = StudioToken.Source.none.rawValue
    public var reason = ""
    public var ok = false

    public var dictionary: [String: Any] {
        [
            "pressToUserStartMs": pressToUserStartMs,
            "pressToFirstChunkMs": pressToFirstChunkMs,
            "userChunksSent": userChunksSent,
            "userStopBytes": userStopBytes,
            "vadTrailingSilenceMs": vadTrailingSilenceMs,
            "pressToUserStopMs": pressToUserStopMs,
            "bargeToSilenceMs": bargeToSilenceMs,
            "chunksReceived": chunksReceived,
            "chunksPlayed": chunksPlayed,
            "chunksDiscarded": chunksDiscarded,
            "playedAfterBarge": playedAfterBarge,
            "beats": beats,
            "states": states,
            "framesSent": framesSent,
            "framesReceived": framesReceived,
            "tokenSource": tokenSource,
            "reason": reason,
            "ok": ok
        ]
    }
}

public final class TurnController {
    // MARK: Wiring

    public typealias SourceFactory = () -> PCMFrameSource
    public typealias SinkFactory = () -> AudioChunkSink

    /// The daemon's controller: real mic, real speaker (silent under night mode
    /// or headless), the studio URL from PET_TALK_WS_URL or the SPEC default.
    public static let shared = TurnController()

    private let makeSource: SourceFactory
    private let makeSink: SinkFactory
    private let urlString: String
    /// Skip the permission prompt when the source is not a microphone.
    private let usesMicrophone: Bool

    private let lock = NSLock()
    private var stateValue: TurnState = .idle
    private var turnId = ""
    private var client: WSClient?
    private var source: PCMFrameSource?
    private var playback: ChunkPlayback?
    private var vad = EnergyVAD()
    private var captured = Data()
    private var pressedAt = DispatchTime.now()
    private var speakingSince: DispatchTime?
    private var safetyTimer: DispatchSourceTimer?
    private var metricsValue = TurnMetrics()
    private var handoverPending = false
    private var finished = false

    // MARK: Callbacks (main.swift wires these; the HUD is not this file's job)

    /// A line for the daemon log.
    public var onLog: ((String) -> Void)?
    /// Final transcript text, for the capsule and the paste injector.
    public var onTranscript: ((String) -> Void)?
    /// `(reason, capsuleText)` — the named reason for the log, the plain line
    /// for the capsule's error state.
    public var onError: ((String, String) -> Void)?
    /// The turn is over, for any reason. `nil` reason means it completed.
    public var onFinish: ((String?) -> Void)?

    public var state: TurnState {
        lock.lock(); defer { lock.unlock() }
        return stateValue
    }
    public var isActive: Bool {
        let s = state
        return s != .idle && s != .error
    }
    public var metrics: TurnMetrics {
        lock.lock(); defer { lock.unlock() }
        return metricsValue
    }

    public init(
        urlString: String = ProcessInfo.processInfo.environment["PET_TALK_WS_URL"] ?? WSClient.defaultURL,
        makeSource: SourceFactory? = nil,
        makeSink: SinkFactory? = nil,
        usesMicrophone: Bool = true
    ) {
        self.urlString = urlString
        self.usesMicrophone = usesMicrophone
        self.makeSource = makeSource ?? { AudioCapture() }
        self.makeSink = makeSink ?? {
            // Night mode and headless never reach an output device (AGENTS.md
            // law 7): the chunks are still decoded and parsed, just not played.
            if EarconEngine.isSilentModeEnv || HUDController.isHeadless {
                return SilentPlaybackSink()
            }
            return EnginePlaybackSink()
        }
    }

    // MARK: - Start

    /// Option+Tab (or Option+Shift+Tab with `handover: true`). `pressedAt` is
    /// the hotkey timestamp, so press-to-first-chunk is measured from the key,
    /// not from whenever this function happened to run.
    public func startTurn(handover: Bool = false, pressedAt: DispatchTime = DispatchTime.now()) {
        lock.lock()
        guard stateValue == .idle || stateValue == .error else {
            lock.unlock()
            onLog?("| [turn] ignored — a turn is already \(stateValue.rawValue)")
            return
        }
        stateValue = .probing
        self.pressedAt = pressedAt
        self.handoverPending = handover
        self.finished = false
        self.captured = Data()
        self.vad = EnergyVAD()
        self.turnId = TurnController.newTurnId()
        var m = TurnMetrics()
        m.states = ["probing"]
        self.metricsValue = m
        lock.unlock()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.probeThenOpen()
        }
    }

    public static func newTurnId() -> String {
        "native-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(10))"
    }

    private func probeThenOpen() {
        let client: WSClient
        do {
            client = try WSClient(urlString: urlString)
        } catch let e as WSClientError {
            fail(e.reason, e.capsuleText)
            return
        } catch {
            fail("ws_bad_url", "studio address is wrong")
            return
        }
        lock.lock()
        self.client = client
        metricsValue.tokenSource = client.tokenSource.rawValue
        lock.unlock()

        // A third of a second to learn the studio is not there — before the
        // microphone is ever opened (SPEC §5: no turn without a server).
        if case .failure(let e) = client.probeHealth() {
            fail(e.reason, e.capsuleText)
            return
        }

        client.onLog = { [weak self] line in self?.onLog?(line) }
        client.onFailure = { [weak self] e in self?.fail(e.reason, e.capsuleText) }
        client.onFrame = { [weak self] frame in self?.handle(frame) }
        client.onOpen = { [weak self] in self?.socketOpened() }

        let sink = makeSink()
        let pb = ChunkPlayback(sink: sink)
        pb.onError = { [weak self] reason, detail in
            self?.onLog?("! [playback] \(reason) \(detail)")
        }
        pb.onBeat = { [weak self] in
            guard let self = self else { return }
            HUDController.shared.panel.capsuleView.archieGlyph.beat()
            self.lock.lock(); self.metricsValue.beats += 1; self.lock.unlock()
        }
        lock.lock(); self.playback = pb; lock.unlock()

        client.connect()
    }

    private func socketOpened() {
        if handoverPending {
            // Hand-over: the frame first, then the mic — the server needs to know
            // the next turn is delegated work before it hears any of it.
            send("user.handover", ["source": "hotkey"])
            handoverPending = false
        }
        send("user.start", ["persona": ProcessInfo.processInfo.environment["PET_TALK_PERSONA"] ?? "donna"])
        lock.lock()
        metricsValue.pressToUserStartMs = msSince(pressedAt)
        lock.unlock()

        guard usesMicrophone else {
            beginCapture()
            return
        }
        MicPermission.request { [weak self] granted in
            guard let self = self else { return }
            guard granted else {
                self.fail("mic_permission_denied", MicPermission.deniedReasonText)
                return
            }
            self.beginCapture()
        }
    }

    private func beginCapture() {
        let source = makeSource()
        lock.lock()
        self.source = source
        stateValue = .listening
        metricsValue.states.append("listening")
        lock.unlock()
        hud { HUDController.shared.show(state: .listening) }

        do {
            try source.start { [weak self] frame in self?.consume(frame) }
        } catch let e as AudioCaptureError {
            fail(e.reason, "microphone unavailable")
            return
        } catch {
            fail("mic_start_failed", "microphone unavailable")
            return
        }

        // The 20 s safety cap. The VAD normally ends the turn long before this;
        // this is the promise that the mic closes even when it does not.
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global())
        timer.schedule(deadline: .now() + EnergyVAD.maxTurnSeconds)
        timer.setEventHandler { [weak self] in
            guard let self = self, self.state == .listening else { return }
            self.onLog?("| [vad] 20s safety cap reached — closing the mic")
            self.endCapture(sendStop: true)
        }
        safetyTimer = timer
        timer.resume()
    }

    // MARK: - Capture

    private func consume(_ frame: PCMFrame) {
        guard state == .listening else { return }

        lock.lock()
        captured.append(frame.pcm)
        metricsValue.userChunksSent += 1
        let isFirst = metricsValue.userChunksSent == 1
        if isFirst { metricsValue.pressToFirstChunkMs = msSince(pressedAt) }
        lock.unlock()

        send("user.chunk", [
            "chunk": frame.pcm.base64EncodedString(),
            "sample_rate": Int(EnergyVAD.sampleRate)
        ])

        // The capsule's existing level path, fed straight from the tap — no pipe,
        // no regex, no "[RMS: 0.24]" line in between.
        let (nRms, nPeak) = PCMEnergy.normalise(rms: frame.rms, peak: frame.peak)
        hud { HUDController.shared.updateAudioLevel(rms: nRms, peak: nPeak) }

        let result = vad.process(rms: frame.rms)
        if result.turnFinished {
            let spoke = vad.speechStarted
            lock.lock(); metricsValue.vadTrailingSilenceMs = vad.silenceDurationMs; lock.unlock()
            endCapture(sendStop: spoke)
        }
    }

    /// Closes the mic. `sendStop: false` is the ambient-silence case: the CLI
    /// sends a `barge` and goes home rather than making the server transcribe
    /// four seconds of room tone, and so does this.
    private func endCapture(sendStop: Bool) {
        safetyTimer?.cancel(); safetyTimer = nil
        lock.lock()
        let src = source
        source = nil
        let pcm = captured
        guard stateValue == .listening else { lock.unlock(); return }
        stateValue = sendStop ? .thinking : .idle
        metricsValue.states.append(sendStop ? "thinking" : "idle")
        lock.unlock()
        src?.stop()

        guard sendStop else {
            onLog?("| [vad] no speech detected (ambient silence) — turn abandoned")
            send("barge", [:])
            finish(reason: nil)
            return
        }

        lock.lock()
        metricsValue.userStopBytes = pcm.count
        metricsValue.pressToUserStopMs = msSince(pressedAt)
        lock.unlock()
        // The client's merged buffer wins server-side (SPEC §4.1), so this is
        // both the end-of-turn signal and the authoritative audio.
        send("user.stop", [
            "pcm_b64": pcm.base64EncodedString(),
            "sample_rate": Int(EnergyVAD.sampleRate)
        ])
        hud {
            EarconEngine.shared.playSilenceCutoff()
            HUDController.shared.update(state: .thinking)
        }
    }

    // MARK: - Barge

    /// Escape, or a second Option+Tab. Sends the `barge` frame, cuts playback
    /// and flushes — in that order, because the server's cancellation is slower
    /// than the speaker's and the listener hears the speaker.
    @discardableResult
    public func barge() -> Double {
        guard isActive || (playback?.isPlaying ?? false) else { return -1 }
        let t0 = DispatchTime.now()
        send("barge", [:])
        let cutMs = playback?.stop() ?? 0
        lock.lock()
        let src = source
        source = nil
        stateValue = .idle
        metricsValue.states.append("barged")
        metricsValue.bargeToSilenceMs = msSince(t0)
        lock.unlock()
        src?.stop()
        safetyTimer?.cancel(); safetyTimer = nil
        onLog?("| [barge] playback cut in \(String(format: "%.2f", cutMs))ms")
        // The socket stays open for the server's own half of the barge: the
        // state.listening ack carrying the new turn_id, the agent.done for the
        // killed turn, and whatever chunks were already in flight — which the
        // playback discards by name rather than by luck. agent.done closes it;
        // this timer is the promise that it closes even if that never arrives.
        let grace = DispatchSource.makeTimerSource(queue: DispatchQueue.global())
        grace.schedule(deadline: .now() + 1.5)
        grace.setEventHandler { [weak self] in self?.finish(reason: nil) }
        safetyTimer = grace
        grace.resume()
        return cutMs
    }

    // MARK: - Server frames

    private func handle(_ frame: [String: Any]) {
        guard let type = frame["type"] as? String else {
            onLog?("! [ws] frame with no type (bad_frame)")
            return
        }
        lock.lock(); metricsValue.framesReceived.append(type); lock.unlock()

        switch type {
        case "state.idle":
            break
        case "state.listening":
            // A barge ack carries the new turn_id the client must adopt
            // (server/ws.py mints it); a plain ack carries no barged_turn.
            if let barged = frame["barged_turn"] as? String, !barged.isEmpty,
               let newId = frame["turn_id"] as? String {
                lock.lock(); turnId = newId; lock.unlock()
                onLog?("| [barge] server dropped \(frame["dropped"] as? Int ?? 0) sentence(s) of \(barged)")
            }
        case "state.thinking":
            hud { HUDController.shared.update(state: .thinking) }
        case "state.speaking":
            lock.lock()
            if stateValue != .speaking {
                stateValue = .speaking
                metricsValue.states.append("speaking")
            }
            speakingSince = DispatchTime.now()
            lock.unlock()
            hud { HUDController.shared.update(state: .speaking) }
        case "transcript.user":
            let text = (frame["text"] as? String) ?? ""
            let isPartial = (frame["partial"] as? Bool) ?? false
            if !text.isEmpty && !isPartial { onTranscript?(text) }
        case "agent.stall":
            if let text = frame["text"] as? String, !text.isEmpty {
                hud { HUDController.shared.showBreadcrumb(badge: "Thinking", detail: text, state: .thinking) }
            }
        case "agent.chunk":
            let seq = (frame["seq"] as? Int) ?? 0
            let chunkNo = (frame["chunk_no"] as? Int) ?? 0
            let b64 = (frame["audio_b64"] as? String) ?? ""
            let final = (frame["final"] as? Bool) ?? false
            playback?.enqueue(seq: seq, chunkNo: chunkNo, audioB64: b64, final: final)
            syncPlaybackCounters()
        case "agent.sentence":
            if let times = frame["word_times"] as? [[String: Any]], !times.isEmpty {
                playback?.scheduleBeats(wordTimes: times)
            } else {
                // No timings: one beat for the sentence, never a fake rhythm.
                hud { HUDController.shared.panel.capsuleView.archieGlyph.beat() }
            }
        case "agent.error":
            let reason = (frame["reason"] as? String) ?? "agent_error"
            fail(reason, TurnController.capsuleText(for: reason))
        case "agent.done":
            let path = (frame["path"] as? String) ?? ""
            onLog?("| [turn] done path=\(path) sentences=\(frame["sentences"] as? Int ?? 0)")
            drainThenFinish()
        default:
            onLog?("| [ws] ignored frame \(type)")
        }
    }

    private func syncPlaybackCounters() {
        guard let pb = playback else { return }
        lock.lock()
        metricsValue.chunksReceived = pb.received
        metricsValue.chunksPlayed = pb.played
        metricsValue.chunksDiscarded = pb.discarded
        metricsValue.playedAfterBarge = pb.playedAfterBarge
        lock.unlock()
    }

    /// Plain-English capsule lines for the server's named reasons. A reason with
    /// no line here is shown as "something went wrong" and still logged by name
    /// — the user surface never carries the engineering word (product rule).
    public static func capsuleText(for reason: String) -> String {
        switch reason {
        case "stt_empty_audio": return "didn't catch that"
        case "stt_unavailable", "stt_failed": return "can't hear right now"
        case "tts_unavailable", "tts_failed": return "can't speak right now"
        case "llm_unavailable", "llm_failed": return "can't think right now"
        case "empty_text": return "didn't catch that"
        case "unknown_frame", "bad_frame": return "studio spoke out of turn"
        default: return "something went wrong"
        }
    }

    // MARK: - Finish

    private func drainThenFinish() {
        let deadline = Date().addingTimeInterval(30)
        DispatchQueue.global().async { [weak self] in
            while let self = self, self.playback?.isPlaying == true, Date() < deadline {
                usleep(10_000)
            }
            self?.finish(reason: nil)
        }
    }

    private func fail(_ reason: String, _ capsuleText: String) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        stateValue = .error
        metricsValue.states.append("error")
        metricsValue.reason = reason
        lock.unlock()
        onLog?("! [error] \(reason)")
        onError?(reason, capsuleText)
        hud {
            EarconEngine.shared.playError()
            HUDController.shared.showBreadcrumb(badge: "Error", detail: capsuleText, state: .thinking)
            HUDController.shared.triggerErrorShake()
        }
        finish(reason: reason)
    }

    private func finish(reason: String?) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        finished = true
        let src = source
        source = nil
        let c = client
        client = nil
        syncPlaybackCountersLocked()
        metricsValue.ok = (reason == nil)
        if stateValue != .error { stateValue = .idle }
        lock.unlock()
        src?.stop()
        playback?.finishTurn()
        c?.close()
        hud { HUDController.shared.dismiss() }
        onFinish?(reason)
    }

    /// Must hold `lock`.
    private func syncPlaybackCountersLocked() {
        guard let pb = playback else { return }
        metricsValue.chunksReceived = pb.received
        metricsValue.chunksPlayed = pb.played
        metricsValue.chunksDiscarded = pb.discarded
        metricsValue.playedAfterBarge = pb.playedAfterBarge
    }

    // MARK: - Helpers

    private func send(_ type: String, _ fields: [String: Any]) {
        lock.lock()
        let tid = turnId
        metricsValue.framesSent.append(type)
        let c = client
        lock.unlock()
        var frame: [String: Any] = ["type": type, "turn_id": tid]
        frame.merge(fields) { _, new in new }
        c?.send(frame) { [weak self] error in
            if let error = error {
                self?.onLog?("! [ws] \(type) not sent: \(error.reason)")
            }
        }
    }

    /// Everything AppKit gets hopped to the main thread. The turn machine runs
    /// on the socket's queue and the audio tap's thread, and HUDPanel is an
    /// NSWindow — touching it from either one is a hard crash, not a warning.
    private func hud(_ block: @escaping () -> Void) {
        if Thread.isMainThread { block() } else { DispatchQueue.main.async(execute: block) }
    }

    private func msSince(_ t: DispatchTime) -> Double {
        Double(DispatchTime.now().uptimeNanoseconds - t.uptimeNanoseconds) / 1_000_000.0
    }
}
