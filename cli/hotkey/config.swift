// cli/hotkey/config.swift
// Configuration support for Pet-Talk sensory presence layer.
//
// Reads ~/.pet-talk/config.yaml or falls back gracefully to built-in defaults.
// Supports audio toggling, volume adjustment, sound pack selection, and custom sounds.

import Foundation

public struct AudioConfig {
    public var enabled: Bool = true
    public var volume: Float = 0.70
    public var soundPack: String = "apple_minimal"
    public var customSounds: [String: String] = [:]

    public init(
        enabled: Bool = true,
        volume: Float = 0.70,
        soundPack: String = "apple_minimal",
        customSounds: [String: String] = [:]
    ) {
        self.enabled = enabled
        self.volume = max(0.0, min(1.0, volume))
        self.soundPack = soundPack
        self.customSounds = customSounds
    }
}

public struct PasteConfig {
    public var enabled: Bool = false
    public var mode: String = "paste" // "paste" | "keystroke"
    public var restoreClipboard: Bool = false
    public var delayMs: Int = 30

    public init(
        enabled: Bool = false,
        mode: String = "paste",
        restoreClipboard: Bool = false,
        delayMs: Int = 30
    ) {
        self.enabled = enabled
        self.mode = mode
        self.restoreClipboard = restoreClipboard
        self.delayMs = max(5, delayMs)
    }
}

public struct PetTalkConfig {
    public var version: String = "1.0"
    public var audio: AudioConfig = AudioConfig()
    public var paste: PasteConfig = PasteConfig()

    public init(
        version: String = "1.0",
        audio: AudioConfig = AudioConfig(),
        paste: PasteConfig = PasteConfig()
    ) {
        self.version = version
        self.audio = audio
        self.paste = paste
    }

    public static var defaultConfigPath: String {
        if let envPath = ProcessInfo.processInfo.environment["PET_TALK_CONFIG_PATH"], !envPath.isEmpty {
            return envPath
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return (home as NSString).appendingPathComponent(".pet-talk/config.yaml")
    }

    public static func load(from path: String? = nil) -> PetTalkConfig {
        let targetPath = path ?? defaultConfigPath
        guard FileManager.default.fileExists(atPath: targetPath),
              let content = try? String(contentsOfFile: targetPath, encoding: .utf8) else {
            return PetTalkConfig()
        }
        return parseYAML(content)
    }

    public func save(to path: String? = nil) throws {
        let targetPath = path ?? PetTalkConfig.defaultConfigPath
        let dir = (targetPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: nil)
        let yaml = toYAML()
        try yaml.write(toFile: targetPath, atomically: true, encoding: .utf8)
    }

    public mutating func setAudioEnabled(_ enabled: Bool) {
        audio.enabled = enabled
    }

    public mutating func setAudioVolume(_ volume: Float) {
        audio.volume = max(0.0, min(1.0, volume))
    }

    public mutating func setSoundPack(_ pack: String) {
        audio.soundPack = pack
    }

    @discardableResult
    public mutating func setProperty(key: String, value: String) -> Bool {
        let cleanVal = value.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"\'"))
        switch key.lowercased() {
        case "audio.enabled":
            if let b = PetTalkConfig.parseBool(cleanVal) {
                audio.enabled = b
                return true
            }
        case "audio.volume":
            if let f = Float(cleanVal) {
                audio.volume = max(0.0, min(1.0, f))
                return true
            }
        case "audio.sound_pack", "audio.soundpack":
            audio.soundPack = cleanVal
            return true
        case "paste.enabled":
            if let b = PetTalkConfig.parseBool(cleanVal) {
                paste.enabled = b
                return true
            }
        case "paste.mode":
            paste.mode = cleanVal
            return true
        case "paste.restore_clipboard", "paste.restoreclipboard":
            if let b = PetTalkConfig.parseBool(cleanVal) {
                paste.restoreClipboard = b
                return true
            }
        case "paste.delay_ms", "paste.delay":
            if let i = Int(cleanVal) {
                paste.delayMs = max(5, i)
                return true
            }
        default:
            if key.lowercased().starts(with: "audio.custom_sounds.") {
                let soundKey = String(key.dropFirst("audio.custom_sounds.".count))
                audio.customSounds[soundKey] = cleanVal
                return true
            }
        }
        return false
    }

    public func toYAML() -> String {
        var lines: [String] = []
        lines.append("# Pet-Talk Sensory & Daemon Configuration")
        lines.append("version: \"\(version)\"")
        lines.append("")
        lines.append("# Auditory Feedback (Earcons)")
        lines.append("audio:")
        lines.append("  enabled: \(audio.enabled ? "true" : "false")")
        lines.append("  volume: \(String(format: "%.2f", audio.volume))")
        lines.append("  sound_pack: \"\(audio.soundPack)\"")
        if !audio.customSounds.isEmpty {
            lines.append("  custom_sounds:")
            for (k, v) in audio.customSounds.sorted(by: { $0.key < $1.key }) {
                lines.append("    \(k): \"\(v)\"")
            }
        } else {
            lines.append("  custom_sounds: {}")
        }
        lines.append("")
        lines.append("# Cursor Paste Injection (Wispr Flow style)")
        lines.append("paste:")
        lines.append("  enabled: \(paste.enabled ? "true" : "false")")
        lines.append("  mode: \"\(paste.mode)\"")
        lines.append("  restore_clipboard: \(paste.restoreClipboard ? "true" : "false")")
        lines.append("  delay_ms: \(paste.delayMs)")
        lines.append("")
        return lines.joined(separator: "\n")
    }

    public static func parseYAML(_ content: String) -> PetTalkConfig {
        var config = PetTalkConfig()
        let lines = content.components(separatedBy: .newlines)

        var currentSection: String? = nil
        var currentSubSection: String? = nil

        for rawLine in lines {
            // Strip comments
            let uncommented: String
            if let hashIdx = rawLine.firstIndex(of: "#") {
                uncommented = String(rawLine[..<hashIdx])
            } else {
                uncommented = rawLine
            }

            if uncommented.trimmingCharacters(in: .whitespaces).isEmpty {
                continue
            }

            let leadingSpaces = uncommented.prefix(while: { $0 == " " }).count
            let trimmed = uncommented.trimmingCharacters(in: .whitespaces)

            guard let colonIdx = trimmed.firstIndex(of: ":") else {
                continue
            }

            let key = String(trimmed[..<colonIdx]).trimmingCharacters(in: .whitespaces)
            let val = String(trimmed[trimmed.index(after: colonIdx)...]).trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"\'"))

            if leadingSpaces == 0 {
                if val.isEmpty {
                    currentSection = key
                    currentSubSection = nil
                } else {
                    currentSection = nil
                    currentSubSection = nil
                    if key == "version" {
                        config.version = val
                    }
                }
            } else if leadingSpaces >= 2 && leadingSpaces < 4 {
                if currentSection == "audio" {
                    if val.isEmpty {
                        currentSubSection = key
                    } else {
                        currentSubSection = nil
                        switch key {
                        case "enabled":
                            if let b = parseBool(val) { config.audio.enabled = b }
                        case "volume":
                            if let f = Float(val) { config.audio.volume = max(0.0, min(1.0, f)) }
                        case "sound_pack", "soundpack":
                            config.audio.soundPack = val
                        default:
                            break
                        }
                    }
                } else if currentSection == "paste" {
                    currentSubSection = nil
                    switch key {
                    case "enabled":
                        if let b = parseBool(val) { config.paste.enabled = b }
                    case "mode":
                        config.paste.mode = val
                    case "restore_clipboard", "restoreclipboard":
                        if let b = parseBool(val) { config.paste.restoreClipboard = b }
                    case "delay_ms", "delay":
                        if let i = Int(val) { config.paste.delayMs = max(5, i) }
                    default:
                        break
                    }
                }
            } else if leadingSpaces >= 4 {
                if currentSection == "audio" && currentSubSection == "custom_sounds" {
                    if !val.isEmpty {
                        config.audio.customSounds[key] = val
                    }
                }
            }
        }

        return config
    }

    private static func parseBool(_ val: String) -> Bool? {
        let low = val.lowercased()
        if low == "true" || low == "1" || low == "yes" || low == "on" {
            return true
        }
        if low == "false" || low == "0" || low == "no" || low == "off" {
            return false
        }
        return nil
    }
}
