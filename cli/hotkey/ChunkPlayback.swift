// ChunkPlayback.swift — playing `agent.chunk` in the daemon, and cutting it dead.
//
// The old path piped each sentence's WAV to `afplay`, one process per sentence,
// and barge meant kill(2) on a grandchild we had to first prove was ours. This
// plays the chunks on one AVAudioPlayerNode instead: chunk N+1 is scheduled
// where chunk N ends, so a sentence sounds like one utterance, and a barge is
// `stop()` on the node — no process to find, no uid to verify.
//
// The generation counter is copied, deliberately, from
// web/src/audio/ChunkPlayer.ts: every stop() bumps it, every enqueue captures
// the generation it was accepted under, and a chunk whose decode finishes after
// a barge is DISCARDED instead of speaking over the user. A boolean cannot do
// this job — it flips back before the in-flight decode ever reads it, which is
// exactly the bug the web player already paid for.

import AVFoundation
import Foundation

// MARK: - WAV

/// Each `agent.chunk` is a whole RIFF/WAVE file (server/speech.py writes one per
/// synthesis clause) and carries no mime or sample-rate field — so the header is
/// the format contract and has to be read, not assumed.
public struct WavPCM {
    public let sampleRate: Double
    public let channels: Int
    public let bitsPerSample: Int
    /// WAVE_FORMAT_PCM (1) or IEEE_FLOAT (3).
    public let formatTag: Int
    public let samples: Data

    public var frameCount: Int {
        let bytesPerFrame = max(1, (bitsPerSample / 8) * channels)
        return samples.count / bytesPerFrame
    }
    public var durationSeconds: Double {
        sampleRate > 0 ? Double(frameCount) / sampleRate : 0
    }

    public enum ParseError: Error, CustomStringConvertible {
        case notRIFF
        case noFmtChunk
        case noDataChunk
        case unsupported(String)

        public var reason: String {
            switch self {
            case .notRIFF: return "chunk_not_riff"
            case .noFmtChunk: return "chunk_no_fmt"
            case .noDataChunk: return "chunk_no_data"
            case .unsupported: return "chunk_unsupported_format"
            }
        }
        public var description: String {
            switch self {
            case .unsupported(let d): return "chunk_unsupported_format(\(d))"
            default: return reason
            }
        }
    }

    private static func u16(_ d: Data, _ at: Int) -> Int {
        Int(d[at]) | (Int(d[at + 1]) << 8)
    }
    private static func u32(_ d: Data, _ at: Int) -> Int {
        Int(d[at]) | (Int(d[at + 1]) << 8) | (Int(d[at + 2]) << 16) | (Int(d[at + 3]) << 24)
    }

    public static func parse(_ raw: Data) throws -> WavPCM {
        let d = [UInt8](raw).withUnsafeBufferPointer { Data($0) }
        guard d.count >= 44,
              d[0] == 0x52, d[1] == 0x49, d[2] == 0x46, d[3] == 0x46, // RIFF
              d[8] == 0x57, d[9] == 0x41, d[10] == 0x56, d[11] == 0x45 // WAVE
        else { throw ParseError.notRIFF }

        var offset = 12
        var fmt: (tag: Int, channels: Int, rate: Double, bits: Int)?
        var samples: Data?
        while offset + 8 <= d.count {
            let id = String(bytes: d[offset..<(offset + 4)], encoding: .ascii) ?? ""
            let size = u32(d, offset + 4)
            let body = offset + 8
            guard size >= 0, body <= d.count else { break }
            let end = min(d.count, body + size)
            if id == "fmt " && (end - body) >= 16 {
                fmt = (
                    tag: u16(d, body),
                    channels: u16(d, body + 2),
                    rate: Double(u32(d, body + 4)),
                    bits: u16(d, body + 14)
                )
            } else if id == "data" {
                samples = d.subdata(in: body..<end)
            }
            offset = body + size + (size % 2)
        }
        guard let f = fmt else { throw ParseError.noFmtChunk }
        guard let s = samples, !s.isEmpty else { throw ParseError.noDataChunk }
        guard f.tag == 1 || f.tag == 3, f.channels >= 1, f.rate > 0,
              f.bits == 16 || f.bits == 32 else {
            throw ParseError.unsupported("tag=\(f.tag) ch=\(f.channels) bits=\(f.bits) rate=\(f.rate)")
        }
        return WavPCM(
            sampleRate: f.rate,
            channels: f.channels,
            bitsPerSample: f.bits,
            formatTag: f.tag,
            samples: s
        )
    }

    /// Deinterleaved float32, the format an AVAudioPlayerNode is happy to take.
    public func floatBuffer(at format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let frames = frameCount
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(
                  pcmFormat: format,
                  frameCapacity: AVAudioFrameCount(frames)
              ),
              let channelData = buffer.floatChannelData
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(frames)
        let outChannels = Int(format.channelCount)
        samples.withUnsafeBytes { raw in
            for frame in 0..<frames {
                for out in 0..<outChannels {
                    let src = min(out, channels - 1)
                    var value: Float = 0
                    if bitsPerSample == 16 {
                        let idx = frame * channels + src
                        let s = raw.load(fromByteOffset: idx * 2, as: Int16.self)
                        value = Float(s) / 32768.0
                    } else {
                        let idx = frame * channels + src
                        value = raw.load(fromByteOffset: idx * 4, as: Float32.self)
                    }
                    channelData[out][frame] = value
                }
            }
        }
        return buffer
    }
}

// MARK: - Sinks

public protocol AudioChunkSink: AnyObject {
    /// Schedules one chunk at the end of what is already scheduled.
    /// Returns the chunk's duration in seconds. Throws a named reason.
    func schedule(_ wav: WavPCM, seq: Int, chunkNo: Int) throws -> Double
    /// Cuts everything immediately. Returns how long the cut took, in ms.
    @discardableResult func stopAll() -> Double
    var isPlaying: Bool { get }
}

public enum SinkError: Error, CustomStringConvertible {
    case engineStartFailed(String)
    case bufferBuildFailed

    public var reason: String {
        switch self {
        case .engineStartFailed: return "playback_engine_start_failed"
        case .bufferBuildFailed: return "playback_buffer_build_failed"
        }
    }
    public var description: String { reason }
}

/// The real speaker.
public final class EnginePlaybackSink: AudioChunkSink {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var format: AVAudioFormat?
    private let lock = NSLock()
    private var scheduled = 0
    private var started = false

    public init() {}

    public var isPlaying: Bool {
        lock.lock(); defer { lock.unlock() }
        return scheduled > 0
    }

    public func schedule(_ wav: WavPCM, seq: Int, chunkNo: Int) throws -> Double {
        // The first chunk of a turn establishes the playback format; later
        // chunks are converted into it rather than reconnecting the graph
        // mid-sentence (which would be an audible gap).
        if format == nil {
            let f = AVAudioFormat(
                standardFormatWithSampleRate: wav.sampleRate,
                channels: AVAudioChannelCount(min(2, max(1, wav.channels)))
            )
            format = f
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: f)
        }
        guard let format = format else { throw SinkError.bufferBuildFailed }
        guard var buffer = wav.floatBuffer(at: format) else { throw SinkError.bufferBuildFailed }
        if wav.sampleRate != format.sampleRate {
            buffer = try resample(buffer, to: format, from: wav)
        }
        if !started {
            engine.prepare()
            do { try engine.start() } catch {
                throw SinkError.engineStartFailed(error.localizedDescription)
            }
            started = true
        }
        lock.lock(); scheduled += 1; lock.unlock()
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self = self else { return }
            self.lock.lock(); self.scheduled = max(0, self.scheduled - 1); self.lock.unlock()
        }
        if !player.isPlaying { player.play() }
        return Double(buffer.frameLength) / format.sampleRate
    }

    private func resample(_ buffer: AVAudioPCMBuffer, to format: AVAudioFormat, from wav: WavPCM) throws -> AVAudioPCMBuffer {
        let sourceFormat = AVAudioFormat(
            standardFormatWithSampleRate: wav.sampleRate,
            channels: AVAudioChannelCount(min(2, max(1, wav.channels)))
        )
        guard let sourceFormat = sourceFormat,
              let converter = AVAudioConverter(from: sourceFormat, to: format),
              let out = AVAudioPCMBuffer(
                  pcmFormat: format,
                  frameCapacity: AVAudioFrameCount(Double(buffer.frameLength) * format.sampleRate / wav.sampleRate + 1024)
              )
        else { throw SinkError.bufferBuildFailed }
        var supplied = false
        var err: NSError?
        _ = converter.convert(to: out, error: &err) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        if err != nil || out.frameLength == 0 { throw SinkError.bufferBuildFailed }
        return out
    }

    @discardableResult
    public func stopAll() -> Double {
        let t0 = DispatchTime.now()
        player.stop()
        lock.lock(); scheduled = 0; lock.unlock()
        return Double(DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000.0
    }
}

/// Night mode and the headless self-test. The chunk is still base64-decoded and
/// its WAV header still parsed — a malformed chunk fails here exactly as it
/// would on the speaker — but nothing reaches an output device. It keeps the
/// timeline so "is it still speaking" and the barge cut stay measurable.
public final class SilentPlaybackSink: AudioChunkSink {
    private let lock = NSLock()
    private var endsAt: DispatchTime = .now()
    public private(set) var scheduledSeconds: Double = 0

    public init() {}

    public var isPlaying: Bool {
        lock.lock(); defer { lock.unlock() }
        return DispatchTime.now() < endsAt
    }

    public func schedule(_ wav: WavPCM, seq: Int, chunkNo: Int) throws -> Double {
        let duration = wav.durationSeconds
        lock.lock()
        let base = max(DispatchTime.now(), endsAt)
        endsAt = base + .milliseconds(Int(duration * 1000))
        scheduledSeconds += duration
        lock.unlock()
        return duration
    }

    @discardableResult
    public func stopAll() -> Double {
        let t0 = DispatchTime.now()
        lock.lock(); endsAt = .now(); lock.unlock()
        return Double(DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000.0
    }
}

// MARK: - ChunkPlayback

public final class ChunkPlayback {
    /// web/src/audio/ChunkPlayer.ts's MAX_CHUNK_BYTES, and the server's own cap
    /// (PET_TALK_CHUNK_MAX_BYTES, SPEC §4.2.1). The client half of it.
    public static let maxChunkBytes = 512 * 1024

    private let sink: AudioChunkSink
    private let lock = NSLock()
    private var generation = 0
    /// Set by `stop()` and `finishTurn()`: this turn's audio is over, so every
    /// later chunk for it is discarded. The generation counter alone cannot do
    /// this — `enqueue` here is synchronous, so a chunk that arrives AFTER the
    /// barge captures the NEW generation and would match it. The web player
    /// (web/src/audio/ChunkPlayer.ts) only needs the counter because its own
    /// gap is an in-flight `await decodeAudioData()`; ours is the socket, and
    /// the socket keeps delivering the killed turn's chunks for a few ms.
    private var barred = false

    public private(set) var received = 0
    public private(set) var played = 0
    public private(set) var discarded = 0
    /// Chunks that were accepted for playback AFTER a barge. Must stay 0 — a
    /// non-zero value is the barge contract broken, and the self-test fails on it.
    public private(set) var playedAfterBarge = 0
    private var bargeCount = 0

    /// Named reasons, for the log and the capsule.
    public var onError: ((String, String) -> Void)?
    /// One beat per word start, driven by `agent.sentence`'s `word_times`.
    public var onBeat: (() -> Void)?

    public init(sink: AudioChunkSink) {
        self.sink = sink
    }

    public var isPlaying: Bool { sink.isPlaying }

    /// Accepts one `agent.chunk`. Returns true when the chunk was scheduled.
    @discardableResult
    public func enqueue(seq: Int, chunkNo: Int, audioB64: String, final: Bool) -> Bool {
        lock.lock()
        let gen = generation
        received += 1
        let barged = bargeCount
        if barred {
            // The turn this chunk belongs to has been cut. Stale audio is
            // silence, not a late sentence.
            discarded += 1
            lock.unlock()
            return false
        }
        lock.unlock()

        // The terminator chunk carries no audio (server/speech.py sends
        // audio_b64:"" with final:true when the backend drained without one).
        if audioB64.isEmpty {
            return final
        }
        guard let raw = Data(base64Encoded: audioB64) else {
            onError?("chunk_bad_base64", "seq=\(seq) chunk=\(chunkNo)")
            return false
        }
        guard raw.count <= ChunkPlayback.maxChunkBytes else {
            onError?("chunk_over_cap", "seq=\(seq) chunk=\(chunkNo) bytes=\(raw.count)")
            return false
        }
        let wav: WavPCM
        do {
            wav = try WavPCM.parse(raw)
        } catch let e as WavPCM.ParseError {
            onError?(e.reason, "seq=\(seq) chunk=\(chunkNo)")
            return false
        } catch {
            onError?("chunk_unparseable", "seq=\(seq) chunk=\(chunkNo)")
            return false
        }

        // The generation gate. Anything decoded after a barge is stale, and
        // stale audio is silence, not a late sentence.
        lock.lock()
        if gen != generation {
            discarded += 1
            lock.unlock()
            return false
        }
        lock.unlock()

        do {
            _ = try sink.schedule(wav, seq: seq, chunkNo: chunkNo)
        } catch let e as SinkError {
            onError?(e.reason, "seq=\(seq) chunk=\(chunkNo)")
            return false
        } catch {
            onError?("playback_schedule_failed", "seq=\(seq) chunk=\(chunkNo)")
            return false
        }
        lock.lock()
        played += 1
        if barged > 0 { playedAfterBarge += 1 }
        lock.unlock()
        return true
    }

    /// Barge: bump the generation, cut the node, forget the queue. Returns the
    /// measured cut in milliseconds — the number the SPEC budgets.
    @discardableResult
    public func stop() -> Double {
        lock.lock()
        generation += 1
        bargeCount += 1
        barred = true
        lock.unlock()
        return sink.stopAll()
    }

    /// End of turn without a barge: the queue drains on its own, but the
    /// generation still moves so a straggler cannot speak into the next turn.
    public func finishTurn() {
        lock.lock(); generation += 1; barred = true; lock.unlock()
    }

    /// `agent.sentence.word_times` -> one glyph beat per word, on the main queue,
    /// each one dropped if a barge has since bumped the generation. The capsule's
    /// own beat() is a no-op unless it is in the speaking state, so this cannot
    /// animate a capsule that is not talking.
    public func scheduleBeats(wordTimes: [[String: Any]]) {
        lock.lock(); let gen = generation; lock.unlock()
        for entry in wordTimes {
            let startMs = (entry["start_ms"] as? Int) ?? Int((entry["start_ms"] as? Double) ?? 0)
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(startMs)) { [weak self] in
                guard let self = self else { return }
                self.lock.lock(); let current = self.generation; self.lock.unlock()
                guard current == gen else { return }
                self.onBeat?()
            }
        }
    }
}
