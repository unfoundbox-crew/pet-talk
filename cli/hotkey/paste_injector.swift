// cli/hotkey/paste_injector.swift
// Cursor Paste Injection (Wispr Flow style) for Pet-Talk.
//
// Technical Architecture & Specifications:
// - Copies transcribed speech text into NSPasteboard.general.
// - Simulates Cmd+V via CGEvent virtual key 9 (kVK_ANSI_V) with .maskCommand flag.
// - Delivers instant, non-blocking text typing into whatever application has focus (Cursor, terminal, editor).
// - Sub-50ms execution: writes pasteboard immediately (<1ms) and fires Cmd+V after 30ms WindowServer debounce.
// - Supports optional clipboard restoration: restores previous clipboard contents after paste if configured.
// - Supports fallback direct unicode keystroke injection via CGEvent keyboardSetUnicodeString.

import AppKit
import Carbon
import CoreGraphics
import Foundation

public class PasteInjector {
    public static let shared = PasteInjector()

    private init() {}

    /// Inject text into the active cursor position (Wispr Flow style).
    /// Copies text to general pasteboard and simulates Cmd+V in the focused application.
    @discardableResult
    public func inject(
        _ text: String,
        restoreClipboard: Bool = false,
        delayMs: Int = 30
    ) -> Bool {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else { return false }

        let pasteboard = NSPasteboard.general
        let previousString: String? = restoreClipboard ? pasteboard.string(forType: .string) : nil

        pasteboard.clearContents()
        pasteboard.setString(cleanText, forType: .string)

        let delaySec = max(0.005, Double(delayMs) / 1000.0)
        DispatchQueue.global(qos: .userInteractive).asyncAfter(deadline: .now() + delaySec) {
            self.simulateCmdV()

            if restoreClipboard, let prev = previousString {
                DispatchQueue.global(qos: .userInteractive).asyncAfter(deadline: .now() + 0.35) {
                    pasteboard.clearContents()
                    pasteboard.setString(prev, forType: .string)
                }
            }
        }
        return true
    }

    /// Simulate Cmd+V keystroke to paste clipboard into active cursor target.
    public func simulateCmdV() {
        let vKeyCode: CGKeyCode = 9 // kVK_ANSI_V = 9
        let source = CGEventSource(stateID: .combinedSessionState)

        if let keyDown = CGEvent(keyboardEventSource: source, virtualKey: vKeyCode, keyDown: true),
           let keyUp = CGEvent(keyboardEventSource: source, virtualKey: vKeyCode, keyDown: false) {
            keyDown.flags = .maskCommand
            keyUp.flags = .maskCommand

            // Post to both session and HID event taps for maximum compatibility across macOS versions
            keyDown.post(tap: .cghidEventTap)
            keyUp.post(tap: .cghidEventTap)
            keyDown.post(tap: .cgSessionEventTap)
            keyUp.post(tap: .cgSessionEventTap)
        } else {
            // Fallback via AppleScript System Events
            simulateCmdVViaAppleScript()
        }
    }

    /// Fallback Cmd+V simulation using AppleScript System Events.
    private func simulateCmdVViaAppleScript() {
        let script = NSAppleScript(source: "tell application \"System Events\" to keystroke \"v\" using command down")
        var error: NSDictionary?
        script?.executeAndReturnError(&error)
    }

    /// Direct keystroke emission fallback: types unicode characters sequentially via CGEvent.
    public func injectKeystrokes(_ text: String) {
        let utf16Chars = Array(text.utf16)
        let source = CGEventSource(stateID: .combinedSessionState)
        for var char in utf16Chars {
            if let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
               let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) {
                keyDown.keyboardSetUnicodeString(stringLength: 1, unicodeString: &char)
                keyUp.keyboardSetUnicodeString(stringLength: 1, unicodeString: &char)
                keyDown.post(tap: .cgSessionEventTap)
                keyUp.post(tap: .cgSessionEventTap)
            }
            usleep(1500) // 1.5ms interval between keystrokes
        }
    }
}
