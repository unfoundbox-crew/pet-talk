// GENERATED FILE — do not edit by hand.
// Source: design/tokens.css + design/tokens.pet-talk.json
// Regenerate with: python3 design/build.py --write
//
// pet-talk's own token layer (type scale, geometry, motion,
// listening-line). Colour/neutral identity lives in AgentWorth's
// tokens.css and has no Swift consumer today — this file carries
// only what's genuinely pet-talk's own. Must compile standalone:
//   swiftc -typecheck cli/hotkey/DesignTokens.swift

import Foundation

public enum DesignTokens {

    // MARK: - Type scale
    public static let scaleRatio: Double = 1.200
    public static let sizeLabel: Double = 14.2
    public static let sizeBody: Double = 17
    public static let sizeSub: Double = 20.4
    public static let sizeHead: Double = 24.5
    public static let sizeDisplay: Double = 29.4
    public static let familySpoken: String = "IBM Plex Sans"

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
    public static let sleepBead: Double = 2
    public static let hoverPeek: Double = 6

    // MARK: - Motion
    public static let springStiffness: Double = 220
    public static let springDamping: Double = 21
    public static let springMass: Double = 1
    public static let durationStallIn: Double = 120
    public static let durationSentenceArrive: Double = 160
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
}
