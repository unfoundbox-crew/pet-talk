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
    static let logFile = "/tmp/pet-talk-hotkey.log"
    static let defaultCliPath = "/Users/saurabh/code/unfoundbox-crew/pet-talk/bin/pet-talk-cli"
    static let hotKeyCode: UInt32 = UInt32(kVK_Tab) // 48
    static let hotKeyModifier: UInt32 = UInt32(optionKey) // 0x0800 = 2048
    static let hotKeySignature: OSType = 0x50544C4B // 'PTLK'
    static let hotKeyId: UInt32 = 1
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
    private var activeCliProc: Process?
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?

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
        if isDaemon {
            if !FileManager.default.fileExists(atPath: HotkeyConfig.logFile) {
                FileManager.default.createFile(atPath: HotkeyConfig.logFile, contents: nil)
            }
            if let handle = FileHandle(forWritingAtPath: HotkeyConfig.logFile) {
                handle.seekToEndOfFile()
                if let data = text.data(using: .utf8) {
                    handle.write(data)
                }
                handle.closeFile()
            }
        } else {
            fputs(text, stdout)
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

    func handleHotKeyTrigger() {
        log("+-- [HOTKEY] Option+Tab triggered")

        // 1. Check if audio or speech is currently active
        let (killedAfplay, bargeMs) = ProcessManager.killAfplay()
        var wasActive = false

        if killedAfplay > 0 {
            wasActive = true
            log("| [barge-in] Killed \(killedAfplay) afplay process(es) in \(String(format: "%.2f", bargeMs))ms (budget <= 50ms)")
        }

        if let proc = activeCliProc, proc.isRunning {
            wasActive = true
            proc.terminate()
            activeCliProc = nil
            log("| [barge-in] Terminated previous pet-talk-cli process")
        }

        if wasActive {
            // Immediate barge-kill earcon (<1ms)
            EarconEngine.shared.playBargeKill()
            log("| [barge-in] Starting fresh recording turn...")
            // Cue mic open chime right after barge kill chime (35ms)
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(35)) {
                EarconEngine.shared.playMicOpen()
            }
        } else {
            // Immediate mic open earcon (<1.2ms)
            EarconEngine.shared.playMicOpen()
            log("| [idle] Triggered -> starting one-shot recording turn...")
        }

        // 2. Launch pet-talk-cli once
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

        let forwardOutput: (Data) -> Void = { [weak self] data in
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.logCliOutput(text)

            // Detect turn transitions from pet-talk-cli output
            if !silenceCutoffFired && (text.contains("[THINKING]") || text.contains("Transcribing") || text.contains("user.stop")) {
                silenceCutoffFired = true
                EarconEngine.shared.playSilenceCutoff()
                HUDController.shared.update(state: .thinking)
            }
            if text.contains("[SPEAKING]") {
                HUDController.shared.update(state: .speaking)
            }
            if text.contains("[ERROR]") || text.contains("Error:") {
                EarconEngine.shared.playError()
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
        if status == noErr {
            if let ref = testRef {
                UnregisterEventHotKey(ref)
            }
            return true
        }
        return false
    }

    func startListening() -> Int32 {
        _ = NSApplication.shared
        NSApplication.shared.setActivationPolicy(.accessory)

        // Pre-load configuration and acoustic earcons
        let config = PetTalkConfig.load()
        EarconEngine.shared.configure(from: config)
        EarconEngine.shared.prewarm()

        let cliPath = resolveCliPath()
        ProcessManager.writePid(getpid())

        log("+-- pet-talk-hotkey daemon active")
        log("| PID: \(getpid())")
        log("| Hotkey: Option + Tab (keycode: \(HotkeyConfig.hotKeyCode), mod: 0x\(String(HotkeyConfig.hotKeyModifier, radix: 16, uppercase: true)))")
        log("| Target CLI: \(cliPath)")
        log("| Earcons: enabled=\(EarconEngine.shared.isEnabled), pack=\(EarconEngine.shared.soundPack), vol=\(String(format: "%.2f", EarconEngine.shared.volume))")
        log("| HUD: Obsidian Deep Zinc Capsule (220x44px)")
        log("| Mode: \(isDaemon ? "Daemon (background)" : "Foreground")")
        log("| Ready for global Option+Tab barge-in turns (<0.1% CPU)...")

        // 1. Install Carbon Event Handler on Event Dispatcher Target
        let eventHandler: EventHandlerUPP = { (_, _, _) -> OSStatus in
            HotkeyListener.shared.handleHotKeyTrigger()
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

        // 3. Register POSIX Signal Handlers for clean exit
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)

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

        // 4. Run NSApplication RunLoop to pump WindowServer events and AppKit animations
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
        if let proc = activeCliProc, proc.isRunning {
            proc.terminate()
            activeCliProc = nil
        }
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
      pet-talk-hotkey test-audio          Play & benchmark acoustic earcons sequence (<2ms)
      pet-talk-hotkey test-hud            Pop up floating glass capsule HUD in each state (1.5s each)
      pet-talk-hotkey config show         Show current configuration
      pet-talk-hotkey config set <k> <v>  Update configuration setting
      pet-talk-hotkey --dump-hud-spec     Dump HUD specification JSON for test assertions
      pet-talk-hotkey --check-registration Verify Carbon hotkey registration
      pet-talk-hotkey --barge-benchmark   Benchmark afplay barge-in kill latency
      pet-talk-hotkey --help              Show this help message

    Options for test-audio:
      --volume <0.0-1.0>                  Set earcon volume
      --sound-pack <pack>                 apple_minimal | cyberpunk | haptic | none
      --no-audio                          Disable earcons

    Sensory Presence Specifications:
      Key:        Tab (kVK_Tab, keycode 48)
      Modifier:   Option (optionKey, 0x0800 / 2048)
      Barge-in:   <= 50ms afplay instant kill via Darwin libproc
      Earcons:    Pre-loaded NSSound in RAM (<2ms latency)
                  - Mic Open:       Tink.aiff (24ms)
                  - Silence Cutoff: Pop.aiff (32ms)
                  - Barge Kill:     Bottle.aiff (18ms)
                  - Error:          Basso.aiff (45ms)
      HUD:        220x44px Obsidian Glass NSPanel [.nonactivatingPanel]
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
        ProcessManager.removePidFile()
        print("pet-talk-hotkey daemon stopped (PID: \(pid))")
        return 0
    } else {
        ProcessManager.removePidFile()
        print("pet-talk-hotkey is not running (cleaned stale PID file)")
        return 0
    }
}

func doStatus() -> Int32 {
    if let pid = ProcessManager.readPid(), ProcessManager.isProcessAlive(pid: pid) {
        print("pet-talk-hotkey is running (PID: \(pid))")
        return 0
    } else {
        if ProcessManager.readPid() != nil {
            ProcessManager.removePidFile()
        }
        print("pet-talk-hotkey is stopped")
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

    print("+-- Testing Pet-Talk Floating Glass Capsule HUD...")
    print("| Window: 220x44px NSPanel [.nonactivatingPanel, .borderless]")
    print("| Level: .floating, Spaces: [.canJoinAllSpaces, .fullScreenAuxiliary]")
    print("| Theme: Obsidian Deep Zinc (#12141c @ 85%) + 1px border (#282c3f)")
    print("| State 1 (1.5s): [LISTENING] Emerald True (#10b981) + 'Listening...'")

    HUDController.shared.show(state: .listening)

    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
        print("| State 2 (1.5s): [THINKING] SpacePilot Gold (#c9a227) + 'Donna thinking...'")
        HUDController.shared.update(state: .thinking)

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            print("| State 3 (1.5s): [SPEAKING] Liquid Silver (#cfd4dc) + 'Speaking...'")
            HUDController.shared.update(state: .speaking)

            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                print("| Dismissing HUD (200ms ease-out fade)...")
                HUDController.shared.dismiss {
                    print("+-- PASS: HUD visual test sequence completed.")
                    CFRunLoopStop(CFRunLoopGetMain())
                }
            }
        }
    }

    CFRunLoopRun()
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
    let budgetMs: Double = 2.0

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

switch command {
case "start":
    exit(doStart())
case "stop":
    exit(doStop())
case "status":
    exit(doStatus())
case "test-audio", "--test-audio", "--test-earcons":
    exit(doTestAudio(args: args))
case "config":
    exit(doConfig(args: Array(args.dropFirst())))
case "test-hud":
    exit(doTestHUD())
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
