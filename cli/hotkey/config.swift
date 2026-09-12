// cli/hotkey/config.swift
// Configuration support for Pet-Talk sensory presence layer.
//
// Reads ~/.pet-talk/config.yaml or falls back gracefully to built-in defaults.
// Supports audio toggling, volume adjustment, sound pack selection, and custom sounds.

import CoreGraphics
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

/// Spring physics, overridable so a design-playground export applies without a
/// rebuild. `nil` means "keep the built-in token" — see HUDTokens.apply(from:).
public struct MotionConfig {
    public var springStiffness: Double?
    public var springDamping: Double?
    public var springMass: Double?

    public init(springStiffness: Double? = nil, springDamping: Double? = nil, springMass: Double? = nil) {
        self.springStiffness = springStiffness
        self.springDamping = springDamping
        self.springMass = springMass
    }
}

/// Notch/capsule geometry overrides. Same contract: nil keeps the token.
public struct GeometryConfig {
    public var notchWidthFallback: CGFloat?
    public var expandedWidth: CGFloat?
    public var earFilletThreshold: CGFloat?
    public var earFilletRadius: CGFloat?
    public var bottomCornerRadius: CGFloat?
    public var pillCornerRadius: CGFloat?

    public init(
        notchWidthFallback: CGFloat? = nil,
        expandedWidth: CGFloat? = nil,
        earFilletThreshold: CGFloat? = nil,
        earFilletRadius: CGFloat? = nil,
        bottomCornerRadius: CGFloat? = nil,
        pillCornerRadius: CGFloat? = nil
    ) {
        self.notchWidthFallback = notchWidthFallback
        self.expandedWidth = expandedWidth
        self.earFilletThreshold = earFilletThreshold
        self.earFilletRadius = earFilletRadius
        self.bottomCornerRadius = bottomCornerRadius
        self.pillCornerRadius = pillCornerRadius
    }
}

/// Gesture timing. `double_tap_window_ms` is the pause chord's window.
public struct ChordConfig {
    public var doubleTapWindowMs: Double?

    public init(doubleTapWindowMs: Double? = nil) {
        self.doubleTapWindowMs = doubleTapWindowMs
    }
}

public struct PetTalkConfig {
    public var version: String = "1.0"
    public var audio: AudioConfig = AudioConfig()
    public var paste: PasteConfig = PasteConfig()
    public var motion: MotionConfig = MotionConfig()
    public var geometry: GeometryConfig = GeometryConfig()
    public var hotkey: ChordConfig = ChordConfig()
    /// Optional explicit path to the pet-talk-cli executable. Second in the
    /// resolution order (after env PET_TALK_CLI_PATH, before the sibling/repo-root
    /// walk) — see HotkeyListener.resolveCliPath(). Never a hardcoded default here;
    /// an absent/invalid value just falls through to the next resolution step.
    public var cliPath: String? = nil

    public init(
        version: String = "1.0",
        audio: AudioConfig = AudioConfig(),
        paste: PasteConfig = PasteConfig(),
        motion: MotionConfig = MotionConfig(),
        geometry: GeometryConfig = GeometryConfig(),
        hotkey: ChordConfig = ChordConfig(),
        cliPath: String? = nil
    ) {
        self.version = version
        self.audio = audio
        self.paste = paste
        self.motion = motion
        self.geometry = geometry
        self.hotkey = hotkey
        self.cliPath = cliPath
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
        case "motion.spring_stiffness":
            if Double(cleanVal) != nil {
                motion.springStiffness = PetTalkConfig.parseFiniteClamped(cleanVal, range: PetTalkConfig.stiffnessRange, key: key)
                return true
            }
        case "motion.spring_damping":
            if Double(cleanVal) != nil {
                motion.springDamping = PetTalkConfig.parseFiniteClamped(cleanVal, range: PetTalkConfig.dampingRange, key: key)
                return true
            }
        case "motion.spring_mass":
            if Double(cleanVal) != nil {
                motion.springMass = PetTalkConfig.parseFiniteClamped(cleanVal, range: PetTalkConfig.massRange, key: key)
                return true
            }
        case "geometry.notch_width_fallback":
            if let d = Double(cleanVal) { geometry.notchWidthFallback = CGFloat(d); return true }
        case "geometry.expanded_width":
            if let d = Double(cleanVal) { geometry.expandedWidth = CGFloat(d); return true }
        case "geometry.ear_fillet_threshold":
            if let d = Double(cleanVal) { geometry.earFilletThreshold = CGFloat(d); return true }
        case "geometry.ear_fillet_radius":
            if let d = Double(cleanVal) { geometry.earFilletRadius = CGFloat(d); return true }
        case "geometry.bottom_corner_radius":
            if let d = Double(cleanVal) { geometry.bottomCornerRadius = CGFloat(d); return true }
        case "geometry.pill_corner_radius":
            if let d = Double(cleanVal) { geometry.pillCornerRadius = CGFloat(d); return true }
        case "hotkey.double_tap_window_ms":
            if let d = Double(cleanVal) { hotkey.doubleTapWindowMs = d; return true }
        case "cli_path":
            cliPath = cleanVal.isEmpty ? nil : cleanVal
            return true
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
        if let cliPath = cliPath, !cliPath.isEmpty {
            lines.append("cli_path: \"\(cliPath)\"")
        }
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
        lines.append("# Spring physics. A design-playground export lands here and")
        lines.append("# applies on next launch — no rebuild. Omit a key to keep the token.")
        lines.append("motion:")
        lines.append("  spring_stiffness: \(fmt(motion.springStiffness, default: 220.0))")
        lines.append("  spring_damping: \(fmt(motion.springDamping, default: 21.0))")
        lines.append("  spring_mass: \(fmt(motion.springMass, default: 1.0))")
        lines.append("")
        lines.append("# Notch / capsule geometry. The resting width is MEASURED from the")
        lines.append("# screen; notch_width_fallback only applies when there is no notch.")
        lines.append("geometry:")
        lines.append("  notch_width_fallback: \(fmt(geometry.notchWidthFallback.map(Double.init), default: 180.0))")
        lines.append("  expanded_width: \(fmt(geometry.expandedWidth.map(Double.init), default: 440.0))")
        lines.append("  ear_fillet_threshold: \(fmt(geometry.earFilletThreshold.map(Double.init), default: 24.0))")
        lines.append("  ear_fillet_radius: \(fmt(geometry.earFilletRadius.map(Double.init), default: 10.0))")
        lines.append("  bottom_corner_radius: \(fmt(geometry.bottomCornerRadius.map(Double.init), default: 20.0))")
        lines.append("  pill_corner_radius: \(fmt(geometry.pillCornerRadius.map(Double.init), default: 22.0))")
        lines.append("")
        lines.append("# Gestures: Option+Tab asks, Option+Shift+Tab hands over,")
        lines.append("# two Option+Tab presses inside this window pause.")
        lines.append("hotkey:")
        lines.append("  double_tap_window_ms: \(Int(hotkey.doubleTapWindowMs ?? 400.0))")
        lines.append("")
        return lines.joined(separator: "\n")
    }

    private func fmt(_ value: Double?, default defaultValue: Double) -> String {
        return String(format: "%g", value ?? defaultValue)
    }

    /// Ranges the three spring numbers are clamped into. Anything outside these
    /// bounds risks feeding a nonsense value straight into the spring integrator.
    static let stiffnessRange: ClosedRange<Double> = 1...2000
    static let dampingRange: ClosedRange<Double> = 0...200
    static let massRange: ClosedRange<Double> = 0.1...10

    /// Parses a config numeric value for a spring key, rejecting non-finite
    /// values (NaN/inf) outright — those fall back to the built-in default (the
    /// caller keeps `nil`, which HUDTokens.apply(from:) reads as "keep the
    /// token") — and clamping anything in-range-of-parseable but out-of-bounds.
    /// Never crashes; always emits a named reason to stderr so a bad config.yaml
    /// value is visible, not silent.
    static func parseFiniteClamped(_ val: String, range: ClosedRange<Double>, key: String) -> Double? {
        guard let d = Double(val) else { return nil }
        guard d.isFinite else {
            logConfigReason("config_value_non_finite", key: key, raw: val)
            return nil
        }
        if d < range.lowerBound || d > range.upperBound {
            let clamped = Swift.min(Swift.max(d, range.lowerBound), range.upperBound)
            logConfigReason("config_value_out_of_range", key: key, raw: val, clamped: clamped)
            return clamped
        }
        return d
    }

    private static func logConfigReason(_ reason: String, key: String, raw: String, clamped: Double? = nil) {
        var line = "!-- config: reason=\(reason) key=\(key) value=\(raw)"
        if let clamped = clamped {
            line += " clamped_to=\(clamped)"
        } else {
            line += " using_default"
        }
        FileHandle.standardError.write((line + "\n").data(using: .utf8)!)
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
                    } else if key == "cli_path" {
                        config.cliPath = val.isEmpty ? nil : val
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
                } else if currentSection == "motion" {
                    currentSubSection = nil
                    switch key {
                    case "spring_stiffness":
                        if Double(val) != nil {
                            config.motion.springStiffness = parseFiniteClamped(val, range: stiffnessRange, key: "motion.spring_stiffness")
                        }
                    case "spring_damping":
                        if Double(val) != nil {
                            config.motion.springDamping = parseFiniteClamped(val, range: dampingRange, key: "motion.spring_damping")
                        }
                    case "spring_mass":
                        if Double(val) != nil {
                            config.motion.springMass = parseFiniteClamped(val, range: massRange, key: "motion.spring_mass")
                        }
                    default:
                        break
                    }
                } else if currentSection == "geometry" {
                    currentSubSection = nil
                    switch key {
                    case "notch_width_fallback":
                        if let d = Double(val) { config.geometry.notchWidthFallback = CGFloat(d) }
                    case "expanded_width":
                        if let d = Double(val) { config.geometry.expandedWidth = CGFloat(d) }
                    case "ear_fillet_threshold":
                        if let d = Double(val) { config.geometry.earFilletThreshold = CGFloat(d) }
                    case "ear_fillet_radius":
                        if let d = Double(val) { config.geometry.earFilletRadius = CGFloat(d) }
                    case "bottom_corner_radius":
                        if let d = Double(val) { config.geometry.bottomCornerRadius = CGFloat(d) }
                    case "pill_corner_radius":
                        if let d = Double(val) { config.geometry.pillCornerRadius = CGFloat(d) }
                    default:
                        break
                    }
                } else if currentSection == "hotkey" {
                    currentSubSection = nil
                    if key == "double_tap_window_ms", let d = Double(val) {
                        config.hotkey.doubleTapWindowMs = d
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
