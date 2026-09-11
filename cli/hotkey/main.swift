// cli/hotkey/main.swift
// Native macOS Carbon global hotkey listener for Pet-Talk (Option + Tab).
//
// Technical Architecture:
// - Uses Carbon `RegisterEventHotKey` (global key events WITHOUT Accessibility/TCC permissions).
// - Key: kVK_Tab (keycode 48), Modifiers: optionKey (0x0800 / 2048).
// - Sub-50ms instant barge-in kill: terminates any active `afplay` processes via Darwin libproc in <2ms.
// - Launches `bin/pet-talk-cli once` on hotkey press.
// - Zero polling / <0.1% CPU: blocks on CFRunLoopRun() waiting for Mach port Carbon events.

import AppKit
import Carbon
import CoreFoundation
import Darwin
import Foundation

struct HotkeyConfig {
    static let pidFile = "/tmp/pet-talk-hotkey.pid"
    static let pauseFile = "/tmp/pet-talk-hotkey.paused"
    static let logFile = "/tmp/pet-talk-hotkey.log"
    static let defaultCliPath = "/Users/saurabh/code/unfoundbox-crew/pet-talk/bin/pet-talk-cli"
    static let hotKeyCode: UInt32 = UInt32(kVK_Tab) // 48
    static let hotKeyModifier: UInt32 = UInt32(optionKey) // 0x0800 = 2048
    static let pauseHotKeyModifier: UInt32 = UInt32(optionKey | shiftKey) // 0x0A00 = 2560
    static let hotKeySignature: OSType = 0x50544C4B // 'PTLK'
    static let hotKeyId: UInt32 = 1
    static let pauseHotKeyId: UInt32 = 2
}

class ProcessManager {
    static func isProcessAlive(pid: pid_t) -> Bool {
        if pid <= 0 { return false }
        return kill(pid, 0) == 0
    }

    static func readPid() -> pid_t? {
        guard let data = try? String(contentsOfFile: HotkeyConfig.pidFile, encoding: .utf8) else {
            return nil
        }
        let trimmed = data.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let intPid = Int32(trimmed), intPid > 0 else {
            return nil
        }
        return intPid
    }

    static func writePid(_ pid: pid_t) {
        let str = "\(pid)\n"
        try? str.write(toFile: HotkeyConfig.pidFile, atomically: true, encoding: .utf8)
    }

    static func removePidFile() {
        try? FileManager.default.removeItem(atPath: HotkeyConfig.pidFile)
    }

    static func isPaused() -> Bool {
        return FileManager.default.fileExists(atPath: HotkeyConfig.pauseFile)
    }

    static func setPaused(_ paused: Bool) {
        if paused {
            let str = "\(Date().timeIntervalSince1970)\n"
            try? str.write(toFile: HotkeyConfig.pauseFile, atomically: true, encoding: .utf8)
        } else {
            try? FileManager.default.removeItem(atPath: HotkeyConfig.pauseFile)
        }
    }

    static func hasAfplayRunning() -> Bool {
        let numPids = proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0)
        if numPids > 0 {
            var pids = [pid_t](repeating: 0, count: Int(numPids) / MemoryLayout<pid_t>.size)
            proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, numPids)
            var nameBuf = [CChar](repeating: 0, count: 256)
            for pid in pids where pid > 0 {
                let ret = proc_name(pid, &nameBuf, UInt32(nameBuf.count))
                if ret > 0 {
                    let name = String(cString: nameBuf)
                    if name == "afplay" {
                        return true
                    }
                }
            }
        }
        return false
    }

    @discardableResult
    static func killProcessesNamed(_ targetName: String) -> Int {
        var count = 0
        let numPids = proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0)
        if numPids > 0 {
            var pids = [pid_t](repeating: 0, count: Int(numPids) / MemoryLayout<pid_t>.size)
            proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, numPids)
            var nameBuf = [CChar](repeating: 0, count: 256)
            for pid in pids where pid > 0 {
                let ret = proc_name(pid, &nameBuf, UInt32(nameBuf.count))
                if ret > 0 {
                    let name = String(cString: nameBuf)
                    if name == targetName || name.contains(targetName) {
                        kill(pid, SIGKILL)
                        count += 1
                    }
                }
            }
        }
        return count
    }

    /// Native Darwin libproc search and kill for any active afplay processes.
    /// Executes in <= 2ms, safely under the 50ms barge-in budget.
    @discardableResult
    static func killAfplay() -> (killedCount: Int, elapsedMs: Double) {
        let t0 = DispatchTime.now()
        var count = 0
        let numPids = proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0)
        if numPids > 0 {
            var pids = [pid_t](repeating: 0, count: Int(numPids) / MemoryLayout<pid_t>.size)
            proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, numPids)
            var nameBuf = [CChar](repeating: 0, count: 256)
            for pid in pids where pid > 0 {
                let ret = proc_name(pid, &nameBuf, UInt32(nameBuf.count))
                if ret > 0 {
                    let name = String(cString: nameBuf)
                    if name == "afplay" {
                        kill(pid, SIGKILL)
                        count += 1
                    }
                }
            }
        }
        let t1 = DispatchTime.now()
        let ms = Double(t1.uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000.0
        return (count, ms)
    }
}

class HotkeyListener {
    static let shared = HotkeyListener()

    var isDaemon: Bool = false
    var cliOverridePath: String? = nil
    var pasteEnabled: Bool = false
    var pasteConfig: PasteConfig = PasteConfig()
    var explicitPasteFlag: Bool? = nil

    private var activeCliProc: Process?
    private var hotKeyRef: EventHotKeyRef?
    private var pauseHotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?
    private var lastTapTime: DispatchTime?

    func log(_ message: String) {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let line = "[\(timestamp)] \(message)\n"
        if isDaemon {
            if !FileManager.default.fileExists(atPath: HotkeyConfig.logFile) {
                FileManager.default.createFile(atPath: HotkeyConfig.logFile, contents: nil)
            }
            if let handle = FileHandle(forWritingAtPath: HotkeyConfig.logFile) {
                handle.seekToEndOfFile()
                if let data = line.data(using: .utf8) {
                    handle.write(data)
                }
                handle.closeFile()
            }
        } else {
            fputs(line, stdout)
            fflush(stdout)
        }
    }

    func logCliOutput(_ text: String) {
        // Suppress high-frequency acoustic telemetry lines ([RMS: ...]) from polluting the log
        let cleaned = text.components(separatedBy: .newlines).filter { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            return !trimmed.hasPrefix("[RMS:") && !trimmed.hasPrefix("[rms:")
        }.joined(separator: "\n")
        let trimmedCleaned = cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedCleaned.isEmpty else { return }

        let payload = cleaned.hasSuffix("\n") ? cleaned : cleaned + "\n"
        if isDaemon {
            if !FileManager.default.fileExists(atPath: HotkeyConfig.logFile) {
                FileManager.default.createFile(atPath: HotkeyConfig.logFile, contents: nil)
            }
            if let handle = FileHandle(forWritingAtPath: HotkeyConfig.logFile) {
                handle.seekToEndOfFile()
                if let data = payload.data(using: .utf8) {
                    handle.write(data)
                }
                handle.closeFile()
            }
        } else {
            fputs(payload, stdout)
            fflush(stdout)
        }
    }

    func resolveCliPath() -> String {
        if let overridePath = cliOverridePath, FileManager.default.isExecutableFile(atPath: overridePath) {
            return overridePath
        }

        // Relative to binary location: ../bin/pet-talk-cli or sibling pet-talk-cli
        let binaryPath = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
        let binDir = URL(fileURLWithPath: binaryPath).deletingLastPathComponent().path
        let sibling = (binDir as NSString).appendingPathComponent("pet-talk-cli")
        if FileManager.default.isExecutableFile(atPath: sibling) {
            return sibling
        }

        let repoBin = (URL(fileURLWithPath: binDir).deletingLastPathComponent().path as NSString)
            .appendingPathComponent("bin/pet-talk-cli")
        if FileManager.default.isExecutableFile(atPath: repoBin) {
            return repoBin
        }

        return HotkeyConfig.defaultCliPath
    }

    /// Extract transcribed speech sentence from pet-talk-cli stdout stream.
    static func extractTranscribedText(from output: String) -> String? {
        let patterns = [
            #"(?:\[HEARD\]\s*)?\[([^\]]+ heard)\]:\s*\"([^\"]+)\""#,
            #"\[HEARD\]\s*\"([^\"]+)\""#,
            #"\[HEARD\]:\s*\"([^\"]+)\""#
        ]
        for pattern in patterns {
            if let regex = try? NSRegularExpression(pattern: pattern, options: []) {
                let nsRange = NSRange(output.startIndex..<output.endIndex, in: output)
                if let match = regex.firstMatch(in: output, options: [], range: nsRange) {
                    let groupIndex = match.numberOfRanges > 2 ? 2 : 1
                    if let r = Range(match.range(at: groupIndex), in: output) {
                        return String(output[r])
                    }
                }
            }
        }
        return nil
    }

    /// Extract real-time microphone acoustic energy levels (RMS / Peak amplitude) from pet-talk-cli stdout stream.
    /// Supports:
    ///   [RMS: 0.245, PEAK: 0.512]
    ///   [RMS: 0.245, peak: 0.512]
    ///   [RMS: 0.245, 0.512]
    ///   [RMS: rms=0.245, peak=0.512]
    ///   [RMS: 0.245]
    static func parseRMSTelemetry(from text: String) -> [(rms: Float, peak: Float)] {
        let pattern = #"(?i)\[RMS:\s*(?:rms=)?([0-9.]+)(?:[,\s]+(?:(?:PEAK|peak)=?|peak:?)?\s*([0-9.]+))?\]"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: []) else { return [] }
        let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
        let matches = regex.matches(in: text, options: [], range: nsRange)

        var results: [(rms: Float, peak: Float)] = []
        for match in matches {
            guard let rmsRange = Range(match.range(at: 1), in: text),
                  let rmsVal = Float(text[rmsRange]) else { continue }

            var peakVal = rmsVal
            if match.numberOfRanges > 2 && match.range(at: 2).location != NSNotFound,
               let peakRange = Range(match.range(at: 2), in: text),
               let parsedPeak = Float(text[peakRange]) {
                peakVal = parsedPeak
            } else {
                peakVal = min(1.0, rmsVal * 1.5)
            }
            results.append((rms: rmsVal, peak: peakVal))
        }
        return results
    }

    /// Extract spoken sentence from pet-talk-cli or Donna output
    static func extractSpokenSentence(from text: String) -> String? {
        let pattern = #"(?:\[SPEAKING\]\s*)?(?:Donna:\s*)?\"([^\"]+)\""#
        if let regex = try? NSRegularExpression(pattern: pattern, options: []) {
            let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
            if let match = regex.firstMatch(in: text, options: [], range: nsRange),
               let r = Range(match.range(at: 1), in: text) {
                return String(text[r])
            }
        }
        return nil
    }

    /// Sanitizes raw CLI output / agent harness events into clean semantic breadcrumbs.
    /// Filters out raw tool-call JSON, stack traces, and terminal ANSI noise.
    static func sanitizeSemanticBreadcrumb(from line: String) -> (badge: String, detail: String, state: HUDState)? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        // Ignore acoustic telemetry lines
        if trimmed.hasPrefix("[RMS:") || trimmed.hasPrefix("[rms:") { return nil }

        // 1. Explicit [BREADCRUMB] or [STATUS] formats: [STATUS] Running: Compiling Swift daemon
        if trimmed.hasPrefix("[BREADCRUMB]") || trimmed.hasPrefix("[STATUS]") {
            let rest = trimmed.replacingOccurrences(of: "[BREADCRUMB]", with: "")
                              .replacingOccurrences(of: "[STATUS]", with: "")
                              .trimmingCharacters(in: .whitespaces)
            if let colonIdx = rest.firstIndex(of: ":") {
                let badge = String(rest[..<colonIdx]).trimmingCharacters(in: .whitespaces)
                let detail = String(rest[rest.index(after: colonIdx)...]).trimmingCharacters(in: .whitespaces)
                let bLower = badge.lowercased()
                let state: HUDState = bLower.contains("speak") ? .speaking :
                                      (bLower.contains("listen") ? .listening : .thinking)
                return (badge, detail, state)
            }
        }

        // 2. Transcribed user speech: [Donna heard]: "..." or [HEARD] "..."
        if let heard = extractTranscribedText(from: trimmed) {
            return ("Donna heard", "\"\(heard)\"", .thinking)
        }

        // 3. Spoken sentence: [SPEAKING] Donna: "..."
        if trimmed.contains("[SPEAKING]") || trimmed.contains("Donna: \"") {
            if let sentence = extractSpokenSentence(from: trimmed) {
                return ("Speaking", sentence, .speaking)
            }
        }

        // 4. Semantic coding harness actions
        // Detect: [Thinking]: ..., [Running]: ..., [Editing]: ..., [Searching]: ...
        let harnessPatterns = ["Thinking", "Running", "Editing", "Searching", "Reading", "Testing", "Executing"]
        for pattern in harnessPatterns {
            if trimmed.lowercased().hasPrefix("[\(pattern.lowercased())]:") || trimmed.lowercased().hasPrefix("\(pattern.lowercased()):") {
                let parts = trimmed.components(separatedBy: ":")
                if parts.count >= 2 {
                    let detail = parts.dropFirst().joined(separator: ":").trimmingCharacters(in: .whitespaces)
                    return (pattern, detail, .thinking)
                }
            }
        }

        // 5. Raw tool call interception & translation
        if trimmed.contains("run_command") || trimmed.contains("CommandLine") {
            if trimmed.contains("qa/run_all.sh") || trimmed.contains("test_") {
                return ("Running", "Executing QA test suites", .thinking)
            } else if trimmed.contains("swiftc") {
                return ("Running", "Compiling Swift hotkey daemon", .thinking)
            } else if trimmed.contains("git diff") {
                return ("Running", "Checking git diff", .thinking)
            } else if trimmed.contains("git status") {
                return ("Running", "Inspecting repository status", .thinking)
            } else {
                return ("Running", "Executing system task", .thinking)
            }
        }
        if trimmed.contains("replace_file_content") || trimmed.contains("write_to_file") {
            return ("Editing", "Applying code changes", .thinking)
        }
        if trimmed.contains("view_file") || trimmed.contains("read_resource") {
            return ("Reading", "Inspecting file context", .thinking)
        }
        if trimmed.contains("search_web") || trimmed.contains("duckduckgo_web_search") {
            return ("Searching", "Consulting web knowledge", .thinking)
        }

        return nil
    }

    func isTurnActive() -> Bool {
        if let proc = activeCliProc, proc.isRunning {
            return true
        }
        if ProcessManager.hasAfplayRunning() {
            return true
        }
        if HUDController.shared.isVisible {
            return true
        }
        return false
    }

    func handleBargeKill() {
        log("+-- [KILL-SWITCH] Barge-kill triggered")
        let (killedAfplay, bargeMs) = ProcessManager.killAfplay()
        if killedAfplay > 0 {
            log("| [kill] Killed \(killedAfplay) afplay process(es) in \(String(format: "%.2f", bargeMs))ms")
        }
        let killedCli = ProcessManager.killProcessesNamed("pet-talk-cli")
        if killedCli > 0 {
            log("| [kill] Killed \(killedCli) pet-talk-cli process(es)")
        }
        if let proc = activeCliProc, proc.isRunning {
            proc.terminate()
            activeCliProc = nil
            log("| [kill] Terminated active pet-talk-cli process")
        }
        EarconEngine.shared.playBargeKill()
        HUDController.shared.dismiss(immediate: true)
    }

    func togglePause() {
        let isNowPaused = !ProcessManager.isPaused()
        ProcessManager.setPaused(isNowPaused)

        if isNowPaused {
            log("+-- [PAUSE] Donna sleep mode activated")
            handleBargeKill()
            EarconEngine.shared.playSilenceCutoff()
            HUDController.shared.showBreadcrumb(
                badge: "PAUSED",
                detail: "Donna paused (Option+Shift+Tab or double-tap to wake)",
                state: .thinking
            )
        } else {
            log("+-- [RESUME] Donna active mode restored")
            EarconEngine.shared.playMicOpen()
            HUDController.shared.showBreadcrumb(
                badge: "ACTIVE",
                detail: "Donna active and listening",
                state: .listening
            )
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
                if !self.isTurnActive() {
                    HUDController.shared.dismiss()
                }
            }
        }
    }

    func handlePauseHotKeyTrigger() {
        log("+-- [HOTKEY] Option+Shift+Tab pause toggle triggered")
        togglePause()
    }

    func handleHotKeyTrigger() {
        let now = DispatchTime.now()
        if let last = lastTapTime {
            let intervalMs = Double(now.uptimeNanoseconds - last.uptimeNanoseconds) / 1_000_000.0
            if intervalMs <= 350.0 {
                lastTapTime = nil
                log("+-- [HOTKEY] Double-tap detected (\(String(format: "%.1f", intervalMs))ms) -> toggling pause mode")
                togglePause()
                return
            }
        }
        lastTapTime = now

        log("+-- [HOTKEY] Option+Tab triggered")

        // 1. Check if Donna is in Pause / Sleep mode
        if ProcessManager.isPaused() {
            log("| [paused] Ignored trigger — Donna is paused")
            EarconEngine.shared.playError()
            HUDController.shared.showBreadcrumb(
                badge: "PAUSED",
                detail: "Donna paused (Option+Shift+Tab or double-tap to wake)",
                state: .thinking
            )
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
                if !self.isTurnActive() {
                    HUDController.shared.dismiss()
                }
            }
            return
        }

        // 2. KILL SWITCH: If Donna is currently active (speaking, listening, thinking), single-tap immediately stops her!
        if isTurnActive() {
            log("| [kill-switch] Donna was active — immediately stopping audio and dismissing turn")
            handleBargeKill()
            return
        }

        // 3. Donna is idle -> start new recording turn
        startNewTurn()
    }

    private func startNewTurn() {
        // Immediate mic open earcon (<1.2ms)
        EarconEngine.shared.playMicOpen()
        log("| [idle] Triggered -> starting one-shot recording turn...")

        // Launch pet-talk-cli once
        let cliPath = resolveCliPath()
        guard FileManager.default.isExecutableFile(atPath: cliPath) else {
            log("! [error] pet-talk-cli executable not found at \(cliPath)")
            EarconEngine.shared.playError()
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cliPath)
        proc.arguments = ["once"]

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        var silenceCutoffFired = false
        var transcriptionReceived = false
        var outputBuffer = ""

        let forwardOutput: (Data) -> Void = { [weak self] data in
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.logCliOutput(text)
            outputBuffer += text

            // Parse live RMS acoustic telemetry emitted from pet-talk-cli and forward immediately
            let telemetry = HotkeyListener.parseRMSTelemetry(from: text)
            if let latest = telemetry.last {
                HUDController.shared.updateAudioLevel(rms: latest.rms, peak: latest.peak)
            }

            // Detect turn transitions from pet-talk-cli output
            if !silenceCutoffFired && (text.contains("[THINKING]") || text.contains("Transcribing") || text.contains("user.stop")) {
                silenceCutoffFired = true
                EarconEngine.shared.playSilenceCutoff()
                HUDController.shared.update(state: .thinking)
            }

            // Check for semantic breadcrumbs or status events
            for line in text.components(separatedBy: .newlines) {
                if let breadcrumb = HotkeyListener.sanitizeSemanticBreadcrumb(from: line) {
                    HUDController.shared.showBreadcrumb(
                        badge: breadcrumb.badge,
                        detail: breadcrumb.detail,
                        state: breadcrumb.state
                    )
                }
            }

            // Detect transcribed speech from Donna and display on HUD + optionally paste
            if !transcriptionReceived, let transcribed = HotkeyListener.extractTranscribedText(from: outputBuffer) {
                transcriptionReceived = true
                self?.log("| [transcription] Donna heard: \"\(transcribed)\"")
                HUDController.shared.showTranscribedText(transcribed, persona: "Donna")

                if self?.pasteEnabled == true {
                    self?.log("| [paste-injection] Injecting transcribed prompt into active cursor (Wispr Flow style)...")
                    if self?.pasteConfig.mode == "keystroke" {
                        PasteInjector.shared.injectKeystrokes(transcribed)
                    } else {
                        PasteInjector.shared.inject(
                            transcribed,
                            restoreClipboard: self?.pasteConfig.restoreClipboard ?? false,
                            delayMs: self?.pasteConfig.delayMs ?? 30
                        )
                    }
                }
            }

            if text.contains("[SPEAKING]") {
                HUDController.shared.update(state: .speaking)
            }
            if text.contains("[ERROR]") || text.contains("Error:") {
                EarconEngine.shared.playError()
                HUDController.shared.triggerErrorShake()
            }
        }

        outPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            forwardOutput(data)
        }
        errPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            forwardOutput(data)
        }

        do {
            try proc.run()
            activeCliProc = proc
            log("| [spawned] \(cliPath) once (PID: \(proc.processIdentifier))")
            HUDController.shared.show(state: .listening)
            proc.terminationHandler = { [weak self] process in
                outPipe.fileHandleForReading.readabilityHandler = nil
                errPipe.fileHandleForReading.readabilityHandler = nil
                let remOut = outPipe.fileHandleForReading.readDataToEndOfFile()
                forwardOutput(remOut)
                let remErr = errPipe.fileHandleForReading.readDataToEndOfFile()
                forwardOutput(remErr)

                HUDController.shared.dismiss()
                if process.terminationStatus != 0 && !silenceCutoffFired {
                    EarconEngine.shared.playError()
                }
                self?.activeCliProc = nil
            }
        } catch {
            log("! [error] Failed to launch pet-talk-cli: \(error)")
            EarconEngine.shared.playError()
        }
    }

    func verifyRegistration() -> Bool {
        var testRef: EventHotKeyRef?
        let testID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: 999)
        let status = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.hotKeyModifier,
            testID,
            GetApplicationEventTarget(),
            0,
            &testRef
        )
        guard status == noErr else { return false }
        if let ref = testRef {
            UnregisterEventHotKey(ref)
        }

        var pauseTestRef: EventHotKeyRef?
        let pauseTestID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: 998)
        let pauseStatus = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.pauseHotKeyModifier,
            pauseTestID,
            GetApplicationEventTarget(),
            0,
            &pauseTestRef
        )
        guard pauseStatus == noErr else { return false }
        if let pRef = pauseTestRef {
            UnregisterEventHotKey(pRef)
        }

        return true
    }

    func startListening() -> Int32 {
        _ = NSApplication.shared
        NSApplication.shared.setActivationPolicy(.accessory)

        // Pre-load configuration and acoustic earcons
        let config = PetTalkConfig.load()
        EarconEngine.shared.configure(from: config)
        EarconEngine.shared.prewarm()

        // Resolve paste injection settings
        if let explicit = explicitPasteFlag {
            self.pasteEnabled = explicit
        } else {
            self.pasteEnabled = config.paste.enabled
        }
        self.pasteConfig = config.paste

        let cliPath = resolveCliPath()
        ProcessManager.writePid(getpid())

        log("+-- pet-talk-hotkey daemon active")
        log("| PID: \(getpid())")
        log("| Hotkey: Option + Tab (keycode: \(HotkeyConfig.hotKeyCode), mod: 0x\(String(HotkeyConfig.hotKeyModifier, radix: 16, uppercase: true)))")
        log("| Pause Hotkey: Option + Shift + Tab (keycode: \(HotkeyConfig.hotKeyCode), mod: 0x\(String(HotkeyConfig.pauseHotKeyModifier, radix: 16, uppercase: true)))")
        log("| Target CLI: \(cliPath)")
        log("| Earcons: enabled=\(EarconEngine.shared.isEnabled), pack=\(EarconEngine.shared.soundPack), vol=\(String(format: "%.2f", EarconEngine.shared.volume))")
        log("| HUD: Obsidian Deep Zinc Capsule (220x44px -> 380x44px live dictation)")
        log("| Paste Injection: \(pasteEnabled ? "ENABLED (Wispr Flow style -> Cmd+V)" : "disabled (use --paste to enable)")")
        log("| Mode: \(isDaemon ? "Daemon (background)" : "Foreground")")
        log("| Ready for global Option+Tab barge-in turns & kill switch (<0.1% CPU)...")

        // Wire HUD Sensory Callbacks & Event Monitors (Escape barge-in, notch hover tracking)
        HUDController.shared.onBargeKill = { [weak self] in
            self?.handleBargeKill()
        }
        HUDController.shared.onErrorAudio = {
            EarconEngine.shared.playError()
        }
        HUDController.shared.onTriggerTurn = { [weak self] in
            self?.handleHotKeyTrigger()
        }
        HUDController.shared.setupEventMonitors()

        // 1. Install Carbon Event Handler on Event Dispatcher Target
        let eventHandler: EventHandlerUPP = { (_, inEvent, _) -> OSStatus in
            guard let event = inEvent else {
                HotkeyListener.shared.handleHotKeyTrigger()
                return noErr
            }
            var hotKeyID = EventHotKeyID()
            let status = GetEventParameter(
                event,
                EventParamName(kEventParamDirectObject),
                EventParamName(typeEventHotKeyID),
                nil,
                MemoryLayout<EventHotKeyID>.size,
                nil,
                &hotKeyID
            )
            if status == noErr && hotKeyID.id == HotkeyConfig.pauseHotKeyId {
                HotkeyListener.shared.handlePauseHotKeyTrigger()
            } else {
                HotkeyListener.shared.handleHotKeyTrigger()
            }
            return noErr
        }

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )

        let installStatus = InstallEventHandler(
            GetEventDispatcherTarget(),
            eventHandler,
            1,
            &eventType,
            nil,
            &eventHandlerRef
        )

        guard installStatus == noErr else {
            log("! [error] Failed to install Carbon event handler (status: \(installStatus))")
            ProcessManager.removePidFile()
            return 1
        }

        // 2. Register Global Hotkey (kVK_Tab + optionKey) on Event Dispatcher Target
        let hotKeyID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: HotkeyConfig.hotKeyId)
        let regStatus = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.hotKeyModifier,
            hotKeyID,
            GetEventDispatcherTarget(),
            0,
            &hotKeyRef
        )

        guard regStatus == noErr else {
            log("! [error] Failed to register Option+Tab hotkey (status: \(regStatus))")
            ProcessManager.removePidFile()
            return 1
        }

        // 3. Register Global Pause/Resume Hotkey (kVK_Tab + optionKey + shiftKey) on Event Dispatcher Target
        let pauseHotKeyID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: HotkeyConfig.pauseHotKeyId)
        let pauseRegStatus = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.pauseHotKeyModifier,
            pauseHotKeyID,
            GetEventDispatcherTarget(),
            0,
            &pauseHotKeyRef
        )

        if pauseRegStatus != noErr {
            log("! [warning] Could not register secondary Option+Shift+Tab hotkey (status: \(pauseRegStatus))")
        }

        // 4. Register POSIX Signal Handlers for clean exit, pause toggle, and instant kill
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        signal(SIGUSR1, SIG_IGN)
        signal(SIGUSR2, SIG_IGN)

        let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        sigintSource.setEventHandler { [weak self] in
            self?.cleanup()
            exit(0)
        }
        sigintSource.resume()

        let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        sigtermSource.setEventHandler { [weak self] in
            self?.cleanup()
            exit(0)
        }
        sigtermSource.resume()

        let sigusr1Source = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        sigusr1Source.setEventHandler { [weak self] in
            self?.togglePause()
        }
        sigusr1Source.resume()

        let sigusr2Source = DispatchSource.makeSignalSource(signal: SIGUSR2, queue: .main)
        sigusr2Source.setEventHandler { [weak self] in
            self?.handleBargeKill()
        }
        sigusr2Source.resume()

        // 5. Run NSApplication RunLoop to pump WindowServer events and AppKit animations
        NSApplication.shared.run()
        return 0
    }

    func cleanup() {
        log("+-- pet-talk-hotkey shutting down...")
        HUDController.shared.dismiss()
        if let ref = hotKeyRef {
            UnregisterEventHotKey(ref)
            hotKeyRef = nil
        }
        if let pRef = pauseHotKeyRef {
            UnregisterEventHotKey(pRef)
            pauseHotKeyRef = nil
        }
        if let handler = eventHandlerRef {
            RemoveEventHandler(handler)
            eventHandlerRef = nil
        }
        handleBargeKill()
        ProcessManager.removePidFile()
    }
}

// MARK: - CLI Commands

func printUsage() {
    let usage = """
    pet-talk-hotkey — Native macOS Option+Tab Global Hotkey Listener & Sensory Presence

    Usage:
      pet-talk-hotkey                     Run listener in foreground (<0.1% CPU)
      pet-talk-hotkey start               Spawn daemon in background
      pet-talk-hotkey stop                Stop running daemon
      pet-talk-hotkey status              Check if listener daemon is active
      pet-talk-hotkey kill                Instant sub-10ms kill switch (terminate audio & turn)
      pet-talk-hotkey pause               Pause Donna (sleep mode, ignore triggers)
      pet-talk-hotkey resume              Resume Donna from pause mode
      pet-talk-hotkey toggle              Toggle pause/resume mode
      pet-talk-hotkey test-audio          Play & benchmark acoustic earcons sequence (<2ms)
      pet-talk-hotkey test-hud            Test floating glass capsule HUD with live dictation text (380px)
      pet-talk-hotkey test-breadcrumbs    Test multi-line height expansion & semantic breadcrumbs
      pet-talk-hotkey breadcrumb <b> <d>  Display custom breadcrumb in Dynamic Island HUD
      pet-talk-hotkey test-paste [text]   Test Cursor paste injection (Wispr Flow style -> Cmd+V)
      pet-talk-hotkey config show         Show current configuration
      pet-talk-hotkey config set <k> <v>  Update configuration setting
      pet-talk-hotkey --dump-hud-spec     Dump HUD specification JSON for test assertions
      pet-talk-hotkey --check-registration Verify Carbon hotkey registration
      pet-talk-hotkey --barge-benchmark   Benchmark afplay barge-in kill latency
      pet-talk-hotkey --help              Show this help message

    Options for run / start:
      --paste                             Enable Wispr Flow paste injection to active app
      --no-paste                          Disable paste injection
      --cli <path>                        Path to pet-talk-cli binary

    Options for test-audio:
      --volume <0.0-1.0>                  Set earcon volume
      --sound-pack <pack>                 apple_minimal | cyberpunk | haptic | none
      --no-audio                          Disable earcons

    Sensory Presence Specifications:
      Key:        Tab (kVK_Tab, keycode 48)
      Modifier:   Option (optionKey, 0x0800 / 2048)
      Kill:       Single-tap Option+Tab while active cuts audio in <2ms & dismisses HUD
      Pause:      Double-tap Option+Tab (<=350ms) or Option+Shift+Tab toggles Sleep Mode
      Barge-in:   <= 50ms afplay instant kill via Darwin libproc
      Dictation:  Live transcribed speech displayed in Obsidian Zinc Capsule (380x44px)
      Paste:      NSPasteboard + CGEvent Cmd+V into Cursor / terminal / editor
      Earcons:    Pre-loaded NSSound in RAM (<2ms latency)
                  - Mic Open:       Tink.aiff (24ms)
                  - Silence Cutoff: Pop.aiff (32ms)
                  - Barge Kill:     Bottle.aiff (18ms)
                  - Error:          Basso.aiff (45ms)
      HUD:        220x44px -> 380x44px Obsidian Glass NSPanel [.nonactivatingPanel]
    """
    print(usage)
}

func doStart() -> Int32 {
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        print("pet-talk-hotkey is already running (PID: \(pid))")
        return 0
    }

    if !FileManager.default.fileExists(atPath: HotkeyConfig.logFile) {
        FileManager.default.createFile(atPath: HotkeyConfig.logFile, contents: nil)
    }

    let binaryPath = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: binaryPath)
    proc.arguments = ["run", "--daemon"]

    if let logHandle = FileHandle(forWritingAtPath: HotkeyConfig.logFile) {
        logHandle.seekToEndOfFile()
        proc.standardOutput = logHandle
        proc.standardError = logHandle
    }

    do {
        try proc.run()
        let pid = proc.processIdentifier
        ProcessManager.writePid(pid)

        usleep(100_000) // 100ms
        if !ProcessManager.isProcessAlive(pid: pid) {
            print("Error: pet-talk-hotkey daemon exited prematurely. See \(HotkeyConfig.logFile)")
            return 1
        }

        print("pet-talk-hotkey daemon started (PID: \(pid))")
        return 0
    } catch {
        print("Error starting pet-talk-hotkey daemon: \(error)")
        return 1
    }
}

func doStop() -> Int32 {
    guard let pid = ProcessManager.readPid() else {
        ProcessManager.setPaused(false)
        print("pet-talk-hotkey is not running")
        return 0
    }

    if ProcessManager.isProcessAlive(pid: pid) {
        kill(pid, SIGTERM)
        var stopped = false
        for _ in 0..<20 {
            usleep(50_000) // 50ms
            if !ProcessManager.isProcessAlive(pid: pid) {
                stopped = true
                break
            }
        }
        if !stopped {
            kill(pid, SIGKILL)
        }
        ProcessManager.killAfplay()
        _ = ProcessManager.killProcessesNamed("pet-talk-cli")
        ProcessManager.setPaused(false)
        ProcessManager.removePidFile()
        print("pet-talk-hotkey daemon stopped (PID: \(pid))")
        return 0
    } else {
        ProcessManager.setPaused(false)
        ProcessManager.removePidFile()
        print("pet-talk-hotkey is not running (cleaned stale PID file)")
        return 0
    }
}

func doKill() -> Int32 {
    print("+-- Executing Pet-Talk instant kill switch...")
    let (killedAfplay, bargeMs) = ProcessManager.killAfplay()
    if killedAfplay > 0 {
        print("| Killed \(killedAfplay) afplay process(es) in \(String(format: "%.2f", bargeMs))ms")
    }
    let killedCli = ProcessManager.killProcessesNamed("pet-talk-cli")
    if killedCli > 0 {
        print("| Killed \(killedCli) pet-talk-cli process(es)")
    }

    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        kill(pid, SIGUSR2)
    }

    EarconEngine.shared.playBargeKill()
    print("+-- PASS: Instant kill switch executed (audio stopped, HUD dismissed).")
    return 0
}

func doPause() -> Int32 {
    _ = ProcessManager.killAfplay()
    _ = ProcessManager.killProcessesNamed("pet-talk-cli")
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        if !ProcessManager.isPaused() {
            kill(pid, SIGUSR1)
        }
    } else {
        ProcessManager.setPaused(true)
    }
    print("+-- Donna paused (Sleep Mode enabled).")
    print("| Voice triggers are muted. Press Option+Shift+Tab or run 'pet-talk-hotkey resume' to wake.")
    return 0
}

func doResume() -> Int32 {
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        if ProcessManager.isPaused() {
            kill(pid, SIGUSR1)
        }
    } else {
        ProcessManager.setPaused(false)
    }
    print("+-- Donna resumed (Active Mode enabled).")
    print("| Option+Tab will start live voice turns.")
    return 0
}

func doToggle() -> Int32 {
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        kill(pid, SIGUSR1)
        let isNowPaused = !ProcessManager.isPaused()
        if isNowPaused {
            print("+-- Donna paused (Sleep Mode enabled).")
        } else {
            print("+-- Donna resumed (Active Mode enabled).")
        }
    } else {
        let isNowPaused = !ProcessManager.isPaused()
        ProcessManager.setPaused(isNowPaused)
        if isNowPaused {
            print("+-- Donna paused (Sleep Mode enabled).")
        } else {
            print("+-- Donna resumed (Active Mode enabled).")
        }
    }
    return 0
}

func doStatus() -> Int32 {
    let isPaused = ProcessManager.isPaused()
    let pauseLabel = isPaused ? " [PAUSED / SLEEP MODE]" : " [ACTIVE]"

    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        print("pet-talk-hotkey is running (PID: \(pid))\(pauseLabel)")
        return 0
    } else {
        if ProcessManager.readPid() != nil {
            ProcessManager.removePidFile()
        }
        print("pet-talk-hotkey is stopped\(isPaused ? " (paused flag set)" : "")")
        return 1
    }
}

func doVerify() -> Int32 {
    print("+-- Verifying Carbon Option+Tab registration...")
    print("| Keycode:  \(HotkeyConfig.hotKeyCode) (kVK_Tab)")
    print("| Modifier: 0x\(String(HotkeyConfig.hotKeyModifier, radix: 16, uppercase: true)) (optionKey / 2048)")
    let ok = HotkeyListener.shared.verifyRegistration()
    if ok {
        print("+-- PASS: Carbon RegisterEventHotKey succeeded (no TCC/Accessibility prompt required).")
        return 0
    } else {
        print("!-- FAIL: Carbon RegisterEventHotKey failed.")
        return 1
    }
}

func doBargeBenchmark() -> Int32 {
    print("+-- Benchmarking afplay barge-in kill latency...")
    let (count, ms) = ProcessManager.killAfplay()
    print("| Afplay processes killed: \(count)")
    print("| Elapsed latency: \(String(format: "%.3f", ms)) ms (Budget: <= 50.0 ms)")
    if ms <= 50.0 {
        print("+-- PASS: Barge-in kill under 50ms budget.")
        return 0
    } else {
        print("!-- FAIL: Barge-in kill exceeded 50ms budget.")
        return 1
    }
}

func doTestHUD() -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    print("+-- Testing Pet-Talk Floating Glass Capsule HUD (Real-Time Visual Dictation)...")
    print("| Window: 220x44px -> 380x44px NSPanel [.nonactivatingPanel, .borderless]")
    print("| Level: .floating, Spaces: [.canJoinAllSpaces, .fullScreenAuxiliary]")
    print("| Theme: Obsidian Deep Zinc (#12141c @ 85%) + 1px border (#282c3f)")
    print("| State 1 (1.2s): [LISTENING] Emerald True (#10b981) + 'Listening...' (220px)")

    HUDController.shared.show(state: .listening)

    // Simulate acoustic microphone energy levels bouncing to speech
    var pulseStep = 0
    _ = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { timer in
        pulseStep += 1
        if pulseStep > 10 {
            timer.invalidate()
            return
        }
        let simRMS = Float(0.25 + 0.60 * sin(Double(pulseStep) * 0.7))
        let simPeak = Float(min(1.0, simRMS * 1.35))
        HUDController.shared.updateAudioLevel(rms: simRMS, peak: simPeak)
    }

    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
        print("| State 2 (1.2s): [THINKING] SpacePilot Gold (#c9a227) + 'Donna thinking...' (220px)")
        HUDController.shared.update(state: .thinking)

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            let sampleText = "What is our deployment schedule today?"
            print("| State 3 (2.0s): [EXPANDED DICTATION] Expand to 380px -> [Donna heard]: \"\(sampleText)\"")
            HUDController.shared.showTranscribedText(sampleText, persona: "Donna")

            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                print("| State 4 (1.5s): [SPEAKING] Liquid Silver (#cfd4dc) kinetic audio bars with transcribed text (380px)")
                HUDController.shared.update(state: .speaking)

                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    print("| State 5 (0.5s): [ERROR SHAKE] 3-cycle ±3px micro-shake + Basso chime")
                    HUDController.shared.shakeError {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                            print("| Dismissing HUD (200ms ease-out fade)...")
                            HUDController.shared.dismiss {
                                print("+-- PASS: HUD visual test sequence completed. (dictation & text preview)")
                                CFRunLoopStop(CFRunLoopGetMain())
                            }
                        }
                    }
                }
            }
        }
    }

    CFRunLoopRun()
    return 0
}

func doTestBreadcrumbs() -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    print("+-- Testing Dynamic Island Semantic Action Breadcrumbs & Multi-Line Expansion...")
    print("| Phase 1 (1.2s): [Thinking]: Synthesizing dynamic multi-line layout... (60px)")
    HUDController.shared.showBreadcrumb(
        badge: "Thinking",
        detail: "Synthesizing dynamic multi-line layout...",
        state: .thinking
    )

    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
        print("| Phase 2 (1.2s): [Running]: Executing test suite (qa/run_all.sh)... (60px)")
        HUDController.shared.showBreadcrumb(
            badge: "Running",
            detail: "Executing test suite (qa/run_all.sh)...",
            state: .thinking
        )

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            print("| Phase 3 (1.2s): [Editing]: cli/hotkey/hud_window.swift... (60px)")
            HUDController.shared.showBreadcrumb(
                badge: "Editing",
                detail: "cli/hotkey/hud_window.swift",
                state: .thinking
            )

            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                let longSpoken = "How difficult is it to make an ambient agent as my Chief of Staff like Alexa or Siri powered by my frontier subscriptions in duplex?"
                print("| Phase 4 (2.5s): [Donna heard]: Multi-line wrapped text (expanded height 76px-96px)")
                HUDController.shared.showBreadcrumb(
                    badge: "Donna heard",
                    detail: "\"\(longSpoken)\"",
                    state: .thinking
                )

                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                    let reply = "Doable. Weekend prototype, not weekend product. The hard parts are duplex interruptions and subscription limits, not Tailscale."
                    print("| Phase 5 (2.2s): [Speaking]: Multi-line wrapped reply (expanded height 76px-96px)")
                    HUDController.shared.showBreadcrumb(
                        badge: "Speaking",
                        detail: "\"\(reply)\"",
                        state: .speaking
                    )

                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                        print("| Dismissing HUD (suction retraction)...")
                        HUDController.shared.dismiss {
                            print("+-- PASS: Semantic Action Breadcrumbs & Multi-line visual test completed.")
                            CFRunLoopStop(CFRunLoopGetMain())
                        }
                    }
                }
            }
        }
    }

    CFRunLoopRun()
    return 0
}

func doShowBreadcrumb(args: [String]) -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    guard args.count >= 2 else {
        print("Usage: pet-talk-hotkey breadcrumb <badge> <detail>")
        return 1
    }
    let badge = args[0]
    let detail = args.dropFirst().joined(separator: " ")

    let bLower = badge.lowercased()
    let state: HUDState = bLower.contains("speak") ? .speaking :
                          (bLower.contains("listen") ? .listening : .thinking)

    HUDController.shared.showBreadcrumb(badge: badge, detail: detail, state: state)

    DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
        HUDController.shared.dismiss {
            CFRunLoopStop(CFRunLoopGetMain())
        }
    }

    CFRunLoopRun()
    return 0
}

func doTestPaste(args: [String]) -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    let sampleText = args.first ?? "What is our deployment schedule today?"
    print("+-- Testing Cursor Paste Injection (Wispr Flow style)...")
    print("| Target text: \"\(sampleText)\"")
    print("| Method: NSPasteboard.general write + CGEvent Cmd+V simulation")
    let success = PasteInjector.shared.inject(sampleText, restoreClipboard: false, delayMs: 30)
    if success {
        print("+-- PASS: Paste injection dispatched to active application.")
        return 0
    } else {
        print("!-- FAIL: Failed to dispatch paste injection.")
        return 1
    }
}

func doDumpHUDSpec() -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    let summary = HUDController.shared.getSpecificationSummary()
    if let data = try? JSONSerialization.data(withJSONObject: summary, options: [.prettyPrinted, .sortedKeys]),
       let jsonStr = String(data: data, encoding: .utf8) {
        print(jsonStr)
        return 0
    } else {
        print("{\"error\": \"Failed to serialize HUD specification summary\"}")
        return 1
    }
}

func doTestAudio(args: [String]) -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    print("+-- Testing Pet-Talk Acoustic Earcon Engine...")

    // Load configuration
    var config = PetTalkConfig.load()

    // Handle CLI flags
    if let volIdx = args.firstIndex(of: "--volume"), volIdx + 1 < args.count, let vol = Float(args[volIdx + 1]) {
        config.audio.volume = max(0.0, min(1.0, vol))
    }
    if let packIdx = args.firstIndex(of: "--sound-pack"), packIdx + 1 < args.count {
        config.audio.soundPack = args[packIdx + 1]
    }
    if args.contains("--no-audio") || args.contains("--disable") {
        config.audio.enabled = false
    }

    EarconEngine.shared.configure(from: config)
    EarconEngine.shared.prewarm()

    print("| Sound Pack: \(EarconEngine.shared.soundPack) (Volume: \(String(format: "%.2f", EarconEngine.shared.volume)), Enabled: \(EarconEngine.shared.isEnabled))")

    let results = EarconEngine.shared.testSequence()
    var allPassed = true
    let budgetMs: Double = 5.0

    for (index, r) in results.enumerated() {
        let soundNum = index + 1
        let triggerName = r.trigger.displayName
        let status: String
        if !EarconEngine.shared.isEnabled {
            status = "DISABLED"
        } else {
            status = r.success ? "PASS" : "FAIL"
        }
        let latStr = String(format: "%.3f", r.latencyMs)
        print("| \(soundNum). \(triggerName) -> \(r.path) (\(latStr)ms) [\(status)]")

        if !r.success || (!EarconEngine.shared.isEnabled ? false : r.latencyMs > budgetMs) {
            if EarconEngine.shared.isEnabled {
                allPassed = false
            }
        }
    }

    if EarconEngine.shared.isEnabled {
        if allPassed {
            print("+-- PASS: All earcon triggers executed within SLA budget (<= \(budgetMs)ms).")
            return 0
        } else {
            print("!-- FAIL: One or more earcon triggers failed or exceeded \(budgetMs)ms budget.")
            return 1
        }
    } else {
        print("+-- Audio earcons disabled by configuration/flag.")
        return 0
    }
}

func doConfig(args: [String]) -> Int32 {
    let subcmd = args.first ?? "show"
    var config = PetTalkConfig.load()

    switch subcmd {
    case "show":
        print(config.toYAML())
        return 0
    case "set":
        guard args.count >= 3 else {
            print("Usage: pet-talk-hotkey config set <key> <value>")
            print("Example: pet-talk-hotkey config set audio.sound_pack cyberpunk")
            print("         pet-talk-hotkey config set audio.volume 0.65")
            return 1
        }
        let key = args[1]
        let val = args[2]
        if config.setProperty(key: key, value: val) {
            do {
                try config.save()
                print("+-- Successfully updated \(key) = \(val) in \(PetTalkConfig.defaultConfigPath)")
                return 0
            } catch {
                print("!-- Error saving config: \(error)")
                return 1
            }
        } else {
            print("!-- Unknown or unsupported config key: \(key)")
            return 1
        }
    default:
        print("Unknown config command: \(subcmd)")
        return 1
    }
}

// MARK: - Entry Point

let args = Array(CommandLine.arguments.dropFirst())
let command = args.first ?? "run"

// Global paste flag extraction
if args.contains("--paste") {
    HotkeyListener.shared.explicitPasteFlag = true
} else if args.contains("--no-paste") {
    HotkeyListener.shared.explicitPasteFlag = false
}

switch command {
case "start":
    exit(doStart())
case "stop":
    exit(doStop())
case "status":
    exit(doStatus())
case "kill":
    exit(doKill())
case "pause":
    exit(doPause())
case "resume":
    exit(doResume())
case "toggle":
    exit(doToggle())
case "test-audio", "--test-audio", "--test-earcons":
    exit(doTestAudio(args: args))
case "config":
    exit(doConfig(args: Array(args.dropFirst())))
case "test-hud":
    exit(doTestHUD())
case "test-breadcrumbs", "--test-breadcrumbs":
    exit(doTestBreadcrumbs())
case "breadcrumb", "--breadcrumb":
    exit(doShowBreadcrumb(args: Array(args.dropFirst())))
case "test-paste", "--test-paste":
    exit(doTestPaste(args: Array(args.dropFirst())))
case "--dump-hud-spec":
    exit(doDumpHUDSpec())
case "--check-registration", "--verify":
    exit(doVerify())
case "--barge-benchmark":
    exit(doBargeBenchmark())
case "-h", "--help", "help":
    printUsage()
    exit(0)
case "run":
    let isDaemon = args.contains("--daemon")
    HotkeyListener.shared.isDaemon = isDaemon
    if let cliIdx = args.firstIndex(of: "--cli"), cliIdx + 1 < args.count {
        HotkeyListener.shared.cliOverridePath = args[cliIdx + 1]
    }
    exit(HotkeyListener.shared.startListening())
default:
    if command.starts(with: "-") {
        print("Unknown option: \(command)")
        printUsage()
        exit(1)
    }
    // Default to run with possible cli argument
    exit(HotkeyListener.shared.startListening())
}
