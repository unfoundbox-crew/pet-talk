// cli/hotkey/main.swift
// Native macOS Carbon global hotkey listener for Pet-Talk.
//
// Gestures (one chord, one meaning):
//   Option+Tab              ask / kill the live turn
//   Option+Tab twice        pause / resume (inside HUDTokens.doubleTapWindowMs)
//   Option+Shift+Tab        hand over to the agent
//
// Technical Architecture:
// - Uses Carbon `RegisterEventHotKey` (global key events WITHOUT Accessibility/TCC permissions).
// - Key: kVK_Tab (keycode 48), Modifiers: optionKey (0x0800 / 2048).
// - Sub-50ms instant barge-in kill: terminates any active `afplay` processes via Darwin libproc in <2ms.
// - Launches `bin/pet-talk-cli once` on hotkey press.
// - Zero polling / <0.1% CPU: blocks on CFRunLoopRun() waiting for Mach port Carbon events.

import AppKit
import ApplicationServices
import Carbon
import CoreFoundation
import Darwin
import Foundation

struct HotkeyConfig {
    static let hotKeyCode: UInt32 = UInt32(kVK_Tab) // 48
    static let hotKeyModifier: UInt32 = UInt32(optionKey) // 0x0800 = 2048
    /// Option+Shift+Tab is HAND-OVER (was pause until 2026-09-12; pause moved to
    /// a double-tap of Option+Tab so the chord could carry the hand-over).
    static let handoverHotKeyModifier: UInt32 = UInt32(optionKey | shiftKey) // 0x0A00 = 2560
    static let hotKeySignature: OSType = 0x50544C4B // 'PTLK'
    static let hotKeyId: UInt32 = 1
    static let handoverHotKeyId: UInt32 = 2

    /// Private per-user runtime directory for state files. $XDG_RUNTIME_DIR is
    /// already 0700-and-tmpfs on Linux; on macOS (no XDG_RUNTIME_DIR) we fall
    /// back to ~/Library/Application Support/pet-talk and force 0700 ourselves —
    /// world-writable /tmp is never an acceptable home for PID/pause state.
    static let runtimeDir: String = {
        let dir: String
        if let xdg = ProcessInfo.processInfo.environment["XDG_RUNTIME_DIR"], !xdg.isEmpty {
            dir = xdg
        } else {
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            dir = (home as NSString).appendingPathComponent("Library/Application Support/pet-talk")
        }
        if !FileManager.default.fileExists(atPath: dir) {
            try? FileManager.default.createDirectory(
                atPath: dir, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        } else {
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir)
        }
        return dir
    }()

    static var pidFile: String { (runtimeDir as NSString).appendingPathComponent("pet-talk-hotkey.pid") }
    static var pauseFile: String { (runtimeDir as NSString).appendingPathComponent("pet-talk-hotkey.paused") }
    static var logFile: String { (runtimeDir as NSString).appendingPathComponent("pet-talk-hotkey.log") }
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

    // MARK: - Owned-child tracking (the sub-10ms kill path)
    //
    // Any process this daemon spawns directly via Process().run() (currently:
    // the `pet-talk-cli` invocation) is registered here by PID at spawn time.
    // Killing a tracked PID needs no verification — we already proved ownership
    // by holding the PID Foundation handed us — so it's a bare kill(2), same
    // latency as the old code's direct SIGKILL.
    private static var ownedChildPIDs: Set<pid_t> = []
    private static let ownedChildPIDsLock = NSLock()

    static func registerOwnedChild(_ pid: pid_t) {
        ownedChildPIDsLock.lock()
        ownedChildPIDs.insert(pid)
        ownedChildPIDsLock.unlock()
    }

    static func unregisterOwnedChild(_ pid: pid_t) {
        ownedChildPIDsLock.lock()
        ownedChildPIDs.remove(pid)
        ownedChildPIDsLock.unlock()
    }

    /// Sub-10ms kill of every tracked owned child: no ownership check needed,
    /// we already hold these PIDs from our own Process.run() calls. Straight
    /// kill(2), matching the old barge-in engine's <2ms budget.
    @discardableResult
    static func killOwnedChildren() -> (killedCount: Int, elapsedMs: Double) {
        let t0 = DispatchTime.now()
        ownedChildPIDsLock.lock()
        let pids = ownedChildPIDs
        ownedChildPIDsLock.unlock()

        var count = 0
        for pid in pids where isProcessAlive(pid: pid) {
            kill(pid, SIGKILL)
            count += 1
        }

        ownedChildPIDsLock.lock()
        ownedChildPIDs.removeAll()
        ownedChildPIDsLock.unlock()

        let t1 = DispatchTime.now()
        let ms = Double(t1.uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000.0
        return (count, ms)
    }

    static func hasAfplayRunning() -> Bool {
        return !pidsNamed("afplay", exactMatch: true).isEmpty
    }

    /// List every live PID whose short process name matches `targetName`.
    /// Exact match by default — the old `name.contains(targetName)` substring
    /// check is exactly what let the kill switch reap unrelated processes that
    /// merely shared a name fragment.
    private static func pidsNamed(_ targetName: String, exactMatch: Bool) -> [pid_t] {
        var matches: [pid_t] = []
        let numPids = proc_listpids(UInt32(PROC_ALL_PIDS), 0, nil, 0)
        guard numPids > 0 else { return matches }
        var pids = [pid_t](repeating: 0, count: Int(numPids) / MemoryLayout<pid_t>.size)
        proc_listpids(UInt32(PROC_ALL_PIDS), 0, &pids, numPids)
        var nameBuf = [CChar](repeating: 0, count: 256)
        for pid in pids where pid > 0 {
            let ret = proc_name(pid, &nameBuf, UInt32(nameBuf.count))
            guard ret > 0 else { continue }
            let name = String(cString: nameBuf)
            if exactMatch ? (name == targetName) : name.contains(targetName) {
                matches.append(pid)
            }
        }
        return matches
    }

    /// uid that owns `pid`, via the public `proc_pidinfo(PROC_PIDTBSDINFO)` call
    /// (needs no special entitlement, unlike raw kinfo_proc/sysctl).
    static func processUID(pid: pid_t) -> uid_t? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.stride)
        let ret = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size)
        guard ret == size else { return nil }
        return info.pbi_uid
    }

    static func executablePath(pid: pid_t) -> String? {
        // 4 * MAXPATHLEN (1024) — PROC_PIDPATHINFO_MAXSIZE's value, but the macro
        // itself doesn't import into Swift (arithmetic #define).
        var buf = [Int8](repeating: 0, count: 4096)
        let ret = proc_pidpath(pid, &buf, UInt32(buf.count))
        guard ret > 0 else { return nil }
        return String(cString: buf)
    }

    /// Repo root: walk up from this executable's own directory to the nearest
    /// ancestor containing AGENTS.md. Used only to scope which "stale" (untracked)
    /// executables are safe to kill — never to match by name.
    static func repoRoot() -> String? {
        let binaryPath = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
        var dir = URL(fileURLWithPath: binaryPath).deletingLastPathComponent()
        for _ in 0..<12 {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("AGENTS.md").path) {
                return dir.path
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return nil
    }

    /// Verify a PID we did NOT spawn ourselves (a stale PID-file entry, or an
    /// afplay/pet-talk-cli process left over from a prior run) is safe to kill:
    /// owned by our own uid, AND either it's `afplay` (the only external playback
    /// process this project ever starts) or its executable lives under our repo
    /// root. Returns a refusal reason on failure, nil when verified safe.
    static func verifyStalePidKillable(pid: pid_t, expectedName: String? = nil) -> String? {
        guard pid > 0 else { return "invalid_pid" }
        guard isProcessAlive(pid: pid) else { return "process_not_alive" }
        guard let uid = processUID(pid: pid) else { return "uid_lookup_failed" }
        guard uid == getuid() else { return "not_owned_by_current_uid" }

        var nameBuf = [CChar](repeating: 0, count: 256)
        let nameRet = proc_name(pid, &nameBuf, UInt32(nameBuf.count))
        let name = nameRet > 0 ? String(cString: nameBuf) : nil

        if let expected = expectedName, name != expected {
            return "name_mismatch:\(name ?? "?")"
        }

        if name == "afplay" {
            return nil // uid-verified afplay: the only playback process we ever spawn
        }

        guard let path = executablePath(pid: pid) else { return "executable_path_unavailable" }
        guard let repo = repoRoot() else { return "repo_root_unresolved" }
        if path == repo || path.hasPrefix(repo + "/") {
            return nil
        }
        return "executable_outside_repo:\(path)"
    }

    /// SIGTERM, wait up to 50ms for exit, then SIGKILL — the escalation path for
    /// any PID that isn't one of our directly-tracked owned children.
    @discardableResult
    static func killWithEscalation(pid: pid_t) -> Bool {
        guard isProcessAlive(pid: pid) else { return true }
        kill(pid, SIGTERM)
        let deadline = DispatchTime.now() + .milliseconds(50)
        while DispatchTime.now() < deadline {
            if !isProcessAlive(pid: pid) { return true }
            usleep(2_000)
        }
        if isProcessAlive(pid: pid) {
            kill(pid, SIGKILL)
        }
        return !isProcessAlive(pid: pid)
    }

    /// Find every stale (untracked) process named `targetName`, verify each one,
    /// and kill only the verified ones. Refusals are logged with their reason
    /// instead of silently skipped or silently killed.
    @discardableResult
    static func killVerifiedStaleProcessesNamed(_ targetName: String, log: (String) -> Void) -> Int {
        var count = 0
        for pid in pidsNamed(targetName, exactMatch: true) {
            if let reason = verifyStalePidKillable(pid: pid, expectedName: targetName) {
                log("! [kill-refused] pid \(pid) (\(targetName)): \(reason)")
                continue
            }
            if killWithEscalation(pid: pid) {
                count += 1
            } else {
                log("! [kill-failed] pid \(pid) (\(targetName)) survived SIGTERM+SIGKILL escalation")
            }
        }
        return count
    }

    /// Kill every verified afplay process (uid-owned by us). This is the
    /// barge-in audio cutoff: afplay runs as a grandchild of `pet-talk-cli`, so
    /// it is never one of our directly-tracked owned children, but it is still
    /// safe to kill once uid-verified. Budget: <= 50ms (SIGTERM + escalation).
    @discardableResult
    static func killAfplayVerified(log: (String) -> Void) -> (killedCount: Int, elapsedMs: Double) {
        let t0 = DispatchTime.now()
        let count = killVerifiedStaleProcessesNamed("afplay", log: log)
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
    private var handoverHotKeyRef: EventHotKeyRef?
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

    /// Named failure for CLI path resolution — law #1 (fail closed with named
    /// reasons, no silent fallbacks). No hardcoded absolute path anywhere here:
    /// resolution order is `--cli` flag > env `PET_TALK_CLI_PATH` > config.yaml
    /// `cli_path` > sibling of the running executable > `<repo>/bin/pet-talk-cli`
    /// found by walking up from the executable to a directory containing
    /// AGENTS.md.
    enum CliPathResolutionError: Error, CustomStringConvertible {
        case notFound(checked: [String])
        var description: String {
            switch self {
            case .notFound(let checked):
                return "cli_path_unresolved: no executable pet-talk-cli found. Checked: \(checked.joined(separator: " | "))"
            }
        }
    }

    func resolveCliPath() throws -> String {
        if let overridePath = cliOverridePath, FileManager.default.isExecutableFile(atPath: overridePath) {
            return overridePath
        }

        var checked: [String] = []
        if let overridePath = cliOverridePath {
            checked.append("--cli:\(overridePath)")
        }

        // 1. env PET_TALK_CLI_PATH
        if let envPath = ProcessInfo.processInfo.environment["PET_TALK_CLI_PATH"], !envPath.isEmpty {
            checked.append("env:PET_TALK_CLI_PATH=\(envPath)")
            if FileManager.default.isExecutableFile(atPath: envPath) {
                return envPath
            }
        }

        // 2. config.yaml cli_path
        let config = PetTalkConfig.load()
        if let configPath = config.cliPath, !configPath.isEmpty {
            checked.append("config:cli_path=\(configPath)")
            if FileManager.default.isExecutableFile(atPath: configPath) {
                return configPath
            }
        }

        // 3. sibling of the running executable
        let binaryPath = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
        let binDir = URL(fileURLWithPath: binaryPath).deletingLastPathComponent()
        let sibling = binDir.appendingPathComponent("pet-talk-cli").path
        checked.append("sibling:\(sibling)")
        if FileManager.default.isExecutableFile(atPath: sibling) {
            return sibling
        }

        // 4. walk up from the executable to the nearest ancestor containing
        // AGENTS.md, then <that dir>/bin/pet-talk-cli
        var dir = binDir
        for _ in 0..<12 {
            let agentsPath = dir.appendingPathComponent("AGENTS.md").path
            if FileManager.default.fileExists(atPath: agentsPath) {
                let repoBin = dir.appendingPathComponent("bin/pet-talk-cli").path
                checked.append("repo-root:\(repoBin)")
                if FileManager.default.isExecutableFile(atPath: repoBin) {
                    return repoBin
                }
                break
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }

        throw CliPathResolutionError.notFound(checked: checked)
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

        // Sub-10ms path: kill(2) on the PID(s) we spawned ourselves and hold —
        // no verification needed, we already proved ownership at spawn time.
        let (killedOwned, ownedMs) = ProcessManager.killOwnedChildren()
        if killedOwned > 0 {
            log("| [kill] Killed \(killedOwned) owned child process(es) in \(String(format: "%.2f", ownedMs))ms")
        }
        if let proc = activeCliProc {
            activeCliProc = nil
            if proc.isRunning {
                log("| [kill] pet-talk-cli process reaped")
            }
        }

        // Verified path: afplay runs as a grandchild (spawned by pet-talk-cli,
        // not by us), so it can never be a tracked owned child. Kill only after
        // confirming it's ours (uid match) — refusals are logged, never silent.
        let (killedAfplay, bargeMs) = ProcessManager.killAfplayVerified(log: { [weak self] line in self?.log(line) })
        if killedAfplay > 0 {
            log("| [kill] Killed \(killedAfplay) verified afplay process(es) in \(String(format: "%.2f", bargeMs))ms")
        }

        EarconEngine.shared.playBargeKill()
        HUDController.shared.dismiss(hardCut: true)
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
                detail: "Paused — double-tap Option+Tab to wake",
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

    func handleHandoverHotKeyTrigger() {
        log("+-- [HOTKEY] Option+Shift+Tab hand-over triggered")
        emitHandover()
    }

    /// Hand over the current context to the agent. Emitted exactly the way a wake
    /// turn is emitted today — spawn `pet-talk-cli` with the event's own
    /// subcommand — so the daemon stays a keyboard/HUD process with no socket of
    /// its own. The CLI turns it into one WS frame:
    ///
    ///     {"type": "user.handover", "turn_id": "<uuid4>", "source": "hotkey"}
    ///
    /// A CLI that does not know the subcommand exits non-zero: that is reported as
    /// a named reason and an error shake, never swallowed into a silent no-op.
    func emitHandover() {
        if ProcessManager.isPaused() {
            log("| [paused] Hand-over ignored — Donna is paused")
            EarconEngine.shared.playError()
            HUDController.shared.showBreadcrumb(
                badge: "PAUSED",
                detail: "Paused — double-tap Option+Tab to wake",
                state: .thinking
            )
            return
        }

        let cliPath: String
        do {
            cliPath = try resolveCliPath()
        } catch {
            log("! [error] handover_cli_unresolved: \(error)")
            EarconEngine.shared.playError()
            HUDController.shared.triggerErrorShake()
            return
        }

        EarconEngine.shared.playMicOpen()
        HUDController.shared.showBreadcrumb(
            badge: "Hand-over",
            detail: "Handing the floor to the agent",
            state: .thinking
        )

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cliPath)
        proc.arguments = ["handover"]

        let errPipe = Pipe()
        proc.standardError = errPipe

        do {
            try proc.run()
            ProcessManager.registerOwnedChild(proc.processIdentifier)
            proc.terminationHandler = { [weak self] finished in
                ProcessManager.unregisterOwnedChild(finished.processIdentifier)
                guard finished.terminationStatus != 0 else {
                    self?.log("| [handover] emitted (user.handover)")
                    return
                }
                let detail = String(
                    data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                    encoding: .utf8
                ) ?? ""
                self?.log("! [error] handover_emit_failed (exit \(finished.terminationStatus)): \(detail)")
                EarconEngine.shared.playError()
                HUDController.shared.triggerErrorShake()
            }
        } catch {
            log("! [error] handover_spawn_failed: \(error)")
            EarconEngine.shared.playError()
            HUDController.shared.triggerErrorShake()
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
            if !self.isTurnActive() {
                HUDController.shared.dismiss()
            }
        }
    }

    func handleHotKeyTrigger() {
        let now = DispatchTime.now()
        if let last = lastTapTime {
            let intervalMs = Double(now.uptimeNanoseconds - last.uptimeNanoseconds) / 1_000_000.0
            if intervalMs <= HUDTokens.doubleTapWindowMs {
                lastTapTime = nil
                log("+-- [HOTKEY] Double-tap detected (\(String(format: "%.1f", intervalMs))ms, window \(Int(HUDTokens.doubleTapWindowMs))ms) -> toggling pause mode")
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
                detail: "Paused — double-tap Option+Tab to wake",
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
        let cliPath: String
        do {
            cliPath = try resolveCliPath()
        } catch {
            log("! [error] \(error)")
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
            ProcessManager.registerOwnedChild(proc.processIdentifier)
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
                ProcessManager.unregisterOwnedChild(process.processIdentifier)
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

        var handoverTestRef: EventHotKeyRef?
        let handoverTestID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: 998)
        let handoverStatus = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.handoverHotKeyModifier,
            handoverTestID,
            GetApplicationEventTarget(),
            0,
            &handoverTestRef
        )
        guard handoverStatus == noErr else { return false }
        if let hRef = handoverTestRef {
            UnregisterEventHotKey(hRef)
        }

        return true
    }

    func startListening() -> Int32 {
        _ = NSApplication.shared
        NSApplication.shared.setActivationPolicy(.accessory)

        // Pre-load configuration and acoustic earcons
        let config = PetTalkConfig.load()
        EarconEngine.shared.logSink = { [weak self] line in self?.log(line) }
        EarconEngine.shared.configure(from: config)
        EarconEngine.shared.prewarm()

        // Resolve paste injection settings
        if let explicit = explicitPasteFlag {
            self.pasteEnabled = explicit
        } else {
            self.pasteEnabled = config.paste.enabled
        }
        self.pasteConfig = config.paste

        let cliPath: String
        do {
            cliPath = try resolveCliPath()
        } catch {
            log("! [warning] \(error) (will re-resolve at turn start)")
            cliPath = "(unresolved)"
        }
        ProcessManager.writePid(getpid())

        log("+-- pet-talk-hotkey daemon active")
        log("| PID: \(getpid())")
        log("| Hotkey: Option + Tab (keycode: \(HotkeyConfig.hotKeyCode), mod: 0x\(String(HotkeyConfig.hotKeyModifier, radix: 16, uppercase: true)))")
        log("| Hand-over Hotkey: Option + Shift + Tab (keycode: \(HotkeyConfig.hotKeyCode), mod: 0x\(String(HotkeyConfig.handoverHotKeyModifier, radix: 16, uppercase: true)))")
        log("| Pause: double-tap Option + Tab within \(Int(HUDTokens.doubleTapWindowMs))ms")
        log("| Target CLI: \(cliPath)")
        log("| Earcons: enabled=\(EarconEngine.shared.isEnabled), pack=\(EarconEngine.shared.soundPack), vol=\(String(format: "%.2f", EarconEngine.shared.volume))")
        log("| HUD: Obsidian Deep Zinc Capsule (\(Int(HUDCapsuleView.capsuleWidth))pt measured notch -> \(Int(HUDCapsuleView.expandedWidth))pt expanded)")
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
            if status == noErr && hotKeyID.id == HotkeyConfig.handoverHotKeyId {
                HotkeyListener.shared.handleHandoverHotKeyTrigger()
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

        // 3. Register Global Hand-over Hotkey (kVK_Tab + optionKey + shiftKey) on Event Dispatcher Target
        let handoverHotKeyID = EventHotKeyID(signature: HotkeyConfig.hotKeySignature, id: HotkeyConfig.handoverHotKeyId)
        let handoverRegStatus = RegisterEventHotKey(
            HotkeyConfig.hotKeyCode,
            HotkeyConfig.handoverHotKeyModifier,
            handoverHotKeyID,
            GetEventDispatcherTarget(),
            0,
            &handoverHotKeyRef
        )

        if handoverRegStatus != noErr {
            log("! [warning] Could not register the Option+Shift+Tab hand-over chord (status: \(handoverRegStatus)) — hand-over is unavailable this session")
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
        if let hRef = handoverHotKeyRef {
            UnregisterEventHotKey(hRef)
            handoverHotKeyRef = nil
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
      pet-talk-hotkey ax                  Print AX grounding JSON (app/window/selection/path), exit 0
      pet-talk-hotkey ax --request-permission  Also trigger the AX permission prompt first
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
      pet-talk-hotkey --self-test         Headless spring + geometry checks (needs PET_TALK_HEADLESS=1)
      pet-talk-hotkey --dump-state        Dump one JSON line: state machine, geometry, springs, chords
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

    Gestures:
      Ask:        Option+Tab (kVK_Tab 48 + optionKey 0x0800)
      Kill:       Single-tap Option+Tab while active cuts audio in <2ms & dismisses HUD
      Pause:      Double-tap Option+Tab within 400ms toggles Sleep Mode (the ONLY pause gesture)
      Hand-over:  Option+Shift+Tab (0x0A00) hands the floor to the agent (user.handover)
      Barge-in:   <= 50ms afplay instant kill via Darwin libproc; the HUD is a hard cut
      Dictation:  Live transcribed speech displayed in Obsidian Zinc Capsule (380x44px)
      Paste:      NSPasteboard + CGEvent Cmd+V into Cursor / terminal / editor
      Earcons:    Pre-loaded NSSound in RAM (<2ms latency)
                  - Mic Open:       Tink.aiff (24ms)
                  - Silence Cutoff: Pop.aiff (32ms)
                  - Barge Kill:     Bottle.aiff (18ms)
                  - Error:          Basso.aiff (45ms)
      HUD:        Measured-notch-wide Obsidian Glass NSPanel [.nonactivatingPanel],
                  180pt fallback pill on a screen with no notch, real damped spring
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

/// A PID file surviving a reboot can point at a recycled PID that now belongs
/// to a completely different process. Before signalling anything from a PID
/// file, verify it's actually our own daemon: owned by our uid, executable
/// name is `pet-talk-hotkey`.
func verifyPidIsHotkeyDaemon(_ pid: pid_t) -> Bool {
    guard ProcessManager.verifyStalePidKillable(pid: pid, expectedName: "pet-talk-hotkey") == nil else {
        return false
    }
    return true
}

func doStop() -> Int32 {
    guard let pid = ProcessManager.readPid() else {
        ProcessManager.setPaused(false)
        print("pet-talk-hotkey is not running")
        return 0
    }

    guard ProcessManager.isProcessAlive(pid: pid) else {
        ProcessManager.setPaused(false)
        ProcessManager.removePidFile()
        print("pet-talk-hotkey is not running (cleaned stale PID file)")
        return 0
    }

    guard verifyPidIsHotkeyDaemon(pid) else {
        print("!-- Refusing to signal pid \(pid): does not verify as pet-talk-hotkey (stale/recycled PID file)")
        ProcessManager.setPaused(false)
        ProcessManager.removePidFile()
        return 1
    }

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
    _ = ProcessManager.killAfplayVerified(log: { print($0) })
    _ = ProcessManager.killVerifiedStaleProcessesNamed("pet-talk-cli", log: { print($0) })
    ProcessManager.setPaused(false)
    ProcessManager.removePidFile()
    print("pet-talk-hotkey daemon stopped (PID: \(pid))")
    return 0
}

func doKill() -> Int32 {
    print("+-- Executing Pet-Talk instant kill switch...")
    let (killedAfplay, bargeMs) = ProcessManager.killAfplayVerified(log: { print($0) })
    if killedAfplay > 0 {
        print("| Killed \(killedAfplay) afplay process(es) in \(String(format: "%.2f", bargeMs))ms")
    }
    let killedCli = ProcessManager.killVerifiedStaleProcessesNamed("pet-talk-cli", log: { print($0) })
    if killedCli > 0 {
        print("| Killed \(killedCli) pet-talk-cli process(es)")
    }

    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid), verifyPidIsHotkeyDaemon(pid) {
        kill(pid, SIGUSR2)
    }

    EarconEngine.shared.playBargeKill()
    print("+-- PASS: Instant kill switch executed (audio stopped, HUD dismissed).")
    return 0
}

func doPause() -> Int32 {
    _ = ProcessManager.killAfplayVerified(log: { print($0) })
    _ = ProcessManager.killVerifiedStaleProcessesNamed("pet-talk-cli", log: { print($0) })
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid), verifyPidIsHotkeyDaemon(pid) {
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
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid), verifyPidIsHotkeyDaemon(pid) {
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
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid), verifyPidIsHotkeyDaemon(pid) {
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

/// `pet-talk-hotkey ax` — AX grounding for lane E/A per docs/WAVE3.md: one JSON
/// line on stdout, exit 0 always, finishes well under the 300ms subprocess
/// timeout the server enforces (typically <10ms of AX calls).
///
/// Never calls the *prompting* trust-check variant unless explicitly invoked as
/// `ax --request-permission` — a bare `ax` call must never pop a system dialog.
func doAX(args: [String]) -> Int32 {
    if args.contains("--request-permission") {
        let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([promptKey: true] as CFDictionary)
    }

    guard AXIsProcessTrusted() else {
        print("{\"ok\":false,\"reason\":\"ax_permission_denied\"}")
        return 0
    }

    guard let frontApp = NSWorkspace.shared.frontmostApplication else {
        print("{\"ok\":false,\"reason\":\"ax_unavailable\"}")
        return 0
    }

    let appName = frontApp.localizedName ?? "unknown"
    let axApp = AXUIElementCreateApplication(frontApp.processIdentifier)

    func copyAttr(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
        return result == .success ? value : nil
    }

    /// `as? AXUIElement` always "succeeds" (CFTypeRef toll-free bridging), so the
    /// only real conditional cast is a CFTypeID check — this is what replaces the
    /// force cast that used to crash the daemon when the AX API handed back some
    /// other CF type.
    func asAXUIElement(_ ref: CFTypeRef?) -> AXUIElement? {
        guard let ref = ref, CFGetTypeID(ref) == AXUIElementGetTypeID() else { return nil }
        return (ref as! AXUIElement)
    }

    var windowTitle: String?
    var selection: String?
    var path: String?

    if let focusedRef = copyAttr(axApp, kAXFocusedUIElementAttribute as String) {
        guard let focused = asAXUIElement(focusedRef) else {
            print("{\"ok\":false,\"reason\":\"ax_unavailable\"}")
            return 0
        }
        selection = copyAttr(focused, kAXSelectedTextAttribute as String) as? String
        path = copyAttr(focused, kAXDocumentAttribute as String) as? String
        windowTitle = copyAttr(focused, kAXTitleAttribute as String) as? String
    }

    if windowTitle == nil, let windowRef = copyAttr(axApp, kAXFocusedWindowAttribute as String) {
        guard let window = asAXUIElement(windowRef) else {
            print("{\"ok\":false,\"reason\":\"ax_unavailable\"}")
            return 0
        }
        windowTitle = copyAttr(window, kAXTitleAttribute as String) as? String
        if path == nil {
            path = copyAttr(window, kAXDocumentAttribute as String) as? String
        }
    }

    let payload: [String: Any] = [
        "ok": true,
        "app": appName,
        "window": windowTitle ?? "",
        "selection": (selection?.isEmpty == false) ? selection! : NSNull(),
        "path": (path?.isEmpty == false) ? path! : NSNull()
    ]

    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
          let json = String(data: data, encoding: .utf8) else {
        print("{\"ok\":false,\"reason\":\"ax_unavailable\"}")
        return 0
    }
    print(json)
    return 0
}

func doVerify() -> Int32 {
    print("+-- Verifying Carbon Option+Tab registration...")
    print("| Keycode:  \(HotkeyConfig.hotKeyCode) (kVK_Tab)")
    print("| Modifier: 0x\(String(HotkeyConfig.hotKeyModifier, radix: 16, uppercase: true)) (optionKey / 2048) -> ask")
    print("| Hand-over modifier: 0x\(String(HotkeyConfig.handoverHotKeyModifier, radix: 16, uppercase: true)) (optionKey|shiftKey / 2560) -> handover")
    print("| Pause: double-tap Option+Tab within \(Int(HUDTokens.doubleTapWindowMs))ms")
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
    let (count, ms) = ProcessManager.killAfplayVerified(log: { print($0) })
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

/// `pet-talk-hotkey --self-test` — headless verification of the spring
/// integrator and the geometry math. Orders NO window front (and refuses to run
/// unless PET_TALK_HEADLESS=1, so it can never pop the capsule onto a screen
/// someone is working on), plays no audio, starts no daemon.
func doSelfTest() -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    guard HUDController.isHeadless else {
        print("!-- FAIL: --self-test requires PET_TALK_HEADLESS=1 (it must never show a window)")
        return 1
    }

    var failures: [String] = []
    func check(_ label: String, _ condition: Bool, _ detail: String = "") {
        if condition {
            print("| PASS: \(label)\(detail.isEmpty ? "" : " — \(detail)")")
        } else {
            print("!-- FAIL: \(label)\(detail.isEmpty ? "" : " — \(detail)")")
            failures.append(label)
        }
    }

    print("+-- pet-talk-hotkey --self-test (headless: no window, no audio, no daemon)")

    // 1. Spring integrator: the three declared tokens must produce real, settling,
    //    underdamped motion — not a no-op and not a divergence.
    print("| [spring] stiffness=\(HUDTokens.springStiffness) damping=\(HUDTokens.springDamping) mass=\(HUDTokens.springMass)")
    var samples: [Double] = []
    var finished = false
    let started = DispatchTime.now()
    let driver = HUDSpringDriver(onStep: { p in samples.append(p) }, onDone: { finished = true })
    driver.start()

    let deadline = Date().addingTimeInterval(3.0)
    while !finished && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
    }
    let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - started.uptimeNanoseconds) / 1_000_000.0

    check("spring settles", finished, "\(String(format: "%.1f", elapsedMs))ms, \(samples.count) steps")
    check("spring integrates many steps", samples.count > 10, "\(samples.count) steps")
    check("spring starts at rest", samples.first == 0.0, "first=\(samples.first ?? -1)")
    check("spring lands exactly on target", samples.last == 1.0, "last=\(samples.last ?? -1)")
    // The spring's PERCEPTUAL duration — first time it reaches 99% of travel — is
    // what the dripDuration token budgets. The settling tail after that is
    // sub-pixel.
    let perceptualSteps = samples.firstIndex(where: { $0 >= 0.99 }) ?? samples.count
    let perceptualMs = Double(perceptualSteps) * (1000.0 / 120.0)
    check("spring lands inside the drip budget",
          perceptualMs <= HUDMotionTokens.dripDuration * 1000.0 * 1.5,
          "\(String(format: "%.1f", perceptualMs))ms perceptual vs \(Int(HUDMotionTokens.dripDuration * 1000.0))ms budget")

    let overshoot = samples.max() ?? 0.0
    check("spring is underdamped (overshoots)", overshoot > 1.0, "peak=\(String(format: "%.4f", overshoot))")
    check("spring stays bounded", overshoot < 1.5, "peak=\(String(format: "%.4f", overshoot))")
    check("spring never undershoots below rest", (samples.min() ?? 0.0) >= 0.0)
    check("CASpringAnimation settling is positive", HUDSpring.settlingDuration > 0.0,
          "\(String(format: "%.1f", HUDSpring.settlingDuration * 1000.0))ms")
    let springAnim = HUDSpring.animation(keyPath: "opacity", from: 0.0, to: 1.0)
    check("CASpringAnimation reads the tokens",
          Double(springAnim.stiffness) == HUDTokens.springStiffness
            && Double(springAnim.damping) == HUDTokens.springDamping
            && Double(springAnim.mass) == HUDTokens.springMass
            && springAnim.initialVelocity == 0.0)

    // 2. Geometry: measured notch width, fallback pill, ear fillet threshold,
    //    multi-line height stepping, and top-edge anchoring.
    let measured = HUDCapsuleView.measuredNotchWidth()
    let width = HUDCapsuleView.capsuleWidth
    print("| [geometry] measuredNotchWidth=\(measured.map { String(format: "%.1f", $0) } ?? "none") capsuleWidth=\(String(format: "%.1f", width))")
    if let measured = measured {
        check("capsule width is the measured notch", width == measured)
    } else {
        check("capsule width falls back to the pill token", width == HUDTokens.fallbackCapsuleWidth,
              "\(HUDTokens.fallbackCapsuleWidth)pt")
    }

    let notchW: CGFloat = measured ?? HUDTokens.fallbackCapsuleWidth
    check("no ears at the notch width",
          HUDCapsuleView.earFilletRadius(forWidth: notchW, notchWidth: notchW) == 0.0)
    check("no ears at exactly notch + threshold",
          HUDCapsuleView.earFilletRadius(forWidth: notchW + HUDTokens.earFilletThreshold, notchWidth: notchW) == 0.0)
    check("ears once content clears notch + threshold",
          HUDCapsuleView.earFilletRadius(forWidth: notchW + HUDTokens.earFilletThreshold + 1.0, notchWidth: notchW)
            == HUDTokens.earFilletRadius)

    check("one-line height", HUDCapsuleView.computeDynamicNotchHeight(for: 20.0, hasNotch: true) == HUDCapsuleView.notchExpandedHeight)
    check("two-line height", HUDCapsuleView.computeDynamicNotchHeight(for: 40.0, hasNotch: true) == 76.0)
    check("three-line height", HUDCapsuleView.computeDynamicNotchHeight(for: 60.0, hasNotch: true) == 96.0)
    check("four-line clamp", HUDCapsuleView.computeDynamicNotchHeight(for: 400.0, hasNotch: true) == HUDCapsuleView.maxExpandedHeight)

    let frame = HUDController.shared.computeFrame(width: width, height: HUDCapsuleView.notchListeningHeight)
    check("frame keeps the requested size",
          frame.width == width && frame.height == HUDCapsuleView.notchListeningHeight)
    let screen = NSScreen.main ?? (NSScreen.screens.first ?? NSScreen())
    let notchInfo = NotchManager.shared.currentNotch(for: screen)
    if notchInfo.hasNotch {
        check("frame is flush with the top of the display", abs(frame.maxY - notchInfo.screenFrame.maxY) < 0.5)
        check("frame is centred on the notch", abs(frame.midX - notchInfo.rect.midX) < 0.5)
    } else {
        check("pill floats below the menu bar",
              abs(frame.maxY - (notchInfo.visibleFrame.maxY - 12.0)) < 0.5)
    }

    // 3. The HUD never became visible during any of this.
    check("no window was shown", !HUDController.shared.panel.isVisible)
    check("lifecycle untouched", HUDController.shared.lifecycleName == "hidden")

    if failures.isEmpty {
        print("+-- PASS: self-test (spring integrator + geometry math), headless")
        return 0
    }
    print("!-- FAIL: self-test — \(failures.count) check(s) failed: \(failures.joined(separator: ", "))")
    return 1
}

/// `pet-talk-hotkey --dump-state` — ONE line of JSON: the state machine, the
/// measured capsule geometry, the spring constants in force, and the chord map.
/// No daemon, no window, no audio: the read a test can make.
func doDumpState() -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    var dump = HUDController.shared.getStateDump()

    if var state = dump["state"] as? [String: Any] {
        state["paused"] = ProcessManager.isPaused()
        if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
            state["daemonPid"] = Int(pid)
        } else {
            state["daemonPid"] = 0
        }
        dump["state"] = state
    }

    dump["chords"] = [
        "optionTab": "ask",
        "optionTabDoubleTap": "pause",
        "optionShiftTab": "handover",
        "escape": "barge",
        "doubleTapWindowMs": HUDTokens.doubleTapWindowMs,
        "keyCode": Int(HotkeyConfig.hotKeyCode),
        "askModifier": Int(HotkeyConfig.hotKeyModifier),
        "handoverModifier": Int(HotkeyConfig.handoverHotKeyModifier)
    ] as [String: Any]

    // The contract the server lane implements. Documented here so it is readable
    // from the binary, not only from prose.
    dump["handoverEvent"] = [
        "type": "user.handover",
        "fields": ["type", "turn_id", "source"],
        "source": "hotkey",
        "transport": "pet-talk-cli handover"
    ] as [String: Any]

    guard let data = try? JSONSerialization.data(withJSONObject: dump, options: [.sortedKeys]),
          let json = String(data: data, encoding: .utf8) else {
        print("{\"ok\":false,\"reason\":\"dump_state_serialize_failed\"}")
        return 1
    }
    print(json)
    return 0
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

// Spring/geometry/gesture tokens: a ~/.pet-talk/config.yaml override (or a
// design-playground export written into it) applies here, with no rebuild.
HUDTokens.apply(from: PetTalkConfig.load())

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
case "ax":
    exit(doAX(args: Array(args.dropFirst())))
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
case "--self-test", "self-test":
    exit(doSelfTest())
case "--dump-state", "dump-state":
    exit(doDumpState())
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
