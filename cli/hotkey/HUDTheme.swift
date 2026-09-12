// cli/hotkey/HUDTheme.swift
// The notch HUD's one source of colour, type and envelope.
//
// Why this file exists: the capsule used to carry its own palette — "Obsidian",
// "Emerald True", "SpacePilot Gold", "Liquid Silver" — spelled out as hex
// components inside the views. That skin belonged to nothing; it matched no
// product. Everything visible in the capsule now resolves here, and everything
// here resolves to DesignTokens.swift, which design/build.py generates from
// AgentWorth's tokens.css. There is no hex literal in this file and none in
// hud_window.swift; qa/test_hud.py fails the build if one appears.
//
// The capsule is ALWAYS the dark palette. It is not a window that happens to be
// dark — it is the physical notch, which is black glass at every hour of the
// day, so a light variant would be a light rectangle glued under a black notch.
// The three-state theming contract still holds where the theme is a choice: the
// cockpit (web/src) reads light by default and swaps on data-theme. The capsule
// has no choice to make.
//
// One violet rule (agentworth/docs/DESIGN.md): --mv-accent carries exactly two
// things in here — the peak of the listening line, and the receipt total.
// Selection and focus do not exist in a click-through HUD, and links do not
// either. State does NOT get the accent: success/warn/danger are the reserved
// state colours, and Archie's light carries the rest.
//
// Type: Geist and Geist Mono are AgentWorth's faces, and neither is installed
// system-wide on macOS. SF Pro and SF Mono are the native stand-ins — the same
// role split (mono for eyebrows, labels and tabular numbers; sans for
// everything a person reads as a sentence) at the same token sizes. Bundling
// the Geist OFL files into the hotkey binary is a follow-up; see
// docs/DYNAMIC-NOTCH-ISLAND-SPEC.md.

import AppKit
import Foundation

// MARK: - Envelopes

/// The six shapes the capsule can take. A seventh is a design decision, not a
/// code change — the set is pinned by qa/test_hud.py.
public enum HUDEnvelope: String, CaseIterable {
    /// Glyph plus one line, capsule at the measured notch width.
    case compact
    /// Glyph, the current sentence, and a dimmed preview of the next one.
    case expanded
    /// Multi-line, up to the 110pt ceiling. Past that the answer belongs in the
    /// cockpit, not under the notch.
    case tall
    /// A reason string and its fix. The only envelope that waits to be dismissed.
    case error
    /// Paused. The capsule collapses to the notch and leaves one bead behind —
    /// she stops taking up room rather than disappearing.
    case sleep
    /// No hardware notch (external display, iMac, pre-2021 MacBook): a floating
    /// pill. Same content, rounder corners, nothing gained and nothing added.
    case pill

    public var name: String { rawValue }
}

/// What one HUD state looks like: which envelope, which glyph pose, and whether
/// the listening line is live. Exported through `--dump-state` so the mapping is
/// asserted rather than described.
public struct HUDVisual {
    public let envelope: HUDEnvelope
    public let glyph: String
    /// "live" only while the microphone is actually feeding RMS in. Everything
    /// else is "off" — a line that moves without data is a lie about hearing.
    public let line: String

    public var asDictionary: [String: String] {
        ["envelope": envelope.rawValue, "glyph": glyph, "line": line]
    }
}

// MARK: - Theme

public enum HUDTheme {
    /// Reported by `--dump-state`. Changing this string means the palette
    /// underneath it changed.
    public static let name: String = "agentworth"

    /// The two roles --mv-accent is allowed to carry in the capsule. Exported so
    /// the one-violet rule is a test, not a comment.
    public static let accentRoles: [String] = ["listeningLinePeak", "receiptTotal"]

    // MARK: Colour

    /// Decode `#rgb`, `#rrggbb` or `#rrggbbaa` into an sRGB NSColor. This is the
    /// ONLY place the HUD constructs a colour: the inputs are token strings from
    /// the generated DesignTokens, so a literal cannot enter without being
    /// spelled as one and caught by the suite.
    public static func color(_ token: String, alpha: CGFloat = 1.0) -> NSColor {
        var s = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("#") { s.removeFirst() }
        if s.count == 3 {
            s = s.map { "\($0)\($0)" }.joined()
        }
        guard s.count == 6 || s.count == 8, let value = UInt64(s, radix: 16) else {
            // A malformed token is a generator bug, not a runtime condition. Fail
            // visibly rather than quietly painting something plausible.
            return .magenta
        }
        let shift = s.count == 8 ? 8 : 0
        let r = CGFloat((value >> (16 + shift)) & 0xFF) / 255.0
        let g = CGFloat((value >> (8 + shift)) & 0xFF) / 255.0
        let b = CGFloat((value >> shift) & 0xFF) / 255.0
        let a = s.count == 8 ? CGFloat(value & 0xFF) / 255.0 : 1.0
        return NSColor(srgbRed: r, green: g, blue: b, alpha: a * alpha)
    }

    /// The capsule ground: the same black as the physical notch glass, so the
    /// software edge and the hardware edge are one shape.
    public static var ground: NSColor { color(DesignTokens.paletteGroundDark) }
    /// A raised plane inside the capsule (the receipt block's backing).
    public static var surface: NSColor { color(DesignTokens.paletteSurfaceDark) }
    /// The 1pt outline. Dark enough to read as an edge, never as a window chrome.
    public static var border: NSColor { color(DesignTokens.paletteBorderDark) }
    /// Highest-contrast text: the sentence she is speaking.
    public static var ink: NSColor { color(DesignTokens.paletteInkDark) }
    /// Body text: what was heard, an error's fix line.
    public static var text: NSColor { color(DesignTokens.paletteTextDark) }
    /// Eyebrows, labels, the body of the listening line, receipt labels.
    public static var muted: NSColor { color(DesignTokens.paletteMutedDark) }
    /// The dimmest legible step: the next-sentence preview, the sleep bead.
    public static var faint: NSColor { color(DesignTokens.paletteFaintDark) }
    /// One violet, two roles. See `accentRoles`.
    public static var accent: NSColor { color(DesignTokens.paletteAccentDark) }
    /// Reserved state pegs. Never decoration, never an accent substitute.
    public static var success: NSColor { color(DesignTokens.paletteSuccessDark) }
    public static var warn: NSColor { color(DesignTokens.paletteWarnDark) }
    public static var danger: NSColor { color(DesignTokens.paletteDangerDark) }

    // MARK: Type

    /// Which face and size a piece of capsule text takes. Mono carries anything
    /// a machine produced; sans carries anything a person reads as language.
    public enum TextRole {
        /// Mono, smallest step: receipt labels and values, the no-notch tag.
        case receipt
        /// Mono, smallest step, violet: the receipt total. The only number the
        /// accent touches.
        case receiptTotal
        /// Mono, smallest step, letter-spaced: "heard", "paused", an error code.
        case eyebrow
        /// Sans: the one line a compact capsule shows.
        case line
        /// Sans: the sentence being spoken, in an expanded or tall capsule.
        case sentence
        /// Sans: the dimmed next-sentence preview.
        case preview
    }

    /// SF Pro / SF Mono as the native stand-in for Geist / Geist Mono, at token
    /// sizes. Bundling the Geist OFL files is a follow-up.
    public static func font(_ role: TextRole) -> NSFont {
        switch role {
        case .receipt, .receiptTotal:
            return NSFont.monospacedSystemFont(ofSize: CGFloat(DesignTokens.sizeMicro), weight: .medium)
        case .eyebrow:
            return NSFont.monospacedSystemFont(ofSize: CGFloat(DesignTokens.sizeMicro), weight: .semibold)
        case .line, .sentence:
            return NSFont.systemFont(ofSize: CGFloat(DesignTokens.sizeCaption), weight: .medium)
        case .preview:
            return NSFont.systemFont(ofSize: CGFloat(DesignTokens.sizeCaption), weight: .regular)
        }
    }

    /// The colour that goes with a text role, so the two are never chosen apart.
    public static func colour(_ role: TextRole) -> NSColor {
        switch role {
        case .receipt: return muted
        case .receiptTotal: return accent
        case .eyebrow: return muted
        case .line: return text
        case .sentence: return ink
        case .preview: return faint
        }
    }

    /// The token names behind each role, for the spec and for `--dump-state`.
    /// Prose in a doc drifts; this does not.
    public static let typeTokens: [String: String] = [
        "receipt": "type.size.micro / type.family.mono",
        "receiptTotal": "type.size.micro / type.family.mono / --mv-accent",
        "eyebrow": "type.size.micro / type.family.mono",
        "line": "type.size.caption / type.family.ui",
        "sentence": "type.size.caption / type.family.ui",
        "preview": "type.size.caption / type.family.ui"
    ]

    // MARK: State → visual

    /// The mapping table. `hasNotch: false` rewrites every envelope to `.pill`,
    /// because the fallback has only one shape.
    public static func visual(
        for state: String,
        hasNotch: Bool = true,
        tall: Bool = false
    ) -> HUDVisual {
        let resolved: HUDVisual
        switch state {
        case "listening":
            resolved = HUDVisual(envelope: .compact, glyph: "listening", line: "live")
        case "thinking":
            // No spinner. The glyph light holds steady and the stall text says
            // what she is waiting on — a spinner would claim progress it cannot
            // measure.
            resolved = HUDVisual(envelope: .compact, glyph: "idle", line: "off")
        case "speaking":
            resolved = HUDVisual(envelope: tall ? .tall : .expanded, glyph: "speaking", line: "off")
        case "error":
            resolved = HUDVisual(envelope: .error, glyph: "error", line: "off")
        case "sleep":
            resolved = HUDVisual(envelope: .sleep, glyph: "error", line: "off")
        default:
            resolved = HUDVisual(envelope: .compact, glyph: "idle", line: "off")
        }
        guard hasNotch else {
            return HUDVisual(envelope: .pill, glyph: resolved.glyph, line: resolved.line)
        }
        return resolved
    }

    /// The whole table at once, for `--dump-state`.
    public static var stateVisualTable: [String: [String: String]] {
        var out: [String: [String: String]] = [:]
        for state in ["listening", "thinking", "speaking", "error", "sleep"] {
            out[state] = visual(for: state).asDictionary
        }
        return out
    }

    // MARK: Motion

    /// Reduced motion, stated once: distance goes to zero, durations stay, and
    /// live microphone data is smoothed rather than suppressed — hiding the line
    /// would hide whether she can hear.
    public static var reducedMotionPolicy: [String: Any] {
        [
            "active": NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
            "distance": "zero",
            "crossfadeMs": DesignTokens.reducedMotionCrossfade,
            "micDataSmoothedNotSuppressed": true
        ]
    }
}
