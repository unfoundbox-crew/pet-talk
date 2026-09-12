# CTO Architecture Specification: Hardware Notch Dynamic Island for Pet-Talk

**Document ID**: `SPEC-PET-TALK-004`  
**Status**: `APPROVED / IMPLEMENTATION-ACTIVE`  
**Date**: September 11, 2026  
**Revised**: September 12, 2026 — measured notch width, a real spring, hand-over chord  
**Verified against code**: September 12, 2026 (`cli/hotkey/hud_window.swift`, `cli/hotkey/main.swift`)  
**Target Repository**: `unfoundbox-crew/pet-talk`  

---

## 1. Executive Summary & The Core Inversion

Traditional desktop voice assistants render arbitrary floating windows that fight macOS system chrome. On modern Apple Silicon MacBooks the display features a physical hardware webcam notch. Its size is **measured at runtime**, never assumed: `right.minX - left.maxX` of the screen's auxiliary top areas (220 pt on the machine this was verified on, 2026-09-12). A screen that reports no notch gets a 180 pt floating pill instead.

Following the foundational rule of native macOS island engineering—**"Reflect, Don't Host"**—Donna turns the hardware notch from an obstruction into a **living physical anchor**:

1. **Hardware-Software Fusion**: The top edge is flush with `screen.frame.maxY`, enveloping the camera lens module in pitch black (`#000000`), erasing the boundary between glass and display pixels.
2. **Real spring motion**: `stiffness: 220`, `damping: 21`, `mass: 1` — declared once in `DesignTokens.swift`, read by `HUDTokens`, and actually integrated. Interruptible, skippable, snappy. Measured 2026-09-12: 225 ms to 99 % of travel (the 220 ms drip budget), 3.1 % overshoot, fully settled at 527 ms.
3. **Acoustic Truth**: Replace fake CSS/CoreAnimation loops with **live acoustic energy levels (RMS / FFT)** driven by the physical microphone.
4. **Zero Mock Code**: Native Swift, compiled via `swiftc -O` into `bin/pet-talk-hotkey`. Zero third-party baggage, zero Electron bloat, sub-50ms latency.

---

## 2. The 6-State Lifecycle Machine

```
+-----------------------------------------------------------------------------------+
|                            DYNAMIC ISLAND STATE MACHINE                           |
+-----------------------------------------------------------------------------------+

   [ 1. IDLE / NOTCH TUCKED ]   <--- (Escape = hard cut, or Turn Done)
         |
         +--- [Mouse Hover] --------> [ 2. HOVER PEEK ] (6px subtle gold shelf)
         |                                  |
         | (Option+Tab)                     | (Click / Option+Tab)
         v                                  v
   [ 3. LISTENING ("The Drip") ] <----------+
         | (Height 38px -> 52px, Emerald Waveform, Live RMS bars)
         v
   [ 4. THINKING ("The Pulse") ]
         | (measured notch width, SpacePilot Gold spinning orbit)
         v
   [ 5. SPEAKING / DICTATION ("The Blossom") ]
         | (Expands to 440px x 60px, concave top ear fillets r=10px,
         |  Donna persona tag, transcribed zinc typography, interruptible)
         +-----------------------------+
         |                             |
         | (Turn Complete)             | (Network / STT Error)
         v                             v
   [ 6. SUCTION RETRACTION ]     [ ERROR SHAKE ]
     (Springs up into the notch)   (±3pt horizontal shake, Basso chime)

   Chords: Option+Tab = ask/kill · Option+Tab twice (<=400ms) = pause
           Option+Shift+Tab = hand over · Escape = barge (hard cut)
```

---

## 3. Motion: one spring, two drivers

A `CASpringAnimation` cannot animate an `NSWindow` frame, and the HUD is a window.
So the same three tokens drive two things:

* **`HUDSpringDriver`** — a damped-spring integrator (semi-implicit Euler on
  `a = (-k·x - c·v) / m`, 120 Hz) that reports normalized 0 → 1 progress, overshoot
  included. The panel frame is lerped along it every step. This is what moves the
  capsule.
* **`HUDSpring.animation(...)`** — the one place a `CASpringAnimation` is built
  (stiffness/damping/mass from the tokens, `initialVelocity: 0`). It fades the
  capsule layer's opacity so content and geometry settle together.

| Event | Mechanism | Tokens & constraints |
| :--- | :--- | :--- |
| **Drip entrance** | Frame height notch → 52 pt, spring driver + opacity spring | `stiffness: 220`, `damping: 21`, `mass: 1`; measured 225 ms to 99 %, settles 527 ms |
| **Island blossom** | Width measured-notch → 440 pt, height 52 → 60 pt | same spring, no separate curve |
| **Suction retraction** | Collapses upward into the physical notch as it fades | same spring (`durationSleepRetract` 180 ms is the budget it lands inside) |
| **Barge / Escape** | **Hard cut.** Zero animation, zero spring, panel gone on the same run-loop turn | budget 40 ms, measured as instant (no timer involved) |
| **Error feedback** | `errorShakeCycles` horizontal micro-shakes, decaying | ±3 pt over 120 ms, `Basso.aiff` chime |
| **Reduce Motion** | `NSWorkspace.accessibilityDisplayShouldReduceMotion` | **Zero spatial travel, time kept**: frame snaps, 80 ms crossfade |

Every number above comes from `cli/hotkey/DesignTokens.swift` (generated from
`design/tokens.pet-talk.json`) through `HUDTokens`, and every one of them can be
overridden in `~/.pet-talk/config.yaml` — `motion.spring_stiffness`,
`motion.spring_damping`, `motion.spring_mass`, `geometry.*`,
`hotkey.double_tap_window_ms` — so a playground export applies with no rebuild.

---

## 4. Geometric Specification & Fillet Math

### Notch mode (`hasNotch == true`)
* **Resting width**: the measured notch, `right.minX - left.maxX`. No hardcoded
  width exists in the source — a 14-inch, a 16-inch and a future model each get
  their own.
* **Screen alignment**: `origin.y = screen.frame.maxY - height`, centred on the
  notch rect.
* **Bottom corners**: continuous squircle, `radiusBottom` = 20 pt.
* **Top concave ear fillets**: `earFilletRadius(forWidth:notchWidth:)` returns
  `radiusEarFillet` (10 pt) only when
  `width > notchWidth + earFilletThreshold` (24 pt), otherwise 0. At exactly
  notch + 24 pt there are still no ears.

### External display mode (`hasNotch == false`)
* **Symmetrical floating pill**, `capsuleWidthRest` = **180 pt** (not the notch
  width — there is no notch to match).
  * Centred at `screen.visibleFrame.midX`.
  * 12 pt below the menu bar: `origin.y = screen.visibleFrame.maxY - height - 12`.
  * Corner radius `pillCornerRadius` = 22 pt.

---

## 4a. Gestures

| Chord | Meaning | Notes |
| :--- | :--- | :--- |
| `Option+Tab` | Ask — start a turn | While a turn is live, a single press is the kill switch |
| `Option+Tab` twice | Pause / resume (sleep mode) | Within `hotkey.double_tap_window_ms` = 400 ms. **The only pause gesture.** |
| `Option+Shift+Tab` | **Hand over** to the agent | Was pause until 2026-09-12 |
| `Escape` | Barge — kill audio, hard-cut the HUD | ≤ 50 ms budget, no animation at all |

Hand-over is emitted the way a wake turn is emitted: the daemon spawns
`pet-talk-cli handover` (it holds no socket of its own). The CLI turns that into
one WS frame — the contract for the server lane:

```json
{"type": "user.handover", "turn_id": "<uuid4>", "source": "hotkey"}
```

A CLI that does not know the subcommand exits non-zero; the daemon logs
`handover_emit_failed` with the exit status and shakes the capsule. It never
silently succeeds.

---

## 4b. Headless verification

The HUD is never shown to prove it works:

* `pet-talk-hotkey --self-test` (requires `PET_TALK_HEADLESS=1`) exercises the
  spring integrator (settles, starts at rest, lands exactly on target, overshoots,
  stays bounded, lands inside the drip budget) and the geometry math (measured
  width, fallback pill, ear-fillet threshold, height stepping, top-edge anchoring),
  then asserts no window became visible.
* `pet-talk-hotkey --dump-state` prints one JSON line: state machine, capsule
  geometry, spring constants in force, chord map, and the hand-over event shape.
* `PET_TALK_HEADLESS=1` routes every show path through
  `HUDController.orderFrontUnlessHeadless()`, so a QA run can never pop the
  capsule onto the screen someone is working on. The visual `test-hud` /
  `test-breadcrumbs` sequences are opt-in behind `PET_TALK_HUD_VISUAL=1`.

---

## 5. Where the code is

| Concern | Symbol |
| :--- | :--- |
| Spring tokens + config overrides | `HUDTokens` (`hud_window.swift`), defaults from `DesignTokens.swift` |
| The one `CASpringAnimation` | `HUDSpring.animation(keyPath:from:to:)` |
| Window-frame spring | `HUDSpringDriver`, `HUDController.springGeometry(...)` |
| Measured notch | `NotchManager.currentNotch()`, `HUDCapsuleView.measuredNotchWidth()` |
| Capsule width | `HUDCapsuleView.capsuleWidth` = measured ?? `fallbackCapsuleWidth` |
| Ear fillets | `HUDCapsuleView.earFilletRadius(forWidth:notchWidth:)` |
| Hard cut | `HUDController.dismiss(hardCut: true)` |
| Chords | `HotkeyConfig.hotKeyModifier` / `handoverHotKeyModifier`, `HotkeyListener.handleHotKeyTrigger()` / `handleHandoverHotKeyTrigger()` / `emitHandover()` |
| Headless guard | `HUDController.isHeadless`, `orderFrontUnlessHeadless()` |
| Tests | `qa/test_hud.py`, `qa/test_hotkey.py` (both headless) |
