// cli/hotkey/ArchieGlyph.swift
// Archie's small form, ported to native drawing — the Swift-side twin of
// web/src/components/ArchieGlyph.tsx (PLAN-2026-09-12 §C, Lane 5a).
//
// This is the small form ONLY: no torch drawing, no accessories. Below 40px
// (agentworth/packages/ui/brand/archie/README.md, "The size rule") Archie's
// hand-held torch stops reading as a drawing, so the small form swaps it for
// a lit disc with a soft halo at the paw and grows the nose so it cannot
// read as an open mouth. That is the only form this file ever draws — the
// notch/menu-bar glyph never needs the full pose set.
//
// Geometry is transcribed from the vendored SVG's own numbers
// (web/src/brand/archie/front-sit.svg, the elements visible when
// data-size="small": the torch-dot group, the front legs, the torso, the
// head/ear/nose-sm/eyes group — everything under svg[data-size="small"]'s
// CSS in that file), not redrawn by eye, so this and the web glyph stay the
// same drawing in two renderers. The accessory group (#lamp, #goggles) is
// left out entirely — this glyph always arrives bare.
//
// Colours are AgentWorth's C3 colourway (their default, and pet-talk's too —
// no new palette). C4 ("Quiet", dense chrome) is also available.
//
// State is the light, same data-lamp convention as the web glyph:
//   idle       lit, steady   (halo alpha 0.55)
//   listening  lit, bright   (halo alpha 1.0)
//   speaking   lit; beat() briefly boosts the halo/lens per word arrival
//   error      off           (halo hidden, lens paints as body colour)
//
// Fan rule: this file only needs `swiftc -typecheck` locally; the real
// build runs via cli/hotkey/build.sh on ssh air/lenovo.
//
// Integration point (lane 3's HUD is not this lane's to wire): drop
//   let archie = ArchieGlyphView(state: .idle)
// into the HUD's content view as a subview near the capsule label, and call
// `archie.state = <mapped state>` / `archie.beat()` from wherever hud_window.swift
// already tracks turn state and word arrival.

import AppKit
import Foundation

public enum ArchieGlyphState {
    case idle
    case listening
    case speaking
    case error

    /// Distinct per state — matches ArchieGlyph.tsx's ARIA_LABEL table exactly.
    public var ariaLabel: String {
        switch self {
        case .idle: return "Archie — idle"
        case .listening: return "Archie — listening"
        case .speaking: return "Archie — speaking"
        case .error: return "Archie — error"
        }
    }

    fileprivate var lampOn: Bool { self != .error }

    fileprivate var haloAlpha: CGFloat {
        switch self {
        case .idle: return 0.55
        case .listening: return 1.0
        case .speaking: return 0.85
        case .error: return 0.0
        }
    }
}

public enum ArchieColourway {
    case c3 // AgentWorth's default — the only one this glyph ships with by default.
    case c4 // Quiet: dense chrome, must not out-shout the data.

    fileprivate var colours: ArchieGlyphColours {
        switch self {
        case .c3: return ArchieGlyphColours(
            ink: NSColor(srgbRed: 0x11 / 255, green: 0x11 / 255, blue: 0x13 / 255, alpha: 1),
            body: NSColor(srgbRed: 0xff / 255, green: 0xff / 255, blue: 0xff / 255, alpha: 1),
            mass: NSColor(srgbRed: 0xa3 / 255, green: 0x96 / 255, blue: 0xd6 / 255, alpha: 1),
            accent: NSColor(srgbRed: 0x7c / 255, green: 0x6b / 255, blue: 0xb3 / 255, alpha: 1),
            accent2: NSColor(srgbRed: 0xe2 / 255, green: 0xdd / 255, blue: 0xf0 / 255, alpha: 1)
        )
        case .c4: return ArchieGlyphColours(
            ink: NSColor(srgbRed: 0x52 / 255, green: 0x52 / 255, blue: 0x5b / 255, alpha: 1),
            body: NSColor(srgbRed: 0xff / 255, green: 0xff / 255, blue: 0xff / 255, alpha: 1),
            mass: NSColor(srgbRed: 0xe4 / 255, green: 0xe4 / 255, blue: 0xe7 / 255, alpha: 1),
            accent: NSColor(srgbRed: 0xa1 / 255, green: 0xa1 / 255, blue: 0xaa / 255, alpha: 1),
            accent2: NSColor(srgbRed: 0xf4 / 255, green: 0xf4 / 255, blue: 0xf5 / 255, alpha: 1)
        )
        }
    }
}

private struct ArchieGlyphColours {
    let ink: NSColor
    let body: NSColor
    let mass: NSColor
    let accent: NSColor
    let accent2: NSColor
}

private extension NSBezierPath {
    /// SVG quadratic Bezier (`Q`), elevated to the cubic curve NSBezierPath
    /// actually has. Exact conversion, not an approximation.
    func quadCurve(to end: NSPoint, controlPoint qcp: NSPoint) {
        let start = currentPoint
        let cp1 = NSPoint(x: start.x + 2.0 / 3.0 * (qcp.x - start.x),
                           y: start.y + 2.0 / 3.0 * (qcp.y - start.y))
        let cp2 = NSPoint(x: end.x + 2.0 / 3.0 * (qcp.x - end.x),
                           y: end.y + 2.0 / 3.0 * (qcp.y - end.y))
        curve(to: end, controlPoint1: cp1, controlPoint2: cp2)
    }
}

/// The notch/menu-bar glyph: Archie's small form, state-driven.
public final class ArchieGlyphView: NSView {
    public var state: ArchieGlyphState {
        didSet { if oldValue != state { needsDisplay = true } }
    }

    public var colourway: ArchieColourway {
        didSet { needsDisplay = true }
    }

    /// Decays each tick; draw() reads it to briefly scale the paw-light up.
    /// Public API is `beat()`, not this — same shape as the web glyph's
    /// per-word "kick" class, driven by the caller's own word-arrival timer
    /// rather than a CSS loop, because a loop can't know when a word lands.
    private var kick: CGFloat = 0
    private var kickTimer: Timer?

    public init(state: ArchieGlyphState = .idle, colourway: ArchieColourway = .c3, frame: NSRect = NSRect(x: 0, y: 0, width: 24, height: 24)) {
        self.state = state
        self.colourway = colourway
        super.init(frame: frame)
        wantsLayer = true
    }

    public required init?(coder: NSCoder) {
        self.state = .idle
        self.colourway = .c3
        super.init(coder: coder)
        wantsLayer = true
    }

    /// SVG viewBox is y-down; flipping keeps every coordinate below a direct
    /// transcription of the SVG's own numbers.
    public override var isFlipped: Bool { true }

    public override func accessibilityLabel() -> String? { state.ariaLabel }

    public override func setAccessibilityLabel(_ label: String?) {
        // State drives the label (see ArchieGlyphState.ariaLabel) — ignore
        // external overrides so it can never drift from the four state labels.
    }

    public override func accessibilityRole() -> NSAccessibility.Role? { .image }

    /// Call once per word arrival while state == .speaking (mirrors
    /// ArchieGlyph.tsx scheduling one "kick" per WordTime.start_ms). A no-op
    /// under reduced motion — the state change (bright, lit) already
    /// happened; only the beat itself is what reduced motion drops.
    public func beat() {
        guard state == .speaking else { return }
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        kick = 1.0
        needsDisplay = true
        kickTimer?.invalidate()
        kickTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] timer in
            guard let self = self else { timer.invalidate(); return }
            self.kick *= 0.6
            self.needsDisplay = true
            if self.kick < 0.02 {
                self.kick = 0
                timer.invalidate()
            }
        }
    }

    public override func draw(_ dirtyRect: NSRect) {
        let colours = colourway.colours
        let scale = min(bounds.width, bounds.height) / 48.0
        guard scale > 0 else { return }
        let originX = (bounds.width - 48.0 * scale) / 2.0
        let originY = (bounds.height - 48.0 * scale) / 2.0

        func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint {
            NSPoint(x: originX + x * scale, y: originY + y * scale)
        }

        // ---- torso + front legs (base) ----
        let frontLeg = NSBezierPath()
        frontLeg.move(to: p(31.4, 32))
        frontLeg.quadCurve(to: p(39, 21.4), controlPoint: p(37.6, 28.6))
        frontLeg.line(to: p(42.2, 23))
        frontLeg.quadCurve(to: p(33.6, 36), controlPoint: p(41.4, 32.6))
        frontLeg.close()
        colours.body.setFill()
        frontLeg.fill()

        let torso = NSBezierPath()
        torso.move(to: p(18, 24.6))
        torso.quadCurve(to: p(30, 24.6), controlPoint: p(24, 22))
        torso.line(to: p(32.6, 39.8))
        torso.quadCurve(to: p(27.6, 44.2), controlPoint: p(32.6, 44.2))
        torso.line(to: p(20.4, 44.2))
        torso.quadCurve(to: p(15.4, 39.8), controlPoint: p(15.4, 44.2))
        torso.close()
        colours.body.setFill()
        torso.fill()

        let leg1 = NSBezierPath(roundedRect: NSRect(x: p(16.2, 39.4).x, y: p(16.2, 39.4).y,
                                                      width: 6.6 * scale, height: 5.6 * scale),
                                 xRadius: 2.8 * scale, yRadius: 2.8 * scale)
        colours.body.setFill()
        leg1.fill()
        let leg2 = NSBezierPath(roundedRect: NSRect(x: p(25.2, 39.4).x, y: p(25.2, 39.4).y,
                                                      width: 6.6 * scale, height: 5.6 * scale),
                                 xRadius: 2.8 * scale, yRadius: 2.8 * scale)
        colours.body.setFill()
        leg2.fill()

        // ---- head group, local origin translate(24, 13.2) ----
        func h(_ x: CGFloat, _ y: CGFloat) -> NSPoint { p(24 + x, 13.2 + y) }

        let ear1 = NSBezierPath()
        ear1.move(to: h(-7, -6))
        ear1.curve(to: h(-13, 15), controlPoint1: h(-14, -5), controlPoint2: h(-16, 6))
        ear1.curve(to: h(-5.5, 13), controlPoint1: h(-11, 20), controlPoint2: h(-5, 19))
        ear1.curve(to: h(-6, -6), controlPoint1: h(-6, 7), controlPoint2: h(-7, 0))
        ear1.close()
        colours.mass.setFill()
        ear1.fill()

        let ear2 = NSBezierPath()
        ear2.move(to: h(7, -6))
        ear2.curve(to: h(13, 16), controlPoint1: h(14, -5), controlPoint2: h(16, 7))
        ear2.curve(to: h(5.5, 14), controlPoint1: h(11, 21), controlPoint2: h(5, 20))
        ear2.curve(to: h(6, -6), controlPoint1: h(6, 8), controlPoint2: h(7, 0))
        ear2.close()
        colours.mass.setFill()
        ear2.fill()

        let head = NSBezierPath()
        head.move(to: h(0, -8.6))
        head.curve(to: h(8, -1), controlPoint1: h(4.8, -8.6), controlPoint2: h(8, -5.2))
        head.curve(to: h(9.2, 6.2), controlPoint1: h(8, 2), controlPoint2: h(9.2, 4.4))
        head.curve(to: h(0, 10.2), controlPoint1: h(9.2, 8.6), controlPoint2: h(5.4, 10.2))
        head.curve(to: h(-9.2, 6.2), controlPoint1: h(-5.4, 10.2), controlPoint2: h(-9.2, 8.6))
        head.curve(to: h(-8, -1), controlPoint1: h(-9.2, 4.4), controlPoint2: h(-8, 2))
        head.curve(to: h(0, -8.6), controlPoint1: h(-8, -5.2), controlPoint2: h(-4.8, -8.6))
        head.close()
        colours.body.setFill()
        head.fill()

        // nose-sm: the grown nose that stands in for `.archie-nose` under 40px.
        let noseRect = NSRect(x: h(0, 5.6).x - 3.2 * scale, y: h(0, 5.6).y - 2.5 * scale,
                               width: 6.4 * scale, height: 5.0 * scale)
        colours.ink.setFill()
        NSBezierPath(ovalIn: noseRect).fill()

        for eyeX: CGFloat in [-3.5, 3.5] {
            let center = h(eyeX, -1.6)
            let r = 1.5 * scale
            let eyeRect = NSRect(x: center.x - r, y: center.y - r, width: r * 2, height: r * 2)
            colours.ink.setFill()
            NSBezierPath(ovalIn: eyeRect).fill()
        }

        // ---- torch-dot: the paw light, state is the light ----
        let paw = p(11.6, 41.4)
        let kickScale = 1.0 + kick * 0.2
        if state.lampOn {
            let haloR = 7.2 * scale * kickScale
            let haloRect = NSRect(x: paw.x - haloR, y: paw.y - haloR, width: haloR * 2, height: haloR * 2)
            colours.accent2.withAlphaComponent(state.haloAlpha).setFill()
            NSBezierPath(ovalIn: haloRect).fill()

            let lensR = 3.4 * scale * kickScale
            let lensRect = NSRect(x: paw.x - lensR, y: paw.y - lensR, width: lensR * 2, height: lensR * 2)
            colours.accent.setFill()
            let lens = NSBezierPath(ovalIn: lensRect)
            lens.fill()
            colours.ink.setStroke()
            lens.lineWidth = 1.8 * scale
            lens.stroke()
        } else {
            // data-lamp="off": halo hidden, lens paints as body colour — same
            // rule as every other "off" pose in the vendored archie.css.
            let lensR = 3.4 * scale
            let lensRect = NSRect(x: paw.x - lensR, y: paw.y - lensR, width: lensR * 2, height: lensR * 2)
            colours.body.setFill()
            let lens = NSBezierPath(ovalIn: lensRect)
            lens.fill()
            colours.ink.setStroke()
            lens.lineWidth = 1.8 * scale
            lens.stroke()
        }
    }
}
