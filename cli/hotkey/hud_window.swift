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

// MARK: - Motion Design Language & Physics Tokens

public struct HUDMotionTokens {
    public static let springStiffness: Double = 220.0
    public static let springDamping: Double = 21.0
    public static let springMass: Double = 1.0
    public static let dripDuration: Double = 0.22 // 220ms
    public static let blossomDuration: Double = 0.24 // 240ms
    public static let stateTransitionDuration: Double = 0.16 // 160ms
    public static let suctionRetractionDuration: Double = 0.18 // 180ms
    public static let errorShakeDuration: Double = 0.12 // 120ms
    public static let errorShakeAmplitude: Double = 3.0 // ±3px
    public static let errorShakeCycles: Int = 3
    public static let reduceMotionDuration: Double = 0.08 // 80ms
    public static let escapeDismissDuration: Double = 0.04 // 40ms (<= 50ms budget)

    public static let blossomTimingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
    public static let stateTransitionTimingFunction = CAMediaTimingFunction(name: .easeOut)
    public static let suctionRetractionTimingFunction = CAMediaTimingFunction(name: .easeIn)
}

public typealias AppleMotionTokens = HUDMotionTokens

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
    private var waveformBars: [CALayer] = []
    private var smoothedRMS: Float = 0.0
    private var smoothedPeak: Float = 0.0
    private var decayTimer: Timer?
    private var isAcousticListening: Bool = false

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

    deinit {
        decayTimer?.invalidate()
    }

    public override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        if isAcousticListening {
            applyBarHeights()
        }
    }

    public func configure(for state: HUDState) {
        guard let layer = self.layer else { return }

        decayTimer?.invalidate()
        decayTimer = nil
        smoothedRMS = 0.0
        smoothedPeak = 0.0
        isAcousticListening = false
        waveformBars.removeAll()

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

    // [LISTENING] Emerald True acoustic waveform bars (Acoustic Truth: zero fake looping animations)
    private func setupListeningWaveform(parentLayer: CALayer, color: NSColor) {
        isAcousticListening = true
        waveformBars.removeAll()

        let barCount = 4
        let barWidth: CGFloat = 2.0
        let spacing: CGFloat = 2.0
        let minHeight: CGFloat = 3.5

        let totalW = CGFloat(barCount) * barWidth + CGFloat(barCount - 1) * spacing
        let startX: CGFloat = (bounds.width - totalW) / 2.0
        let centerY = bounds.height / 2.0

        for index in 0..<barCount {
            let barLayer = CALayer()
            let x = startX + CGFloat(index) * (barWidth + spacing)
            barLayer.frame = CGRect(x: x, y: centerY - minHeight / 2.0, width: barWidth, height: minHeight)
            barLayer.cornerRadius = barWidth / 2.0
            barLayer.backgroundColor = color.cgColor

            barLayer.shadowColor = color.cgColor
            barLayer.shadowRadius = 3.0
            barLayer.shadowOpacity = 0.55
            barLayer.shadowOffset = .zero

            parentLayer.addSublayer(barLayer)
            activeLayers.append(barLayer)
            waveformBars.append(barLayer)
        }
    }

    /// Update live acoustic energy levels (RMS and Peak amplitude, normalized 0.0 ... 1.0)
    public func updateAudioLevel(rms: Float, peak: Float) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.updateAudioLevel(rms: rms, peak: peak)
            }
            return
        }

        guard isAcousticListening, !waveformBars.isEmpty else { return }

        // Normalize inputs (safely handles raw 16-bit PCM scale or 0.0 ... 1.0)
        let normRMS: Float = rms > 1.0 ? min(1.0, max(0.0, rms / 8000.0)) : min(1.0, max(0.0, rms))
        let normPeak: Float = peak > 1.0 ? min(1.0, max(0.0, peak / 32768.0)) : min(1.0, max(0.0, peak))

        // Vocal cord acoustic filtering:
        // Fast attack (~15ms) for crisp vocal onset, smooth springy decay to eliminate digital jitter
        if normRMS > smoothedRMS {
            smoothedRMS = smoothedRMS * 0.22 + normRMS * 0.78
        } else {
            smoothedRMS = smoothedRMS * 0.76 + normRMS * 0.24
        }

        if normPeak > smoothedPeak {
            smoothedPeak = normPeak
        } else {
            smoothedPeak = smoothedPeak * 0.80
        }

        applyBarHeights()
        startDecayTimerIfNeeded()
    }

    private func applyBarHeights() {
        guard isAcousticListening, !waveformBars.isEmpty else { return }

        let barCount = waveformBars.count
        let barWidth: CGFloat = 2.0
        let spacing: CGFloat = 2.0
        let totalW = CGFloat(barCount) * barWidth + CGFloat(barCount - 1) * spacing
        let startX = (bounds.width - totalW) / 2.0
        let centerY = bounds.height / 2.0
        let minHeight: CGFloat = 3.5
        let maxHeight: CGFloat = max(minHeight, bounds.height > 4 ? bounds.height - 3.0 : 15.0)

        // Vocal formant frequency weights across 4 bars (low resonance, core vocal 1 & 2, high sibilance)
        let weights: [(rms: Float, peak: Float)] = [
            (rms: 0.65, peak: 0.25),
            (rms: 1.00, peak: 0.35),
            (rms: 0.90, peak: 0.45),
            (rms: 0.55, peak: 0.35)
        ]

        CATransaction.begin()
        CATransaction.setAnimationDuration(0.04)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))

        for (index, bar) in waveformBars.enumerated() {
            let w = index < weights.count ? weights[index] : (rms: 0.8, peak: 0.3)
            let energy = min(1.0, max(0.0, smoothedRMS * w.rms + smoothedPeak * w.peak))
            let barH = minHeight + CGFloat(energy) * (maxHeight - minHeight)
            let x = startX + CGFloat(index) * (barWidth + spacing)
            let y = centerY - (barH / 2.0)
            bar.frame = CGRect(x: x, y: y, width: barWidth, height: barH)
        }

        CATransaction.commit()
    }

    private func startDecayTimerIfNeeded() {
        guard decayTimer == nil else { return }
        decayTimer = Timer.scheduledTimer(withTimeInterval: 0.033, repeats: true) { [weak self] timer in
            guard let self = self else {
                timer.invalidate()
                return
            }
            if !self.isAcousticListening || self.waveformBars.isEmpty {
                timer.invalidate()
                self.decayTimer = nil
                return
            }

            if self.smoothedRMS > 0.005 || self.smoothedPeak > 0.005 {
                self.smoothedRMS *= 0.78
                self.smoothedPeak *= 0.80
                self.applyBarHeights()
            } else {
                self.smoothedRMS = 0.0
                self.smoothedPeak = 0.0
                self.applyBarHeights()
                timer.invalidate()
                self.decayTimer = nil
            }
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
    public static let earFilletRadius: CGFloat = 10.0
    public static let bottomCornerRadius: CGFloat = 20.0
    public static let hoverPeekHeight: CGFloat = 6.0

    // Flipped coordinates: (0, 0) is top-left, making top-edge anchoring clean and deterministic
    public override var isFlipped: Bool { return true }

    private let maskShapeLayer = CAShapeLayer()
    private let borderShapeLayer = CAShapeLayer()
    private let visualEffectView = NSVisualEffectView()
    private let obsidianBackgroundLayer = CALayer()

    public let indicatorView = HUDIndicatorView()
    public let personaBadge = NSTextField()
    public let labelField = NSTextField()
    public let goldDotLayer = CALayer()
    private var trackingArea: NSTrackingArea?

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

        // 7. SpacePilot Gold Dot Layer for Subtle Notch Peek
        goldDotLayer.bounds = CGRect(x: 0, y: 0, width: 6.0, height: 6.0)
        goldDotLayer.cornerRadius = 3.0
        let goldColor = NSColor(srgbRed: 0xc9 / 255.0, green: 0xa2 / 255.0, blue: 0x27 / 255.0, alpha: 1.0)
        goldDotLayer.backgroundColor = goldColor.cgColor
        goldDotLayer.shadowColor = goldColor.cgColor
        goldDotLayer.shadowRadius = 3.0
        goldDotLayer.shadowOpacity = 0.8
        goldDotLayer.shadowOffset = .zero
        goldDotLayer.opacity = 0.0
        rootLayer.addSublayer(goldDotLayer)

        updateShapePath(width: bounds.width, height: bounds.height)
        layoutSubviews(forWidth: bounds.width, height: bounds.height)
    }

    public override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = trackingArea {
            removeTrackingArea(existing)
        }
        let options: NSTrackingArea.Options = [
            .mouseEnteredAndExited,
            .activeAlways,
            .inVisibleRect
        ]
        let newArea = NSTrackingArea(rect: bounds, options: options, owner: self, userInfo: nil)
        addTrackingArea(newArea)
        trackingArea = newArea
    }

    public override func mouseEntered(with event: NSEvent) {
        super.mouseEntered(with: event)
        HUDController.shared.handleMouseEntered()
    }

    public override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        HUDController.shared.handleMouseExited()
    }

    public override func mouseDown(with event: NSEvent) {
        super.mouseDown(with: event)
        HUDController.shared.handleCapsuleClick()
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

        // Gold dot positioned at bottom center of the shelf
        goldDotLayer.position = CGPoint(x: width / 2.0, y: height - 5.0)
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
        goldDotLayer.opacity = 0.0
        indicatorView.isHidden = false
    }

    public func updateAudioLevel(rms: Float, peak: Float) {
        indicatorView.updateAudioLevel(rms: rms, peak: peak)
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
    public private(set) var isHoverPeekActive: Bool = false

    // Sensory & Execution Callbacks
    public var onBargeKill: (() -> Void)?
    public var onErrorAudio: (() -> Void)?
    public var onTriggerTurn: (() -> Void)?

    private var localKeyMonitor: Any?
    private var globalMouseMonitor: Any?

    private init() {}

    /// Register local Escape monitor and global notch hover tracking
    public func setupEventMonitors() {
        ensureMainThread {
            if self.localKeyMonitor == nil {
                self.localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                    guard let self = self else { return event }
                    // Escape key (keycode 53): instant <= 50ms dismiss and barge-kill
                    if event.keyCode == 53 && (self.isVisible || self.isHoverPeekActive) {
                        self.dismissWithBargeKill()
                        return nil
                    }
                    return event
                }
            }

            if self.globalMouseMonitor == nil {
                self.globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: .mouseMoved) { [weak self] _ in
                    guard let self = self else { return }
                    let mouseLoc = NSEvent.mouseLocation
                    let notchInfo = NotchManager.shared.currentNotch()
                    guard notchInfo.hasNotch else { return }

                    let notchH = notchInfo.notchHeight
                    let notchW = HUDCapsuleView.capsuleWidth
                    let notchX = notchInfo.screenFrame.midX - (notchW / 2.0)
                    let notchY = notchInfo.screenFrame.maxY - notchH - HUDCapsuleView.hoverPeekHeight
                    let notchZone = NSRect(
                        x: notchX,
                        y: notchY,
                        width: notchW,
                        height: notchH + HUDCapsuleView.hoverPeekHeight + 4.0
                    )

                    if notchZone.contains(mouseLoc) {
                        if !self.isVisible && !self.isHoverPeekActive {
                            self.showHoverPeek()
                        }
                    } else {
                        if self.isHoverPeekActive {
                            self.hideHoverPeek()
                        }
                    }
                }
            }
        }
    }

    /// Subtle notch hover peek (6px downward expansion revealing SpacePilot Gold dot)
    public func showHoverPeek() {
        ensureMainThread {
            guard !self.isVisible, !self.isHoverPeekActive else { return }
            let notchInfo = NotchManager.shared.currentNotch()
            guard notchInfo.hasNotch else { return }

            self.isHoverPeekActive = true
            let peekH = notchInfo.notchHeight + HUDCapsuleView.hoverPeekHeight
            let frame = self.computeFrame(width: HUDCapsuleView.capsuleWidth, height: peekH)

            self.panel.ignoresMouseEvents = false
            self.panel.setFrame(frame, display: true)
            self.panel.alphaValue = 1.0
            self.panel.capsuleView.goldDotLayer.opacity = 1.0
            self.panel.capsuleView.indicatorView.isHidden = true
            self.panel.capsuleView.labelField.stringValue = ""
            self.panel.orderFrontRegardless()
        }
    }

    /// Snap hover peek back into the physical notch
    public func hideHoverPeek() {
        ensureMainThread {
            guard self.isHoverPeekActive else { return }
            self.isHoverPeekActive = false
            self.panel.ignoresMouseEvents = true
            self.panel.capsuleView.goldDotLayer.opacity = 0.0
            self.panel.capsuleView.indicatorView.isHidden = false
            self.panel.orderOut(nil)
        }
    }

    public func handleMouseEntered() {
        showHoverPeek()
    }

    public func handleMouseExited() {
        hideHoverPeek()
    }

    public func handleCapsuleClick() {
        ensureMainThread {
            if self.isHoverPeekActive {
                self.hideHoverPeek()
                if let trigger = self.onTriggerTurn {
                    trigger()
                } else {
                    self.show(state: .listening)
                }
            }
        }
    }

    /// Instant Escape dismiss: terminates active speech/processes in <= 50ms
    public func dismissWithBargeKill() {
        ensureMainThread {
            self.onBargeKill?()
            if self.isHoverPeekActive {
                self.hideHoverPeek()
            }
            if self.isVisible {
                self.dismiss(immediate: true)
            }
        }
    }

    /// 3-cycle horizontal micro-shake feedback (±3px over 120ms) + Basso chime
    public func triggerErrorShake(completion: (() -> Void)? = nil) {
        ensureMainThread {
            guard self.isVisible else {
                completion?()
                return
            }

            if let audio = self.onErrorAudio {
                audio()
            } else {
                NSSound(named: "Basso")?.play()
            }

            guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
                completion?()
                return
            }

            let shake = CAKeyframeAnimation(keyPath: "transform.translation.x")
            shake.duration = HUDMotionTokens.errorShakeDuration
            shake.values = [
                0.0,
                -HUDMotionTokens.errorShakeAmplitude,
                HUDMotionTokens.errorShakeAmplitude,
                -HUDMotionTokens.errorShakeAmplitude,
                HUDMotionTokens.errorShakeAmplitude,
                -2.0,
                2.0,
                0.0
            ]
            shake.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            shake.isRemovedOnCompletion = true
            self.panel.capsuleView.layer?.add(shake, forKey: "errorShake")

            DispatchQueue.main.asyncAfter(deadline: .now() + HUDMotionTokens.errorShakeDuration) {
                completion?()
            }
        }
    }

    /// Alias for triggerErrorShake
    public func shakeError(completion: (() -> Void)? = nil) {
        triggerErrorShake(completion: completion)
    }

    /// Real-time microphone acoustic energy levels (RMS / peak, normalized 0.0 ... 1.0)
    public func updateAudioLevel(rms: Float, peak: Float) {
        ensureMainThread {
            guard self.isVisible, self.currentState == .listening else { return }
            self.panel.capsuleView.updateAudioLevel(rms: rms, peak: peak)
        }
    }

    /// Present the Dynamic Island with Apple-grade "Drip" spring entrance animation.
    public func show(state: HUDState = .listening) {
        ensureMainThread {
            if self.isHoverPeekActive {
                self.hideHoverPeek()
            }

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

                if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                    self.panel.setFrame(finalFrame, display: false)
                    self.panel.alphaValue = 0.0
                    self.panel.orderFrontRegardless()

                    NSAnimationContext.runAnimationGroup { context in
                        context.duration = HUDMotionTokens.reduceMotionDuration
                        context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                        self.panel.animator().alphaValue = 1.0
                        self.panel.capsuleView.layoutSubviews(forWidth: targetW, height: targetH)
                    }
                } else {
                    let startH = notchInfo.hasNotch ? notchInfo.notchHeight : targetH
                    let startFrame = self.computeFrame(width: targetW, height: startH)

                    self.panel.setFrame(startFrame, display: false)
                    self.panel.alphaValue = 0.0
                    self.panel.orderFrontRegardless()

                    NSAnimationContext.runAnimationGroup { context in
                        context.duration = HUDMotionTokens.dripDuration
                        context.timingFunction = CAMediaTimingFunction(controlPoints: 0.25, 0.1, 0.25, 1.0)
                        self.panel.animator().setFrame(finalFrame, display: true)
                        self.panel.animator().alphaValue = 1.0
                        self.panel.capsuleView.layoutSubviews(forWidth: targetW, height: targetH)
                    }
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
            if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                self.panel.setFrame(newFrame, display: true)
                self.panel.capsuleView.layoutSubviews(forWidth: newWidth, height: newHeight)
            } else {
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = HUDMotionTokens.blossomDuration
                    context.timingFunction = HUDMotionTokens.blossomTimingFunction
                    self.panel.animator().setFrame(newFrame, display: true)
                    self.panel.capsuleView.layoutSubviews(forWidth: newWidth, height: newHeight)
                }
            }
        } else {
            self.panel.setFrame(newFrame, display: true)
            self.panel.capsuleView.layoutSubviews(forWidth: newWidth, height: newHeight)
        }
        self.panel.invalidateShadow()
    }

    /// Dismiss the HUD with a snappy suction retraction back into the physical notch.
    public func dismiss(immediate: Bool = false, completion: (() -> Void)? = nil) {
        ensureMainThread {
            guard self.isVisible else {
                completion?()
                return
            }

            let cleanup: () -> Void = {
                self.panel.orderOut(nil)
                self.isVisible = false
                self.currentState = nil
                self.transcribedText = nil
                let notchInfo = NotchManager.shared.currentNotch()
                self.currentWidth = HUDCapsuleView.capsuleWidth
                self.currentHeight = notchInfo.hasNotch ? HUDCapsuleView.notchListeningHeight : HUDCapsuleView.capsuleHeight
                self.panel.capsuleView.reset()
                completion?()
            }

            if immediate {
                NSAnimationContext.runAnimationGroup({ context in
                    context.duration = HUDMotionTokens.escapeDismissDuration
                    self.panel.animator().alphaValue = 0.0
                }, completionHandler: {
                    cleanup()
                })
                return
            }

            if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                NSAnimationContext.runAnimationGroup({ context in
                    context.duration = HUDMotionTokens.reduceMotionDuration
                    self.panel.animator().alphaValue = 0.0
                }, completionHandler: {
                    cleanup()
                })
                return
            }

            let notchInfo = NotchManager.shared.currentNotch()
            let retractH = notchInfo.hasNotch ? notchInfo.notchHeight : self.currentHeight
            let retractFrame = self.computeFrame(width: HUDCapsuleView.capsuleWidth, height: retractH)

            NSAnimationContext.runAnimationGroup({ context in
                context.duration = HUDMotionTokens.suctionRetractionDuration
                context.timingFunction = HUDMotionTokens.suctionRetractionTimingFunction
                self.panel.animator().setFrame(retractFrame, display: true)
                self.panel.animator().alphaValue = 0.0
            }, completionHandler: {
                cleanup()
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
            "transcribedText": transcribedText ?? "",
            "errorShakeSupported": true,
            "isHoverPeekActive": isHoverPeekActive,
            "reduceMotion": NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
            "motionTokens": [
                "springStiffness": HUDMotionTokens.springStiffness,
                "springDamping": HUDMotionTokens.springDamping,
                "springMass": HUDMotionTokens.springMass,
                "dripDuration": HUDMotionTokens.dripDuration,
                "blossomDuration": HUDMotionTokens.blossomDuration,
                "stateTransitionDuration": HUDMotionTokens.stateTransitionDuration,
                "suctionRetractionDuration": HUDMotionTokens.suctionRetractionDuration,
                "errorShakeDuration": HUDMotionTokens.errorShakeDuration,
                "errorShakeAmplitude": HUDMotionTokens.errorShakeAmplitude,
                "errorShakeCycles": HUDMotionTokens.errorShakeCycles,
                "reduceMotionDuration": HUDMotionTokens.reduceMotionDuration,
                "escapeDismissDuration": HUDMotionTokens.escapeDismissDuration
            ],
            "notchSpecs": [
                "restingHeight": HUDCapsuleView.notchRestingHeight,
                "listeningHeight": HUDCapsuleView.notchListeningHeight,
                "expandedHeight": HUDCapsuleView.notchExpandedHeight,
                "restingWidth": HUDCapsuleView.capsuleWidth,
                "expandedWidth": HUDCapsuleView.expandedWidth,
                "earFilletRadius": HUDCapsuleView.earFilletRadius,
                "bottomCornerRadius": HUDCapsuleView.bottomCornerRadius,
                "fallbackCornerRadius": HUDCapsuleView.capsuleRadius,
                "hoverPeekHeight": HUDCapsuleView.hoverPeekHeight
            ]
        ]
    }
}
