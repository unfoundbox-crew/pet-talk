# CTO Architecture Specification: Hardware Notch Dynamic Island for Pet-Talk

**Document ID**: `SPEC-PET-TALK-004`  
**Status**: `APPROVED / IMPLEMENTATION-ACTIVE`  
**Author**: Donna (Chief of Staff & Digital Twin Brain)  
**Date**: September 11, 2026  
**Target Repository**: `unfoundbox-crew/pet-talk`  

---

## 1. Executive Summary & The Core Inversion

Traditional desktop voice assistants render arbitrary floating windows that fight macOS system chrome. On modern Apple Silicon MacBooks (M1/M2/M3/M4 Pro & Max), the display features a physical hardware webcam notch ($220\text{px} \times 38\text{px}$).

Following the foundational rule of native macOS island engineering—**"Reflect, Don't Host"**—Donna turns the hardware notch from an obstruction into a **living physical anchor**:

1. **Hardware-Software Fusion**: The top edge is flush with `screen.frame.maxY`, enveloping the camera lens module in pitch black (`#000000`), erasing the boundary between glass and display pixels.
2. **Apple HIG Spring Motion**: Exact Apple physics (`stiffness: 220.0`, `damping: 21.0`, `mass: 1.0`). Interruptible, skippable, snappy.
3. **Acoustic Truth**: Replace fake CSS/CoreAnimation loops with **live acoustic energy levels (RMS / FFT)** driven by the physical microphone.
4. **Zero Mock Code**: Native Swift, compiled via `swiftc -O` into `bin/pet-talk-hotkey`. Zero third-party baggage, zero Electron bloat, sub-50ms latency.

---

## 2. The 6-State Lifecycle Machine

```
+-----------------------------------------------------------------------------------+
|                            DYNAMIC ISLAND STATE MACHINE                           |
+-----------------------------------------------------------------------------------+

   [ 1. IDLE / NOTCH TUCKED ]   <--- (Escape or Turn Done)
         |
         +--- [Mouse Hover] --------> [ 2. HOVER PEEK ] (6px subtle gold shelf)
         |                                  |
         | (Option + Tab)                   | (Click / Option+Tab)
         v                                  v
   [ 3. LISTENING ("The Drip") ] <----------+
         | (Height 38px -> 52px, Emerald Waveform, Live RMS bars)
         v
   [ 4. THINKING ("The Pulse") ]
         | (220px width, SpacePilot Gold spinning orbit)
         v
   [ 5. SPEAKING / DICTATION ("The Blossom") ]
         | (Expands to 440px x 60px, concave top ear fillets r=10px,
         |  Donna persona tag, transcribed zinc typography, interruptible)
         +-----------------------------+
         |                             |
         | (Turn Complete)             | (Network / STT Error)
         v                             v
   [ 6. SUCTION RETRACTION ]     [ ERROR SHAKE ]
     (Collapses up into notch)     (±3px horizontal shake, Basso chime)
```

---

## 3. Apple Motion Design Language & Exact Tokens

| Event | Mechanism | Tokens & Constraints |
| :--- | :--- | :--- |
| **Drip Entrance** | Frame height $38\text{px} \rightarrow 52\text{px}$ | `stiffness: 220.0`, `damping: 21.0`, `mass: 1.0` ($220\text{ms}$) |
| **Island Blossom** | Width $220\text{px} \rightarrow 440\text{px}$, Height $52\text{px} \rightarrow 60\text{px}$ | `CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)` ($240\text{ms}$) |
| **State Transitions** | Indicator glyph & color morph | $160\text{ms}$ ease-out cubic |
| **Suction Retraction** | Collapses upward back into physical notch | $180\text{ms}$ ease-in curve |
| **Error Feedback** | 3-cycle horizontal micro-shake | $\pm 3\text{px}$ over $120\text{ms}$, `Basso.aiff` chime |
| **Reduce Motion** | `NSWorkspace.accessibilityDisplayShouldReduceMotion` | Zero spatial travel; instant $80\text{ms}$ crossfade |

---

## 4. Geometric Specification & Fillet Math

### Notch Mode (`hasNotch == true`)
* **Screen Alignment**: `origin.y = screen.frame.maxY - height`.
* **Notch Core Width**: Exactly matches `right.minX - left.maxX` ($220\text{px}$).
* **Bottom Corners**: Continuous squircle curvature ($r = 20\text{px}$).
* **Top Concave Ear Fillets**:
  $$\text{When } W > \text{notchWidth} + 20\text{px} \implies \text{Fillet Radius } r_{\text{ear}} = 10\text{px}$$
  Quadratic curve sweeping seamlessly from the top screen bezel into the expanded island wings.

### External Display Mode (`hasNotch == false`)
* **Symmetrical Floating Pill**:
  * Centered at `screen.visibleFrame.midX`.
  * Positioned $12\text{px}$ below menu bar: `origin.y = screen.visibleFrame.maxY - height - 12.0`.
  * Symmetrical continuous corner radius ($r = \text{height} / 2.0 = 22\text{px}$).

---

## 5. Subagent Fanout Work Breakdown

1. **Subagent 1 (`Notch Motion & Interaction Engineer`)**:
   - Updates `cli/hotkey/hud_window.swift` with exact Apple spring parameters (`stiffness: 220`, `damping: 21`).
   - Implements hover tracking area (`NSTrackingArea`) for subtle notch peek ($6\text{px}$).
   - Implements 3-cycle error shake animation ($\pm 3\text{px}$ over $120\text{ms}$).
   - Implements `Escape` key event monitor for instant $\le 50\text{ms}$ dismiss and barge-kill.
   - Adds `NSWorkspace.accessibilityDisplayShouldReduceMotion` support.

2. **Subagent 2 (`Live Acoustic Telemetry Engineer`)**:
   - Replaces fake looping `CABasicAnimation` bars in `HUDIndicatorView` with **live mic RMS energy levels**.
   - Accepts real-time normalized audio energy values ($0.0 \dots 1.0$) from the audio capture stream and drives bar heights dynamically.

3. **Subagent 3 (`QA Gatekeeper & Verification Lead`)**:
   - Updates `qa/test_hud.py` to assert new motion tokens, notch specs, and error shake.
   - Validates clean compilation via `swiftc -O`.
   - Runs `qa/run_all.sh` asserting all 12 QA gates pass `RESULT: OK`.
   - Restarts hotkey daemon and captures operational telemetry.
