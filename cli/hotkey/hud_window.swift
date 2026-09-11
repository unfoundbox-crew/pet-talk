// cli/hotkey/hud_window.swift
// Native macOS Floating Glass Capsule HUD for Pet-Talk.
//
// Technical Architecture & Specifications:
// - Non-activating NSPanel subclass: [.nonactivatingPanel, .borderless]
// - Never steals keyboard focus from active editor/IDE/terminal.
// - Level: .floating (floats above all standard windows).
// - Spaces: [.canJoinAllSpaces, .fullScreenAuxiliary] (visible on full-screen apps and all spaces).
// - Click-Through: ignoresMouseEvents = true.
// - Surfaces: Obsidian Deep Zinc (#12141c @ 85% opacity, NSVisualEffectView glass, #282c3f 1px border).
// - Dimensions: 220px width x 44px height, corner radius 22px.
// - Accents & States:
//     [LISTENING] -> Emerald True (#10b981) pulsating waveform + "Listening..."
//     [THINKING]  -> SpacePilot Gold (#c9a227) spinning indicator + "Donna thinking..."
//     [SPEAKING]  -> Liquid Silver (#cfd4dc) kinetic audio bars + "Speaking..."
// - Motion: 120ms spring entrance (scale 0.95 -> 1.0), 200ms ease-out fade exit.

import AppKit
import Foundation
import QuartzCore

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

// MARK: - Indicator View (Kinetic Glyphs)

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

        let startX: CGFloat = (bounds.width - (CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * spacing)) / 2.0
        let centerY = bounds.height / 2.0

        for (index, cfg) in heights.enumerated() {
            let barLayer = CALayer()
            let x = startX + CGFloat(index) * (barWidth + spacing)
            barLayer.frame = CGRect(x: x, y: centerY - cfg.initial / 2.0, width: barWidth, height: cfg.initial)
            barLayer.cornerRadius = barWidth / 2.0
            barLayer.backgroundColor = color.cgColor

            // Soft glowing pulse
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

// MARK: - Capsule Content View (Obsidian Zinc Glass)

public class HUDCapsuleView: NSView {
    public static let capsuleWidth: CGFloat = 220.0
    public static let expandedWidth: CGFloat = 380.0
    public static let capsuleHeight: CGFloat = 44.0
    public static let capsuleRadius: CGFloat = 22.0

    private let visualEffectView = NSVisualEffectView()
    private let zincOverlayView = NSView()
    public let indicatorView = HUDIndicatorView()
    public let labelField = NSTextField()

    public override init(frame frameRect: NSRect) {
        super.init(frame: NSRect(x: 0, y: 0, width: Self.capsuleWidth, height: Self.capsuleHeight))
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
        rootLayer.cornerRadius = Self.capsuleRadius

        // 1. Frosted glass backdrop (.behindWindow)
        visualEffectView.frame = bounds
        visualEffectView.autoresizingMask = [.width, .height]
        visualEffectView.material = .hudWindow
        visualEffectView.blendingMode = .behindWindow
        visualEffectView.state = .active
        visualEffectView.wantsLayer = true
        visualEffectView.layer?.cornerRadius = Self.capsuleRadius
        visualEffectView.layer?.masksToBounds = true
        addSubview(visualEffectView)

        // 2. Obsidian Deep Zinc surface overlay (#12141c @ 85% opacity, 1px border #282c3f)
        zincOverlayView.frame = bounds
        zincOverlayView.autoresizingMask = [.width, .height]
        zincOverlayView.wantsLayer = true
        if let zincLayer = zincOverlayView.layer {
            zincLayer.cornerRadius = Self.capsuleRadius
            zincLayer.masksToBounds = true
            zincLayer.backgroundColor = NSColor(
                srgbRed: 0x12 / 255.0,
                green: 0x14 / 255.0,
                blue: 0x1c / 255.0,
                alpha: 0.85
            ).cgColor
            zincLayer.borderColor = NSColor(
                srgbRed: 0x28 / 255.0,
                green: 0x2c / 255.0,
                blue: 0x3f / 255.0,
                alpha: 0.90
            ).cgColor
            zincLayer.borderWidth = 1.0
        }
        addSubview(zincOverlayView)

        // 3. Indicator Glyph Container (20x20px, left margin 16px)
        indicatorView.frame = NSRect(x: 16, y: 12, width: 20, height: 20)
        addSubview(indicatorView)

        // 4. Status Typography
        labelField.frame = NSRect(x: 44, y: 11, width: 162, height: 22)
        labelField.isEditable = false
        labelField.isSelectable = false
        labelField.isBordered = false
        labelField.drawsBackground = false
        labelField.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        labelField.textColor = NSColor(srgbRed: 0xf3 / 255.0, green: 0xf4 / 255.0, blue: 0xf6 / 255.0, alpha: 1.0)
        labelField.alignment = .left
        labelField.lineBreakMode = .byTruncatingTail
        labelField.cell?.truncatesLastVisibleLine = true
        labelField.maximumNumberOfLines = 1
        labelField.usesSingleLineMode = true
        labelField.stringValue = "Listening..."
        addSubview(labelField)
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
            .font: NSFont.systemFont(ofSize: 12.5, weight: .semibold),
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

    public func layoutSubviews(forWidth width: CGFloat) {
        self.frame = NSRect(x: 0, y: 0, width: width, height: Self.capsuleHeight)
        visualEffectView.frame = NSRect(x: 0, y: 0, width: width, height: Self.capsuleHeight)
        zincOverlayView.frame = NSRect(x: 0, y: 0, width: width, height: Self.capsuleHeight)
        labelField.frame = NSRect(x: 44, y: 11, width: width - 58, height: 22)
    }

    public func reset() {
        layoutSubviews(forWidth: Self.capsuleWidth)
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
            height: HUDCapsuleView.capsuleHeight
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
    public private(set) var transcribedText: String?
    public private(set) var isVisible: Bool = false

    private init() {}

    /// Present the HUD capsule with a 120ms spring entrance animation.
    public func show(state: HUDState = .listening) {
        ensureMainThread {
            self.currentState = state
            self.transcribedText = nil
            self.currentWidth = HUDCapsuleView.capsuleWidth
            self.panel.capsuleView.reset()
            self.panel.capsuleView.update(state: state)
            self.positionWindow(width: HUDCapsuleView.capsuleWidth)

            if !self.isVisible {
                self.isVisible = true
                self.panel.alphaValue = 0.0
                self.panel.orderFrontRegardless()

                // 120ms spring entrance (scale 0.95 -> 1.0, alpha 0.0 -> 1.0)
                if let layer = self.panel.capsuleView.layer {
                    let spring = CASpringAnimation(keyPath: "transform")
                    var startTransform = CATransform3DIdentity
                    startTransform = CATransform3DTranslate(startTransform, HUDCapsuleView.capsuleWidth / 2.0, HUDCapsuleView.capsuleHeight / 2.0, 0)
                    startTransform = CATransform3DScale(startTransform, 0.95, 0.95, 1.0)
                    startTransform = CATransform3DTranslate(startTransform, -HUDCapsuleView.capsuleWidth / 2.0, -HUDCapsuleView.capsuleHeight / 2.0, 0)

                    spring.fromValue = NSValue(caTransform3D: startTransform)
                    spring.toValue = NSValue(caTransform3D: CATransform3DIdentity)
                    spring.duration = 0.12
                    spring.damping = 16.0
                    spring.initialVelocity = 4.0
                    spring.isRemovedOnCompletion = true
                    layer.add(spring, forKey: "springEntrance")
                }

                NSAnimationContext.runAnimationGroup { context in
                    context.duration = 0.12
                    context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                    self.panel.animator().alphaValue = 1.0
                }
            }
        }
    }

    /// Display transcribed text: widens capsule to 380px and displays `[Donna heard]: "<transcribed text>"`.
    public func showTranscribedText(_ text: String, persona: String = "Donna") {
        ensureMainThread {
            self.transcribedText = text
            self.panel.capsuleView.setTranscribedText(text, persona: persona)
            self.resizeCapsule(to: HUDCapsuleView.expandedWidth, animated: true)

            if !self.isVisible {
                self.show(state: .thinking)
                self.panel.capsuleView.setTranscribedText(text, persona: persona)
                self.resizeCapsule(to: HUDCapsuleView.expandedWidth, animated: false)
            }
        }
    }

    /// Update HUD state dynamically (e.g. listening -> thinking -> speaking).
    /// If transcribed text has been received, preserves the text display while updating indicator.
    public func update(state: HUDState) {
        ensureMainThread {
            self.currentState = state
            if let text = self.transcribedText, !text.isEmpty {
                // Keep the transcribed text on screen, only update indicator glyph!
                self.panel.capsuleView.indicatorView.configure(for: state)
                self.resizeCapsule(to: HUDCapsuleView.expandedWidth, animated: false)
            } else {
                self.panel.capsuleView.update(state: state)
                self.resizeCapsule(to: HUDCapsuleView.capsuleWidth, animated: true)
            }
            if !self.isVisible {
                self.show(state: state)
            }
        }
    }

    /// Resize capsule width smoothly and keep it centered horizontally.
    public func resizeCapsule(to newWidth: CGFloat, animated: Bool = true) {
        guard self.currentWidth != newWidth else { return }
        self.currentWidth = newWidth

        let screen = NSScreen.main ?? (NSScreen.screens.first ?? NSScreen())
        let screenFrame = screen.visibleFrame
        let x = screenFrame.midX - (newWidth / 2.0)
        let y = screenFrame.maxY - HUDCapsuleView.capsuleHeight - 24.0
        let newFrame = NSRect(x: x, y: y, width: newWidth, height: HUDCapsuleView.capsuleHeight)

        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.20
                context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                self.panel.animator().setFrame(newFrame, display: true)
                self.panel.capsuleView.layoutSubviews(forWidth: newWidth)
            }
        } else {
            self.panel.setFrame(newFrame, display: true)
            self.panel.capsuleView.layoutSubviews(forWidth: newWidth)
        }
        self.panel.invalidateShadow()
    }

    /// Dismiss the HUD capsule with a smooth 200ms ease-out fade.
    public func dismiss(completion: (() -> Void)? = nil) {
        ensureMainThread {
            guard self.isVisible else {
                completion?()
                return
            }

            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.20
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                self.panel.animator().alphaValue = 0.0
            }, completionHandler: {
                if self.panel.alphaValue == 0.0 {
                    self.panel.orderOut(nil)
                    self.isVisible = false
                    self.currentState = nil
                    self.transcribedText = nil
                    self.currentWidth = HUDCapsuleView.capsuleWidth
                    self.panel.capsuleView.reset()
                    self.positionWindow(width: HUDCapsuleView.capsuleWidth)
                }
                completion?()
            })
        }
    }

    /// Position capsule centered horizontally, floating gracefully below menu bar.
    private func positionWindow(width: CGFloat = HUDCapsuleView.capsuleWidth) {
        let screen = NSScreen.main ?? (NSScreen.screens.first ?? NSScreen())
        let screenFrame = screen.visibleFrame
        let x = screenFrame.midX - (width / 2.0)
        let y = screenFrame.maxY - HUDCapsuleView.capsuleHeight - 24.0
        panel.setFrame(NSRect(x: x, y: y, width: width, height: HUDCapsuleView.capsuleHeight), display: false)
        panel.invalidateShadow()
    }

    private func ensureMainThread(_ block: @escaping () -> Void) {
        if Thread.isMainThread {
            block()
        } else {
            DispatchQueue.main.async(execute: block)
        }
    }

    /// Export specification introspection metadata for automated test assertions.
    public func getSpecificationSummary() -> [String: Any] {
        return [
            "width": HUDCapsuleView.capsuleWidth,
            "expandedWidth": HUDCapsuleView.expandedWidth,
            "currentWidth": currentWidth,
            "height": HUDCapsuleView.capsuleHeight,
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
            "transcribedText": transcribedText ?? ""
        ]
    }
}
