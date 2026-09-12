// GENERATED FILE — do not edit by hand.
// Source: design/tokens.css + design/tokens.pet-talk.json
// Regenerate with: python3 design/build.py --write
//
// pet-talk's own token layer (type scale, geometry, motion,
// listening-line) PLUS the AgentWorth palette lifted out of
// design/tokens.css — AppKit cannot read a CSS custom property, so the
// notch HUD reads the same values from here. Colour identity is still
// AgentWorth's and is never redefined in tokens.pet-talk.json.
// Must compile standalone:
//   swiftc -typecheck cli/hotkey/DesignTokens.swift

import Foundation

public enum DesignTokens {

    // MARK: - Type scale
    public static let scaleRatio: Double = 1.200
    public static let sizeMicro: Double = 9.9
    public static let sizeCaption: Double = 11.8
    public static let sizeLabel: Double = 14.2
    public static let sizeBody: Double = 17
    public static let sizeSub: Double = 20.4
    public static let sizeHead: Double = 24.5
    public static let sizeDisplay: Double = 29.4
    public static let familySpoken: String = "IBM Plex Sans"
    public static let familyUi: String = "SF Pro"
    public static let familyMono: String = "SF Mono"

    // MARK: - Geometry
    public static let notchWidth: Double = 180
    public static let notchHeight: Double = 37
    public static let capsuleWidthRest: Double = 180
    public static let capsuleWidthExpanded: Double = 440
    public static let capsuleHeightListening: Double = 52
    public static let capsuleHeightExpanded: Double = 60
    public static let capsuleHeightMax: Double = 110
    public static let radiusBottom: Double = 20
    public static let radiusEarFillet: Double = 10
    public static let idleLineThickness: Double = 4
    public static let listeningLineThickness: Double = 2
    public static let listeningLineSamples: Double = 56
    public static let capsuleHeightError: Double = 60
    public static let sleepBead: Double = 2
    public static let hoverPeek: Double = 6

    // MARK: - Motion
    public static let springStiffness: Double = 220
    public static let springDamping: Double = 21
    public static let springMass: Double = 1
    public static let durationStallIn: Double = 120
    public static let durationSentenceArrive: Double = 160
    public static let durationReceiptStagger: Double = 60
    public static let durationBargeCut: Double = 0
    public static let durationSleepRetract: Double = 180
    public static let durationEscapeDismiss: Double = 40
    public static let errorShakeCycles: Double = 3
    public static let errorShakeAmplitude: Double = 3
    public static let errorShakeDuration: Double = 120
    public static let reducedMotionDistance: String = "full"
    public static let reducedMotionCrossfade: Double = 80

    // MARK: - Listening line / receipt colours
    /// live acoustic data only. Never a button.
    public static let signalLight: String = "#B7621A"
    public static let signalDark: String = "#F0A23C"
    /// one meaning only: a number outside its budget
    public static let pegLight: String = "#A02C1B"
    public static let pegDark: String = "#FF6A47"
    /// listening-line RMS attack filter coefficient, measured from cli/hotkey/hud_window.swift
    public static let attackLight: String = "0.22"
    public static let attackDark: String = "0.22"
    /// listening-line RMS decay filter coefficient, measured from cli/hotkey/hud_window.swift
    public static let decayLight: String = "0.78"
    public static let decayDark: String = "0.78"

    // MARK: - AgentWorth palette (parsed from design/tokens.css)
    //
    // Role names and values are AgentWorth's. `--mv-accent` is the ONE
    // accent in the system: selection, focus, links, and the receipt total
    // — nothing else. success/warn/danger are the reserved state colours.
    public static let paletteGroundLight: String = "#ffffff"
    public static let paletteGroundDark: String = "#000000"
    public static let paletteSurfaceLight: String = "#f4f4f5"
    public static let paletteSurfaceDark: String = "#18181b"
    public static let paletteSurface2Light: String = "#efeff1"
    public static let paletteSurface2Dark: String = "#1d1d20"
    public static let paletteSurface3Light: String = "#e4e4e7"
    public static let paletteSurface3Dark: String = "#27272a"
    public static let paletteBorderLight: String = "#e4e4e7"
    public static let paletteBorderDark: String = "#2a2a2d"
    public static let paletteBorderSoftLight: String = "#ececee"
    public static let paletteBorderSoftDark: String = "#1c1c1f"
    public static let paletteInkLight: String = "#111113"
    public static let paletteInkDark: String = "#ffffff"
    public static let paletteTextLight: String = "#3f3f46"
    public static let paletteTextDark: String = "#d4d4d8"
    public static let paletteMutedLight: String = "#52525b"
    public static let paletteMutedDark: String = "#a1a1aa"
    public static let paletteFaintLight: String = "#a1a1aa"
    public static let paletteFaintDark: String = "#52525b"
    public static let paletteAccentLight: String = "#7c6bb3"
    public static let paletteAccentDark: String = "#a396d6"
    public static let paletteSuccessLight: String = "#15803d"
    public static let paletteSuccessDark: String = "#34d399"
    public static let paletteWarnLight: String = "#b45309"
    public static let paletteWarnDark: String = "#fbbf24"
    public static let paletteDangerLight: String = "#b91c1c"
    public static let paletteDangerDark: String = "#f87171"
}
