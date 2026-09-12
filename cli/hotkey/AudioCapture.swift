// AudioCapture.swift — the daemon's own microphone, 20 ms at a time.
//
// Before this file a voice turn spawned `pet-talk-cli`, which spawned `sox`,
// and the capsule learned the mic level by regex-parsing "[RMS: 0.24]" lines
// out of a pipe. Three processes and a text protocol to move a number that was
// already in this process's address space.
//
// Now: one AVAudioEngine input tap, converted to the wire format the server
// wants (16 kHz mono linear16 — TECH-SPEC), emitted as 20 ms frames with the
// RMS and peak already computed. The HUD's existing level path
// (HUDController.updateAudioLevel) is fed directly.
//
// The VAD constants are deliberately the SAME numbers as cli/audio.py's
// EnergyVAD (threshold 350, 800 ms trailing silence, 2 confirm frames,
// 4000 ms initial-silence cutoff). Two engines that end a turn at different
// moments would be two products. Frame SIZE differs (20 ms here vs the CLI's
// 32 ms / 512-sample chunk) and the duration arithmetic follows the frame, so
// the millisecond thresholds hold either way.

import AVFoundation
import Foundation

// MARK: - Frame

/// One 20 ms frame of capture, with its energy already measured.
public struct PCMFrame {
    /// 16 kHz mono linear16, little-endian — the bytes that go into
    /// `user.chunk`'s base64 `chunk` field.
    public let pcm: Data
    /// Raw RMS, 0...32768 — the same scale cli/audio.py's calculate_rms uses,
    /// so the VAD threshold is comparable.
    public let rms: Float
    /// Raw peak amplitude, 0...32768.
    public let peak: Float

    public init(pcm: Data, rms: Float, peak: Float) {
        self.pcm = pcm
        self.rms = rms
        self.peak = peak
    }
}

/// Where frames come from. The real mic is one implementation; the headless
/// self-test uses `SyntheticAudioSource` so the whole turn machine is provable
/// on a machine with no microphone, and without ever opening one.
public protocol PCMFrameSource: AnyObject {
    var isRunning: Bool { get }
    /// Starts producing frames. `onFrame` is called off the main thread.
    /// Throws a named reason; never fails silently.
    func start(onFrame: @escaping (PCMFrame) -> Void) throws
    func stop()
}

// MARK: - Energy measurement

public enum PCMEnergy {
    /// RMS and peak of little-endian linear16 bytes, on cli/audio.py's scale.
    public static func measure(_ pcm: Data) -> (rms: Float, peak: Float) {
        let count = pcm.count / 2
        guard count > 0 else { return (0, 0) }
        var sumSq: Double = 0
        var maxPeak: Int32 = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for i in 0..<count {
                let s = Int32(samples[i])
                let a = abs(s)
                if a > maxPeak { maxPeak = a }
                sumSq += Double(s) * Double(s)
            }
        }
        return (Float((sumSq / Double(count)).squareRoot()), Float(maxPeak))
    }

    /// The HUD's level path wants 0...1. Same normalisation the CLI printed
    /// (`rms / 4000`, `peak / 16000`) so the capsule behaves identically on
    /// either engine — main.swift's parseRMSTelemetry consumed exactly these.
    public static func normalise(rms: Float, peak: Float) -> (rms: Float, peak: Float) {
        (min(1.0, max(0.0, rms / 4000.0)), min(1.0, max(0.0, peak / 16000.0)))
    }
}

// MARK: - VAD

/// Energy VAD, constant-for-constant with cli/audio.py's EnergyVAD.
public struct EnergyVAD {
    public static let defaultThreshold: Float = 350.0
    public static let defaultSilenceMs: Double = 800.0
    public static let defaultConfirmFrames: Int = 2
    public static let defaultInitialSilenceMs: Double = 4000.0
    public static let sampleRate: Double = 16000.0
    /// 20 ms at 16 kHz = 320 samples = 640 bytes.
    public static let frameSamples: Int = 320
    /// Never hold the mic longer than this, whatever the VAD thinks.
    public static let maxTurnSeconds: Double = 20.0

    public let threshold: Float
    public let confirmFrames: Int
    public let silenceMs: Double
    public let initialSilenceMs: Double
    public let frameDurationMs: Double

    public private(set) var speechStarted = false
    public private(set) var silenceDurationMs: Double = 0
    public private(set) var initialSilenceDurationMs: Double = 0
    public private(set) var speechDurationMs: Double = 0
    private var consecutiveSpeech = 0

    /// Env overrides match the CLI's flags (`PET_TALK_VAD_SILENCE_MS`,
    /// `PET_TALK_VAD_THRESHOLD`) so one setting moves both engines.
    public init(
        threshold: Float? = nil,
        silenceMs: Double? = nil,
        confirmFrames: Int = EnergyVAD.defaultConfirmFrames,
        initialSilenceMs: Double = EnergyVAD.defaultInitialSilenceMs,
        frameSamples: Int = EnergyVAD.frameSamples
    ) {
        let env = ProcessInfo.processInfo.environment
        let envThreshold = (env["PET_TALK_VAD_THRESHOLD"]).flatMap { Float($0) }
        let envSilence = (env["PET_TALK_VAD_SILENCE_MS"]).flatMap { Double($0) }
        self.threshold = threshold ?? envThreshold ?? EnergyVAD.defaultThreshold
        self.silenceMs = silenceMs ?? envSilence ?? EnergyVAD.defaultSilenceMs
        self.confirmFrames = confirmFrames
        self.initialSilenceMs = initialSilenceMs
        self.frameDurationMs = Double(frameSamples) / EnergyVAD.sampleRate * 1000.0
    }

    public mutating func reset() {
        speechStarted = false
        consecutiveSpeech = 0
        speechDurationMs = 0
        silenceDurationMs = 0
        initialSilenceDurationMs = 0
    }

    /// Returns `(isSpeech, turnFinished)` — cli/audio.py's process_chunk.
    public mutating func process(rms: Float) -> (isSpeech: Bool, turnFinished: Bool) {
        let isSpeech = rms >= threshold
        if isSpeech {
            consecutiveSpeech += 1
            speechDurationMs += frameDurationMs
            if !speechStarted && consecutiveSpeech >= confirmFrames { speechStarted = true }
            if speechStarted { silenceDurationMs = 0 }
            return (true, false)
        }
        consecutiveSpeech = 0
        if speechStarted {
            silenceDurationMs += frameDurationMs
            if silenceDurationMs >= silenceMs { return (false, true) }
        } else {
            initialSilenceDurationMs += frameDurationMs
            if initialSilenceDurationMs >= initialSilenceMs { return (false, true) }
        }
        return (false, false)
    }
}

// MARK: - Microphone permission

public enum MicPermission {
    public enum Status: String {
        case granted, denied, undetermined
    }

    /// The reason the capsule shows when macOS says no. Named, user-facing,
    /// and free of engineering vocabulary (product rule).
    public static let deniedReasonText = "microphone access needed"

    public static var status: Status {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .notDetermined: return .undetermined
        default: return .denied
        }
    }

    /// Requests access on first use. Already-granted returns immediately on the
    /// calling thread's next main-queue turn; a denial is a named failure, not a
    /// silent empty recording.
    public static func request(_ completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            DispatchQueue.main.async { completion(true) }
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { completion(granted) }
            }
        default:
            DispatchQueue.main.async { completion(false) }
        }
    }
}

// MARK: - The real microphone

public enum AudioCaptureError: Error, CustomStringConvertible {
    case noInputFormat
    case converterUnavailable(from: String, to: String)
    case engineStartFailed(String)

    public var description: String {
        switch self {
        case .noInputFormat: return "mic_no_input_format"
        case .converterUnavailable(let a, let b): return "mic_converter_unavailable(\(a)->\(b))"
        case .engineStartFailed(let d): return "mic_engine_start_failed(\(d))"
        }
    }
    /// The named reason a caller reports. Never a bare `error`.
    public var reason: String {
        switch self {
        case .noInputFormat: return "mic_no_input_format"
        case .converterUnavailable: return "mic_converter_unavailable"
        case .engineStartFailed: return "mic_engine_start_failed"
        }
    }
}

/// AVAudioEngine input tap -> 16 kHz mono linear16 -> 20 ms frames.
public final class AudioCapture: PCMFrameSource {
    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var carry = Data()
    private var running = false
    private let bytesPerFrame = EnergyVAD.frameSamples * 2

    public var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return running
    }

    /// The wire format: linear16, 16 kHz, mono, interleaved (TECH-SPEC).
    public static let wireFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: EnergyVAD.sampleRate,
        channels: 1,
        interleaved: true
    )!

    public init() {}

    public func start(onFrame: @escaping (PCMFrame) -> Void) throws {
        stop()
        let input = engine.inputNode
        let inFormat = input.inputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else {
            throw AudioCaptureError.noInputFormat
        }
        let out = AudioCapture.wireFormat
        guard let conv = AVAudioConverter(from: inFormat, to: out) else {
            throw AudioCaptureError.converterUnavailable(from: "\(inFormat)", to: "\(out)")
        }
        conv.sampleRateConverterQuality = AVAudioQuality.medium.rawValue
        converter = conv
        carry.removeAll(keepingCapacity: true)

        // A tap buffer near 20 ms keeps the mic-to-socket path short; the
        // converter and the carry buffer make the emitted frame size exact
        // regardless of what CoreAudio actually hands us.
        let tapFrames = AVAudioFrameCount(max(256, Int(inFormat.sampleRate * 0.02)))
        input.installTap(onBus: 0, bufferSize: tapFrames, format: inFormat) { [weak self] buffer, _ in
            self?.consume(buffer, converter: conv, out: out, onFrame: onFrame)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw AudioCaptureError.engineStartFailed(error.localizedDescription)
        }
        lock.lock(); running = true; lock.unlock()
    }

    public func stop() {
        lock.lock()
        let wasRunning = running
        running = false
        lock.unlock()
        if wasRunning || engine.isRunning {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        converter = nil
        carry.removeAll(keepingCapacity: false)
    }

    private func consume(
        _ buffer: AVAudioPCMBuffer,
        converter conv: AVAudioConverter,
        out: AVAudioFormat,
        onFrame: (PCMFrame) -> Void
    ) {
        let ratio = out.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 1024)
        guard let converted = AVAudioPCMBuffer(pcmFormat: out, frameCapacity: capacity) else { return }
        var supplied = false
        var err: NSError?
        let status = conv.convert(to: converted, error: &err) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, converted.frameLength > 0,
              let channel = converted.int16ChannelData else { return }
        let byteCount = Int(converted.frameLength) * 2
        let chunk = Data(bytes: UnsafeRawPointer(channel[0]), count: byteCount)
        lock.lock()
        carry.append(chunk)
        var emit: [Data] = []
        while carry.count >= bytesPerFrame {
            emit.append(carry.prefix(bytesPerFrame))
            carry.removeFirst(bytesPerFrame)
        }
        lock.unlock()
        for pcm in emit {
            let (rms, peak) = PCMEnergy.measure(pcm)
            onFrame(PCMFrame(pcm: pcm, rms: rms, peak: peak))
        }
    }
}

// MARK: - Synthetic source (headless proof, no hardware)

/// Replays a scripted loudness envelope at real time, in the same 20 ms frames
/// the mic produces. This is how `--self-test --turn-engine native` proves the
/// VAD's end-of-turn timing and the whole frame sequence on a machine with no
/// microphone — and, more to the point, without ever opening the one it has.
public final class SyntheticAudioSource: PCMFrameSource {
    /// Speech loud enough to clear the 350 threshold with headroom.
    public static let speechAmplitude: Int16 = 6000
    /// Room tone well under it.
    public static let silenceAmplitude: Int16 = 40

    private let leadSilenceMs: Double
    private let speechMs: Double
    private let queue = DispatchQueue(label: "pet-talk.synthetic-audio")
    private var timer: DispatchSourceTimer?
    private var elapsedMs: Double = 0
    private var phase: Double = 0
    private let lock = NSLock()
    private var running = false

    public var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return running
    }

    public init(leadSilenceMs: Double = 100, speechMs: Double = 700) {
        self.leadSilenceMs = leadSilenceMs
        self.speechMs = speechMs
    }

    public func start(onFrame: @escaping (PCMFrame) -> Void) throws {
        stop()
        let frameMs = Double(EnergyVAD.frameSamples) / EnergyVAD.sampleRate * 1000.0
        lock.lock(); running = true; elapsedMs = 0; lock.unlock()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: .milliseconds(Int(frameMs)))
        t.setEventHandler { [weak self] in
            guard let self = self, self.isRunning else { return }
            let speaking = self.elapsedMs >= self.leadSilenceMs
                && self.elapsedMs < (self.leadSilenceMs + self.speechMs)
            let amp = speaking ? SyntheticAudioSource.speechAmplitude : SyntheticAudioSource.silenceAmplitude
            var pcm = Data(capacity: EnergyVAD.frameSamples * 2)
            for _ in 0..<EnergyVAD.frameSamples {
                self.phase += 2.0 * Double.pi * 180.0 / EnergyVAD.sampleRate
                let v = Int16(Double(amp) * sin(self.phase))
                withUnsafeBytes(of: v.littleEndian) { pcm.append(contentsOf: $0) }
            }
            self.elapsedMs += frameMs
            let (rms, peak) = PCMEnergy.measure(pcm)
            onFrame(PCMFrame(pcm: pcm, rms: rms, peak: peak))
        }
        timer = t
        t.resume()
    }

    public func stop() {
        lock.lock(); running = false; lock.unlock()
        timer?.cancel()
        timer = nil
    }
}
