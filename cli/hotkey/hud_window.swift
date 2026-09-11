// cli/hotkey/hud_window.swift
// Native macOS Hardware Notch Dynamic Island & Floating Capsule HUD for Pet-Talk.
//
// Technical Architecture & Specifications:
// - Hardware Notch Awareness: Queries NSScreen auxiliaryTopLeftArea / auxiliaryTopRightArea
//   to detect physical MacBook webcam notch geometry (220x38px centered at top of display).
// - Hardware-Software Fusion:
//   - Top edge anchors flush to screen.frame.maxY, enveloping the camera notch in pure #000000.
//   - Continuous squircle curvature (r=20px) on bottom corners.
//   - Concave top ear fillets (r=10px) that sweep outward smoothly into the display bezel.
// - Motion Design Language (Apple Fluid Springs):
//   - "The Drip" Entrance: Unfolds downward from the physical notch (38px -> 52px) via CASpringAnimation.
//   - Dynamic Expansion: Morphing width (220px -> 440px, height 52px -> 60px) during dictation/speaking.
//   - "Suction" Retraction: Springs back up into the physical camera notch on dismissal.
// - Fallback for External Displays: Gracefully falls back to an elegant floating pill (r=22px) centered at top.
// - Non-activating NSPanel subclass: [.nonactivatingPanel, .borderless] (zero focus stealing).
// - Level: .floating, Spaces: [.canJoinAllSpaces, .fullScreenAuxiliary], Click-through: ignoresMouseEvents = true.

import AppKit
import Foundation
import QuartzCore

// MARK: - Notch Geometry & Manager

public struct NotchGeometry {
    public let hasNotch: Bool
    public let rect: NSRect          // Notch rectangle in screen coordinates
    public let notchWidth: CGFloat   // Typically 220.0 on modern MacBook Pro
    public let notchHeight: CGFloat  // Typically 38.0
    public let screenFrame: NSRect
    public let visibleFrame: NSRect
}

public class NotchManager {
    public static let shared = NotchManager()

    public func currentNotch(for screen: NSScreen? = nil) -> NotchGeometry {
        let targetScreen = screen ?? NSScreen.main ?? (NSScreen.screens.first ?? NSScreen())
        let sFrame = targetScreen.frame
        let vFrame = targetScreen.visibleFrame

        if #available(macOS 12.0, *),
           let left = targetScreen.auxiliaryTopLeftArea,
           let right = targetScreen.auxiliaryTopRightArea,
           left.width > 0, right.width > 0 {
            let notchX = left.maxX
            let notchW = right.minX - left.maxX
            let notchH = left.height
            let notchY = sFrame.maxY - notchH
            let notchRect = NSRect(x: notchX, y: notchY, width: notchW, height: notchH)
            return NotchGeometry(
                hasNotch: true,
                rect: notchRect,
                notchWidth: notchW,
                notchHeight: notchH,
                screenFrame: sFrame,
                visibleFrame: vFrame
            )
        }

        // Fallback for displays without a hardware notch (external monitors, iMac, older MacBooks)
        let fallbackWidth: CGFloat = 220.0
        let fallbackHeight: CGFloat = 38.0
        let fallbackX = vFrame.midX - (fallbackWidth / 2.0)
        let fallbackY = vFrame.maxY - fallbackHeight
        return NotchGeometry(
            hasNotch: false,
            rect: NSRect(x: fallbackX, y: fallbackY, width: fallbackWidth, height: fallbackHeight),
            notchWidth: fallbackWidth,
            notchHeight: fallbackHeight,
            screenFrame: sFrame,
            visibleFrame: vFrame
        )
    }
}

// MARK: - HUD State Definition

public enum HUDState: String, CaseIterable {
    case listening = "listening"
    case thinking = "thinking"
    case speaking = "speaking"

    public var labelText: String {
        switch self {
        case .listening:
            return "Listening..."
        case .thinking:
            return "Donna thinking..."
        case .speaking:
            return "Speaking..."
        }
    }

    public var accentColor: NSColor {
        switch self {
        case .listening:
            // Emerald True (#10b981)
            return NSColor(srgbRed: 0x10 / 255.0, green: 0xb9 / 255.0, blue: 0x81 / 255.0, alpha: 1.0)
        case .thinking:
            // SpacePilot Gold (#c9a227)
            return NSColor(srgbRed: 0xc9 / 255.0, green: 0xa2 / 255.0, blue: 0x27 / 255.0, alpha: 1.0)
        case .speaking:
            // Liquid Silver (#cfd4dc)
            return NSColor(srgbRed: 0xcf / 255.0, green: 0xd4 / 255.0, blue: 0xdc / 255.0, alpha: 1.0)
        }
    }
}

// MARK: - Kinetic Indicator View

public class HUDIndicatorView: NSView {
    private var activeLayers: [CALayer] = []

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        self.wantsLayer = true
        self.layer?.masksToBounds = false
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        self.wantsLayer = true
        self.layer?.masksToBounds = false
    }

    public func configure(for state: HUDState) {
        guard let layer = self.layer else { return }

        // Remove existing sublayers and active animations
        activeLayers.forEach { $0.removeAllAnimations(); $0.removeFromSuperlayer() }
        activeLayers.removeAll()

        switch state {
        case .listening:
            setupListeningWaveform(parentLayer: layer, color: state.accentColor)
        case .thinking:
            setupThinkingSpinner(parentLayer: layer, color: state.accentColor)
        case .speaking:
            setupSpeakingAudioBars(parentLayer: layer, color: state.accentColor)
        }
    }

    // [LISTENING] Emerald True pulsating waveform
    private func setupListeningWaveform(parentLayer: CALayer, color: NSColor) {
        let barWidth: CGFloat = 2.5
        let spacing: CGFloat = 3.5
        let heights: [(initial: CGFloat, target: CGFloat, duration: Double)] = [
            (initial: 6.0, target: 14.0, duration: 0.42),
            (initial: 12.0, target: 18.0, duration: 0.32),
            (initial: 7.0, target: 15.0, duration: 0.48)
        ]

        let totalW = CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * spacing
        let startX: CGFloat = (bounds.width - totalW) / 2.0
        let centerY = bounds.height / 2.0

        for (index, cfg) in heights.enumerated() {
            let barLayer = CALayer()
            let x = startX + CGFloat(index) * (barWidth + spacing)
            barLayer.frame = CGRect(x: x, y: centerY - cfg.initial / 2.0, width: barWidth, height: cfg.initial)
            barLayer.cornerRadius = barWidth / 2.0
            barLayer.backgroundColor = color.cgColor

            barLayer.shadowColor = color.cgColor
            barLayer.shadowRadius = 4.0
            barLayer.shadowOpacity = 0.5
            barLayer.shadowOffset = .zero

            let anim = CABasicAnimation(keyPath: "bounds.size.height")
            anim.fromValue = cfg.initial
            anim.toValue = cfg.target
            anim.duration = cfg.duration
            anim.autoreverses = true
            anim.repeatCount = .infinity
            anim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)

            barLayer.add(anim, forKey: "pulseWaveform")
            parentLayer.addSublayer(barLayer)
            activeLayers.append(barLayer)
        }
    }

    // [THINKING] SpacePilot Gold spinning orbit indicator
    private func setupThinkingSpinner(parentLayer: CALayer, color: NSColor) {
        let spinnerLayer = CAShapeLayer()
        let radius: CGFloat = 6.5
        let center = CGPoint(x: bounds.width / 2.0, y: bounds.height / 2.0)

        let path = CGMutablePath()
        path.addArc(
            center: center,
            radius: radius,
            startAngle: 0,
            endAngle: CGFloat(Double.pi * 1.5), // 270 degree open arc
            clockwise: false
        )

        spinnerLayer.bounds = bounds
        spinnerLayer.position = center
        spinnerLayer.path = path
        spinnerLayer.fillColor = nil
        spinnerLayer.strokeColor = color.cgColor
        spinnerLayer.lineWidth = 2.0
        spinnerLayer.lineCap = .round

        spinnerLayer.shadowColor = color.cgColor
        spinnerLayer.shadowRadius = 3.0
        spinnerLayer.shadowOpacity = 0.6
        spinnerLayer.shadowOffset = .zero

        let rotation = CABasicAnimation(keyPath: "transform.rotation.z")
        rotation.fromValue = 0
        rotation.toValue = -Double.pi * 2.0
        rotation.duration = 0.75
        rotation.repeatCount = .infinity
        rotation.isRemovedOnCompletion = false

        spinnerLayer.add(rotation, forKey: "thinkingSpin")
        parentLayer.addSublayer(spinnerLayer)
        activeLayers.append(spinnerLayer)
    }

    // [SPEAKING] Liquid Silver kinetic audio bars
    private func setupSpeakingAudioBars(parentLayer: CALayer, color: NSColor) {
        let barWidth: CGFloat = 2.0
        let spacing: CGFloat = 2.5
        let bars: [(initial: CGFloat, target: CGFloat, duration: Double)] = [
            (initial: 4.0, target: 12.0, duration: 0.30),
            (initial: 11.0, target: 16.0, duration: 0.38),
            (initial: 6.0, target: 14.0, duration: 0.26),
            (initial: 12.0, target: 5.0, duration: 0.34)
        ]

        let totalWidth = CGFloat(bars.count) * barWidth + CGFloat(bars.count - 1) * spacing
        let startX = (bounds.width - totalWidth) / 2.0
        let centerY = bounds.height / 2.0

        for (index, cfg) in bars.enumerated() {
            let bar = CALayer()
            let x = startX + CGFloat(index) * (barWidth + spacing)
            bar.frame = CGRect(x: x, y: centerY - cfg.initial / 2.0, width: barWidth, height: cfg.initial)
            bar.cornerRadius = barWidth / 2.0
            bar.backgroundColor = color.cgColor

            bar.shadowColor = color.cgColor
            bar.shadowRadius = 2.0
            bar.shadowOpacity = 0.4
            bar.shadowOffset = .zero

            let anim = CABasicAnimation(keyPath: "bounds.size.height")
            anim.fromValue = cfg.initial
            anim.toValue = cfg.target
            anim.duration = cfg.duration
            anim.autoreverses = true
            anim.repeatCount = .infinity
            anim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)

            bar.add(anim, forKey: "kineticAudioBar")
            parentLayer.addSublayer(bar)
            activeLayers.append(bar)
        }
    }
}

// MARK: - Capsule Content View (Notch Dynamic Island)

public class HUDCapsuleView: NSView {
    public static let capsuleWidth: CGFloat = 220.0
    public static let expandedWidth: CGFloat = 440.0
    public static let capsuleHeight: CGFloat = 44.0
    public static let capsuleRadius: CGFloat = 22.0

    // Notch specific dynamic metrics
    public static let notchRestingHeight: CGFloat = 38.0
    public static let notchListeningHeight: CGFloat = 52.0
    public static let notchExpandedHeight: CGFloat = 60.0

    // Flipped coordinates: (0, 0) is top-left, making top-edge anchoring clean and deterministic
    public override var isFlipped: Bool { return true }

    private let maskShapeLayer = CAShapeLayer()
    private let borderShapeLayer = CAShapeLayer()
    private let visualEffectView = NSVisualEffectView()
    private let obsidianBackgroundLayer = CALayer()

    public let indicatorView = HUDIndicatorView()
    public let personaBadge = NSTextField()
    public let labelField = NSTextField()

    private var currentWidth: CGFloat = HUDCapsuleView.capsuleWidth
    private var currentHeight: CGFloat = HUDCapsuleView.notchListeningHeight
    private var hasNotch: Bool = true

    public override init(frame frameRect: NSRect) {
        let notchInfo = NotchManager.shared.currentNotch()
        let initialH = notchInfo.hasNotch ? Self.notchListeningHeight : Self.capsuleHeight
        super.init(frame: NSRect(x: 0, y: 0, width: Self.capsuleWidth, height: initialH))
        self.hasNotch = notchInfo.hasNotch
        self.currentHeight = initialH
        setupView()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupView()
    }

    private func setupView() {
        self.wantsLayer = true
        guard let rootLayer = self.layer else { return }

        rootLayer.masksToBounds = true

        // 1. Frosted glass backdrop (.behindWindow)
        visualEffectView.frame = bounds
        visualEffectView.autoresizingMask = [.width, .height]
        visualEffectView.material = .hudWindow
        visualEffectView.blendingMode = .behindWindow
        visualEffectView.state = .active
        visualEffectView.wantsLayer = true
        addSubview(visualEffectView)

        // 2. Obsidian Jet Black & Deep Zinc surface overlay
        obsidianBackgroundLayer.frame = bounds
        // Pitch black (#000000) base matching physical notch glass with subtle deep zinc warmth
        obsidianBackgroundLayer.backgroundColor = NSColor(
            srgbRed: 0x08 / 255.0,
            green: 0x09 / 255.0,
            blue: 0x0e / 255.0,
            alpha: 0.95
        ).cgColor
        rootLayer.insertSublayer(obsidianBackgroundLayer, above: visualEffectView.layer)

        // 3. Precision Border Outline Layer (1px subtle graphite stroke)
        borderShapeLayer.fillColor = nil
        borderShapeLayer.strokeColor = NSColor(
            srgbRed: 0x22 / 255.0,
            green: 0x26 / 255.0,
            blue: 0x36 / 255.0,
            alpha: 0.85
        ).cgColor
        borderShapeLayer.lineWidth = 1.0
        rootLayer.addSublayer(borderShapeLayer)

        // 4. Indicator View (Left aligned or shelf centered)
        indicatorView.frame = NSRect(x: 18, y: 22, width: 20, height: 20)
        addSubview(indicatorView)

        // 5. Persona Badge (SpacePilot Gold semibold "Donna" badge)
        personaBadge.isEditable = false
        personaBadge.isSelectable = false
        personaBadge.isBordered = false
        personaBadge.drawsBackground = false
        personaBadge.font = NSFont.systemFont(ofSize: 11.5, weight: .bold)
        personaBadge.textColor = NSColor(srgbRed: 0xc9 / 255.0, green: 0xa2 / 255.0, blue: 0x27 / 255.0, alpha: 1.0) // SpacePilot Gold
        personaBadge.stringValue = "Donna"
        personaBadge.isHidden = true
        addSubview(personaBadge)

        // 6. Status & Dictation Typography
        labelField.isEditable = false
        labelField.isSelectable = false
        labelField.isBordered = false
        labelField.drawsBackground = false
        labelField.font = NSFont.systemFont(ofSize: 12.5, weight: .medium)
        labelField.textColor = NSColor(srgbRed: 0xf3 / 255.0, green: 0xf4 / 255.0, blue: 0xf6 / 255.0, alpha: 1.0)
        labelField.alignment = .left
        labelField.lineBreakMode = .byTruncatingTail
        labelField.cell?.truncatesLastVisibleLine = true
        labelField.maximumNumberOfLines = 1
        labelField.usesSingleLineMode = true
        labelField.stringValue = "Listening..."
        addSubview(labelField)

        updateShapePath(width: bounds.width, height: bounds.height)
        layoutSubviews(forWidth: bounds.width, height: bounds.height)
    }

    /// Construct Apple-grade Dynamic Island Bezier path:
    /// - Continuous squircle bottom corners (r=20px).
    /// - Top edge flush with display bezel (y=0).
    /// - Concave ear fillets (r=10px) that sweep outward smoothly into the top bezel when expanded.
    private func createDynamicIslandPath(width: CGFloat, height: CGFloat) -> CGPath {
        let path = CGMutablePath()
        let bottomRadius: CGFloat = 20.0
        let notchInfo = NotchManager.shared.currentNotch()

        if !notchInfo.hasNotch {
            // External monitor: symmetrical floating pill
            let radius = min(Self.capsuleRadius, height / 2.0)
            path.addRoundedRect(
                in: CGRect(x: 0, y: 0, width: width, height: height),
                cornerWidth: radius,
                cornerHeight: radius
            )
            return path
        }

        let earRadius: CGFloat = (width > notchInfo.notchWidth + 24.0) ? 10.0 : 0.0

        if earRadius > 0 {
            // Left ear concave swoop into top bezel
            path.move(to: CGPoint(x: 0, y: 0))
            path.addQuadCurve(
                to: CGPoint(x: earRadius, y: earRadius),
                control: CGPoint(x: earRadius, y: 0)
            )
            path.addLine(to: CGPoint(x: earRadius, y: height - bottomRadius))
        } else {
            path.move(to: CGPoint(x: 0, y: 0))
            path.addLine(to: CGPoint(x: 0, y: height - bottomRadius))
        }

        // Bottom-left corner
        let leftBottomX = earRadius > 0 ? earRadius : 0.0
        path.addArc(
            tangent1End: CGPoint(x: leftBottomX, y: height),
            tangent2End: CGPoint(x: leftBottomX + bottomRadius, y: height),
            radius: bottomRadius
        )

        // Bottom horizontal shelf
        let rightBottomX = earRadius > 0 ? (width - earRadius) : width
        path.addLine(to: CGPoint(x: rightBottomX - bottomRadius, y: height))

        // Bottom-right corner
        path.addArc(
            tangent1End: CGPoint(x: rightBottomX, y: height),
            tangent2End: CGPoint(x: rightBottomX, y: height - bottomRadius),
            radius: bottomRadius
        )

        if earRadius > 0 {
            path.addLine(to: CGPoint(x: width - earRadius, y: earRadius))
            // Right ear concave swoop into top bezel
            path.addQuadCurve(
                to: CGPoint(x: width, y: 0),
                control: CGPoint(x: width - earRadius, y: 0)
            )
        } else {
            path.addLine(to: CGPoint(x: width, y: 0))
        }

        path.closeSubpath()
        return path
    }

    public func updateShapePath(width: CGFloat, height: CGFloat) {
        let cgPath = createDynamicIslandPath(width: width, height: height)

        maskShapeLayer.path = cgPath
        self.layer?.mask = maskShapeLayer

        borderShapeLayer.path = cgPath
        obsidianBackgroundLayer.frame = CGRect(x: 0, y: 0, width: width, height: height)
        visualEffectView.frame = NSRect(x: 0, y: 0, width: width, height: height)
    }

    public func layoutSubviews(forWidth width: CGFloat, height: CGFloat) {
        self.currentWidth = width
        self.currentHeight = height
        self.frame = NSRect(x: 0, y: 0, width: width, height: height)

        updateShapePath(width: width, height: height)

        let isExpanded = width > (Self.capsuleWidth + 40.0)

        if isExpanded {
            // Expanded wings layout (flanking the notch core)
            personaBadge.isHidden = false
            personaBadge.frame = NSRect(x: 22, y: height - 32, width: 44, height: 18)
            indicatorView.frame = NSRect(x: 70, y: height - 33, width: 18, height: 18)
            labelField.frame = NSRect(x: 96, y: height - 32, width: width - 118, height: 20)
        } else {
            // Compact shelf layout right beneath the camera lens
            personaBadge.isHidden = true
            let shelfY = height - 28.0
            indicatorView.frame = NSRect(x: 18, y: shelfY, width: 18, height: 18)
            labelField.frame = NSRect(x: 44, y: shelfY, width: width - 56, height: 20)
        }
    }

    public func update(state: HUDState) {
        indicatorView.configure(for: state)
        labelField.stringValue = state.labelText
        labelField.textColor = NSColor(srgbRed: 0xf3 / 255.0, green: 0xf4 / 255.0, blue: 0xf6 / 255.0, alpha: 1.0)
    }

    public func setTranscribedText(_ text: String, persona: String = "Donna") {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)

        let attr = NSMutableAttributedString()
        let prefixAttr: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12.0, weight: .bold),
            .foregroundColor: NSColor(srgbRed: 0xc9 / 255.0, green: 0xa2 / 255.0, blue: 0x27 / 255.0, alpha: 1.0) // SpacePilot Gold
        ]
        let textAttr: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12.5, weight: .medium),
            .foregroundColor: NSColor(srgbRed: 0xf3 / 255.0, green: 0xf4 / 255.0, blue: 0xf6 / 255.0, alpha: 1.0) // Zinc White
        ]

        let prefixStr = "[\(persona) heard]: "
        attr.append(NSAttributedString(string: prefixStr, attributes: prefixAttr))
        attr.append(NSAttributedString(string: "\"\(cleanText)\"", attributes: textAttr))

        labelField.attributedStringValue = attr
        labelField.toolTip = "[\(persona) heard]: \"\(cleanText)\""
    }

    public func reset() {
        let notchInfo = NotchManager.shared.currentNotch()
        let defaultH = notchInfo.hasNotch ? Self.notchListeningHeight : Self.capsuleHeight
        layoutSubviews(forWidth: Self.capsuleWidth, height: defaultH)
        labelField.stringValue = "Listening..."
        labelField.textColor = NSColor(srgbRed: 0xf3 / 255.0, green: 0xf4 / 255.0, blue: 0xf6 / 255.0, alpha: 1.0)
    }
}

// MARK: - HUDPanel (Native Non-Activating NSPanel)

open class HUDPanel: NSPanel {
    public let capsuleView = HUDCapsuleView()

    public init() {
        let contentRect = NSRect(
            x: 0,
            y: 0,
            width: HUDCapsuleView.capsuleWidth,
            height: HUDCapsuleView.notchListeningHeight
        )

        super.init(
            contentRect: contentRect,
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )

        // Window specifications:
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.ignoresMouseEvents = true
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = true
        self.isMovableByWindowBackground = false
        self.isReleasedWhenClosed = false

        self.contentView = capsuleView
    }

    // Crucial: Never steal keyboard focus or become key/main window
    open override var canBecomeKey: Bool { return false }
    open override var canBecomeMain: Bool { return false }
}

// MARK: - HUDController Singleton

public class HUDController {
    public static let shared = HUDController()

    public let panel = HUDPanel()
    public private(set) var currentState: HUDState?
    public private(set) var currentWidth: CGFloat = HUDCapsuleView.capsuleWidth
    public private(set) var currentHeight: CGFloat = HUDCapsuleView.notchListeningHeight
    public private(set) var transcribedText: String?
    public private(set) var isVisible: Bool = false

    private init() {}

    /// Present the Dynamic Island with Apple-grade "Drip" spring entrance animation.
    public func show(state: HUDState = .listening) {
        ensureMainThread {
            let notchInfo = NotchManager.shared.currentNotch()
            let targetH = notchInfo.hasNotch ? HUDCapsuleView.notchListeningHeight : HUDCapsuleView.capsuleHeight
            let targetW = HUDCapsuleView.capsuleWidth

            self.currentState = state
            self.transcribedText = nil
            self.currentWidth = targetW
            self.currentHeight = targetH

            self.panel.capsuleView.reset()
            self.panel.capsuleView.update(state: state)

            let finalFrame = self.computeFrame(width: targetW, height: targetH)

            if !self.isVisible {
                self.isVisible = true

                // Start state: tucked at the physical notch height (38px) or 0 alpha
                let startH = notchInfo.hasNotch ? notchInfo.notchHeight : targetH
                let startFrame = self.computeFrame(width: targetW, height: startH)

                self.panel.setFrame(startFrame, display: false)
                self.panel.alphaValue = 0.0
                self.panel.orderFrontRegardless()

                // Apple fluid spring "Drip" entrance: height expands down out of notch
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = 0.22
                    context.timingFunction = CAMediaTimingFunction(controlPoints: 0.25, 0.1, 0.25, 1.0)
                    self.panel.animator().setFrame(finalFrame, display: true)
                    self.panel.animator().alphaValue = 1.0
                    self.panel.capsuleView.layoutSubviews(forWidth: targetW, height: targetH)
                }
                self.panel.invalidateShadow()
            } else {
                self.panel.setFrame(finalFrame, display: true)
                self.panel.capsuleView.layoutSubviews(forWidth: targetW, height: targetH)
                self.panel.invalidateShadow()
            }
        }
    }

    /// Expand Dynamic Island outward for dictation: expands to 440px with continuous ear fillets.
    public func showTranscribedText(_ text: String, persona: String = "Donna") {
        ensureMainThread {
            self.transcribedText = text
            self.panel.capsuleView.setTranscribedText(text, persona: persona)

            let notchInfo = NotchManager.shared.currentNotch()
            let targetH = notchInfo.hasNotch ? HUDCapsuleView.notchExpandedHeight : HUDCapsuleView.capsuleHeight
            let targetW = HUDCapsuleView.expandedWidth

            self.resizeIsland(toWidth: targetW, height: targetH, animated: true)

            if !self.isVisible {
                self.show(state: .thinking)
                self.panel.capsuleView.setTranscribedText(text, persona: persona)
                self.resizeIsland(toWidth: targetW, height: targetH, animated: false)
            }
        }
    }

    /// Update HUD state dynamically (e.g. listening -> thinking -> speaking).
    public func update(state: HUDState) {
        ensureMainThread {
            self.currentState = state
            let notchInfo = NotchManager.shared.currentNotch()

            if let text = self.transcribedText, !text.isEmpty {
                // Keep the transcribed text on screen, only update indicator glyph!
                self.panel.capsuleView.indicatorView.configure(for: state)
                let targetH = notchInfo.hasNotch ? HUDCapsuleView.notchExpandedHeight : HUDCapsuleView.capsuleHeight
                self.resizeIsland(toWidth: HUDCapsuleView.expandedWidth, height: targetH, animated: false)
            } else {
                self.panel.capsuleView.update(state: state)
                let targetH = notchInfo.hasNotch ? HUDCapsuleView.notchListeningHeight : HUDCapsuleView.capsuleHeight
                self.resizeIsland(toWidth: HUDCapsuleView.capsuleWidth, height: targetH, animated: true)
            }
            if !self.isVisible {
                self.show(state: state)
            }
        }
    }

    /// Fluidly morph island width and height with Apple-grade spring physics.
    public func resizeIsland(toWidth newWidth: CGFloat, height newHeight: CGFloat, animated: Bool = true) {
        guard self.currentWidth != newWidth || self.currentHeight != newHeight else { return }
        self.currentWidth = newWidth
        self.currentHeight = newHeight

        let newFrame = computeFrame(width: newWidth, height: newHeight)

        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.24
                context.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
                self.panel.animator().setFrame(newFrame, display: true)
                self.panel.capsuleView.layoutSubviews(forWidth: newWidth, height: newHeight)
            }
        } else {
            self.panel.setFrame(newFrame, display: true)
            self.panel.capsuleView.layoutSubviews(forWidth: newWidth, height: newHeight)
        }
        self.panel.invalidateShadow()
    }

    /// Dismiss the HUD with a snappy suction retraction back into the physical notch.
    public func dismiss(completion: (() -> Void)? = nil) {
        ensureMainThread {
            guard self.isVisible else {
                completion?()
                return
            }

            let notchInfo = NotchManager.shared.currentNotch()
            let retractH = notchInfo.hasNotch ? notchInfo.notchHeight : self.currentHeight
            let retractFrame = self.computeFrame(width: HUDCapsuleView.capsuleWidth, height: retractH)

            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.18
                context.timingFunction = CAMediaTimingFunction(name: .easeIn)
                // Retract upward into the notch and fade out
                self.panel.animator().setFrame(retractFrame, display: true)
                self.panel.animator().alphaValue = 0.0
            }, completionHandler: {
                if self.panel.alphaValue == 0.0 {
                    self.panel.orderOut(nil)
                    self.isVisible = false
                    self.currentState = nil
                    self.transcribedText = nil
                    self.currentWidth = HUDCapsuleView.capsuleWidth
                    self.currentHeight = notchInfo.hasNotch ? HUDCapsuleView.notchListeningHeight : HUDCapsuleView.capsuleHeight
                    self.panel.capsuleView.reset()
                }
                completion?()
            })
        }
    }

    /// Compute frame based on active screen notch geometry:
    /// - If hardware notch exists: anchors flush to screen.frame.maxY, centered on the notch.
    /// - If no notch: centers horizontally on visibleFrame and floats 12px below menu bar.
    public func computeFrame(width: CGFloat, height: CGFloat) -> NSRect {
        let screen = NSScreen.main ?? (NSScreen.screens.first ?? NSScreen())
        let notchInfo = NotchManager.shared.currentNotch(for: screen)

        if notchInfo.hasNotch {
            let x = notchInfo.rect.midX - (width / 2.0)
            let y = notchInfo.screenFrame.maxY - height
            return NSRect(x: x, y: y, width: width, height: height)
        } else {
            let x = notchInfo.visibleFrame.midX - (width / 2.0)
            let y = notchInfo.visibleFrame.maxY - height - 12.0
            return NSRect(x: x, y: y, width: width, height: height)
        }
    }

    private func ensureMainThread(_ block: @escaping () -> Void) {
        if Thread.isMainThread {
            block()
        } else {
            DispatchQueue.main.async(execute: block)
        }
    }

    /// Export specification metadata for automated test assertions.
    public func getSpecificationSummary() -> [String: Any] {
        let notchInfo = NotchManager.shared.currentNotch()
        return [
            "width": HUDCapsuleView.capsuleWidth,
            "expandedWidth": HUDCapsuleView.expandedWidth,
            "currentWidth": currentWidth,
            "height": HUDCapsuleView.capsuleHeight,
            "notchHeight": currentHeight,
            "cornerRadius": HUDCapsuleView.capsuleRadius,
            "isNonactivatingPanel": panel.styleMask.contains(.nonactivatingPanel),
            "isBorderless": panel.styleMask.contains(.borderless),
            "isFloatingLevel": panel.level == .floating,
            "canJoinAllSpaces": panel.collectionBehavior.contains(.canJoinAllSpaces),
            "fullScreenAuxiliary": panel.collectionBehavior.contains(.fullScreenAuxiliary),
            "ignoresMouseEvents": panel.ignoresMouseEvents,
            "isOpaque": panel.isOpaque,
            "isClearBackground": panel.backgroundColor == .clear,
            "canBecomeKey": panel.canBecomeKey,
            "canBecomeMain": panel.canBecomeMain,
            "hasNotch": notchInfo.hasNotch,
            "notchWidth": notchInfo.notchWidth,
            "transcribedText": transcribedText ?? ""
        ]
    }
}
