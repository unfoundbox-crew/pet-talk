# CTO Architecture Specification: Hardware Notch Dynamic Island for Pet-Talk

**Document ID**: `SPEC-PET-TALK-004`  
**Status**: `APPROVED / IMPLEMENTATION-ACTIVE`  
**Date**: September 11, 2026  
**Revised**: September 12, 2026 — Receipts Over Prose on the AgentWorth palette; measured notch width, a real spring, hand-over chord  
**Verified against code**: September 12, 2026 (`cli/hotkey/HUDTheme.swift`, `cli/hotkey/hud_window.swift`, `cli/hotkey/main.swift`)  
**Target Repository**: `unfoundbox-crew/pet-talk`  

---

## 1. Executive Summary & The Core Inversion

Traditional desktop voice assistants render arbitrary floating windows that fight macOS system chrome. On modern Apple Silicon MacBooks the display features a physical hardware webcam notch. Its size is **measured at runtime**, never assumed: `right.minX - left.maxX` of the screen's auxiliary top areas (220 pt on the machine this was verified on, 2026-09-12). A screen that reports no notch gets a 180 pt floating pill instead.

Following the foundational rule of native macOS island engineering—**"Reflect, Don't Host"**—Donna turns the hardware notch from an obstruction into a **living physical anchor**:

1. **Hardware-Software Fusion**: The top edge is flush with `screen.frame.maxY`, enveloping the camera lens module in `--mv-ground` (dark), which is the same black the notch glass already is — the boundary between glass and display pixels disappears.
2. **Real spring motion**: `stiffness: 220`, `damping: 21`, `mass: 1` — declared once in `DesignTokens.swift`, read by `HUDTokens`, and actually integrated. Interruptible, skippable, snappy. Measured 2026-09-12: 225 ms to 99 % of travel (the 220 ms drip budget), 3.1 % overshoot, fully settled at 527 ms.
3. **Acoustic Truth**: One 2 pt line, and its vertical displacement *is* the microphone level. No bars, no spinner, no loop. Silence is a flat line — still listening, nothing to say.
4. **Zero Mock Code**: Native Swift, compiled via `swiftc -O` into `bin/pet-talk-hotkey`. Zero third-party baggage, zero Electron bloat, sub-50ms latency.

---

## 2. The 6-State Lifecycle Machine

```
+-----------------------------------------------------------------------------------+
|                            DYNAMIC ISLAND STATE MACHINE                           |
+-----------------------------------------------------------------------------------+

   [ 1. IDLE / NOTCH TUCKED ]   <--- (Escape = hard cut, or Turn Done)
         |
         +--- [Mouse Hover] --------> [ 2. HOVER PEEK ]  (hoverPeek shelf + sleep bead)
         |                                  |
         | (Option+Tab)                     | (Click / Option+Tab)
         v                                  v
   [ 3. LISTENING ]  envelope: compact  <---+
         | (37pt -> 52pt "the drip"; glyph + the live 2pt line, no label)
         v
   [ 4. THINKING ]  envelope: compact
         | (glyph light steady + the stall text. No spinner: a spinner
         |  would claim progress nothing is measuring.)
         v
   [ 5. SPEAKING ]  envelope: expanded -> tall
         | (measured notch -> 440pt, ear fillets r=10 once content spills;
         |  sentence in --mv-ink, dimmed next-sentence preview in --mv-faint,
         |  right-aligned mono receipt; 110pt ceiling)
         +-----------------------------+
         |                             |
         | (Turn Complete)             | (Network / STT / provider error)
         v                             v
   [ 6. SUCTION RETRACTION ]     [ ERROR ]  envelope: error
     (springs up into the notch;   (reason + fix, --mv-danger peg; the ONLY
      leaves the sleep bead)        envelope that waits to be dismissed)

   Chords: Option+Tab = ask/kill · Option+Tab twice (<=400ms) = pause (envelope: sleep)
           Option+Shift+Tab = hand over · Escape = barge (hard cut)
```

### State -> visual, asserted not described

`HUDTheme.visual(for:hasNotch:tall:)` is the table. `--dump-state` exports it as
`stateVisuals`, and `qa/test_hud.py::TestStateToVisualMapping` asserts every row —
so this table cannot drift from the binary.

| state | envelope | Archie glyph | listening line |
| --- | --- | --- | --- |
| listening | `compact` | `listening` | **live** |
| thinking | `compact` | `idle` (steady light) | off |
| speaking | `expanded`, `tall` past 60pt | `speaking` (one beat per sentence) | off |
| error | `error` | `error` (lamp off) | off |
| sleep (paused) | `sleep` | `error` (lamp off) | off |

`hasNotch == false` rewrites every row's envelope to `pill`. The fallback display
has one shape; it gains nothing from having six.

---

## 2a. Visual specification — the token behind every element

Every colour in the capsule resolves through `cli/hotkey/HUDTheme.swift`, which
reads `cli/hotkey/DesignTokens.swift`, which `design/build.py` generates from
`design/tokens.css` (AgentWorth's, vendored verbatim) plus
`design/tokens.pet-talk.json`. There is **no hex literal** in `hud_window.swift`
or `HUDTheme.swift`; `qa/test_hud.py::TestNoHexInHUDPaintFiles` fails the suite if
one appears, in either the `#rrggbb` or the `0xNN / 255` form.

**The capsule is always the dark palette.** It is not a window that happens to be
dark — it is the physical notch, which is black glass at every hour of the day. A
light variant would be a light rectangle glued under a black notch. The
three-state theming contract still binds every surface where the theme is a
*choice*; the cockpit honours it, and the capsule has no choice to make.

### Colour

| element | token | note |
| --- | --- | --- |
| capsule ground | `--mv-ground` (dark) | opaque, not 95 % — a translucent ground lets the desktop through and the capsule stops being the notch |
| 1 pt outline | `--mv-border` (dark) | an edge, never window chrome |
| spoken sentence | `--mv-ink` (dark) | the highest-contrast step, used for exactly one thing |
| the one compact line | `--mv-text` (dark) | |
| mono eyebrow (`heard`, `paused`, an error code), receipt label, **body of the listening line** | `--mv-muted` (dark) | the line is chrome that reports; it is not a highlight |
| next-sentence preview, sleep bead | `--mv-faint` (dark) | present enough to say "there is more", quiet enough not to be read first |
| **listening-line peak** | `--mv-accent` | one of two violets |
| **receipt total** | `--mv-accent` | the other one |
| state pegs | `--mv-success` / `--mv-warn` / `--mv-danger` | reserved. Only a real out-of-budget number earns one |

**One violet rule.** `--mv-accent` carries the listening-line peak and the receipt
total. Nothing else. Selection, focus and links — its other jobs in AgentWorth —
do not exist in a click-through HUD. State never gets the accent: the glyph and
the reserved pegs carry that. `HUDTheme.accentRoles` exports the pair and
`qa/test_hud.py` asserts it is exactly those two.

### Type

Geist and Geist Mono are AgentWorth's faces and neither is installed system-wide
on macOS. **SF Pro and SF Mono are the native stand-ins**, via
`NSFont.systemFont` / `NSFont.monospacedSystemFont`: the same role split — mono
for anything a machine produced, sans for anything a person reads as language —
at the same token sizes. **Follow-up: bundle the Geist OFL files into
`bin/pet-talk-hotkey` and switch the two `HUDTheme.font` families over.** Until
then the spec is honest that these are stand-ins, not Geist.

| role | face | size token | colour |
| --- | --- | --- | --- |
| `receipt` | SF Mono, medium | `type.size.micro` (9.9) | `--mv-muted` |
| `receiptTotal` | SF Mono, medium | `type.size.micro` | `--mv-accent` |
| `eyebrow` | SF Mono, semibold, kern 0.4 | `type.size.micro` | `--mv-muted` |
| `line` | SF Pro, medium | `type.size.caption` (11.8) | `--mv-text` |
| `sentence` | SF Pro, medium | `type.size.caption` | `--mv-ink` |
| `preview` | SF Pro, regular | `type.size.caption` | `--mv-faint` |

`type.size.micro` and `type.size.caption` are the 1.200 scale continued *down*
from `type.size.label` (14.2): 14.2 / 1.2 = 11.8, / 1.2 again = 9.9. Not new
numbers — the same scale, two steps further.

### Envelopes

The design boards were drawn at a 180 pt notch. The notch on this machine
measures **220 pt** (`--dump-state`, 2026-09-12). So the boards' horizontal
numbers are proportions, never pixels: every horizontal metric in
`layoutSubviews(forWidth:height:)` is a fraction of the *actual* width, and
`HUDCapsuleView.capsuleWidth` is the measured notch. Heights are unchanged from
the existing tokens, ceiling included.

| envelope | size | contents |
| --- | --- | --- |
| `compact` | measured notch x `capsule.height.listening` (52) | glyph + one line. Listening shows no word at all — the moving line *is* the label |
| `expanded` | `capsule.width.expanded` (440) x `capsule.height.expanded` (60) | glyph, the current sentence, a dimmed next-sentence preview, right-aligned mono receipt |
| `tall` | 440 x up to `capsule.height.max` (**110**) | multi-line sentence. Past the ceiling the answer belongs in the cockpit, not under the notch |
| `error` | 440 x 60 | the reason and its fix. The only envelope that does not retract on its own |
| `sleep` | collapsed to the notch + the `sleep.bead` (2 pt) | she stops taking up room rather than disappearing |
| `pill` | fallback width x `capsule.height` (44), radius `pillCornerRadius` (22) | no hardware notch. Same content, rounder corners, nothing added |

`--dump-state` reports `theme: agentworth`, the `envelope` in force, and the fixed
`envelopes` set. The set is pinned by the suite: a seventh envelope is a design
decision, not a code change.

### Copy

No persona name, no millisecond readings, no frame names. The capsule says what is
happening in a plain lowercase word — `working`, `speaking`, `heard` — and
developer detail lives in the cockpit. `qa/test_hud.py::TestCapsuleCopyIsClean`
greps every string literal in `hud_window.swift` for the persona name and for
digits in `HUDState.labelText`. The engine's own log lines still name her; that is
a log, not a capsule.

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

Hand-over is emitted the way a wake turn is emitted, and since 2026-09-12 that
means the daemon's own socket, not a subprocess (`turn_engine: native`,
docs/SPEC.md §4.4). One WS frame, then the mic opens:

```json
{"type": "user.handover", "turn_id": "<uuid4>", "source": "hotkey"}
```

On the `cli` fallback engine the daemon still spawns `pet-talk-cli handover` and
the CLI sends that frame; a CLI that does not know the subcommand exits non-zero,
the daemon logs `handover_emit_failed` with the exit status and shakes the
capsule. Either way it never silently succeeds.

---

## 4a-1. Where the capsule's numbers come from

The capsule is fed from inside this process now. `AudioCapture`'s input tap
computes RMS and peak on every 20 ms frame and calls
`HUDController.updateAudioLevel(rms:peak:)` directly, normalised the same way as
before (`rms / 4000`, `peak / 16000`), so the waveform behaves identically while
nothing is parsed out of a pipe. On the `cli` engine, `parseRMSTelemetry` still
reads `[RMS: 0.245, PEAK: 0.512]` lines off the child's stdout.

`agent.sentence.word_times` drives the glyph: one `ArchieGlyphView.beat()` per
word start, scheduled on the main queue and dropped if a barge has happened since
(`ChunkPlayback.scheduleBeats`). A sentence with no timings gets one beat — never
an invented rhythm.

The error state carries a plain line, never an engineering word: the named reason
(`health_probe_failed`, `ws_unauthorized`, `mic_permission_denied`, …) goes to the
log, and `TurnController.capsuleText(for:)` maps it to what the capsule shows —
"can't reach the studio", "studio token missing", "microphone access needed".
Microphone permission is requested with `AVCaptureDevice.requestAccess(for:
.audio)` on first use; a denial is that error state, not an empty recording.

---

## 4b. Headless verification

The HUD is never shown to prove it works:

* `pet-talk-hotkey --self-test` (requires `PET_TALK_HEADLESS=1`) exercises the
  spring integrator (settles, starts at rest, lands exactly on target, overshoots,
  stays bounded, lands inside the drip budget) and the geometry math (measured
  width, fallback pill, ear-fillet threshold, height stepping, top-edge anchoring),
  then asserts no window became visible.
* `pet-talk-hotkey --dump-state` prints one JSON line: state machine, capsule
  geometry, spring constants in force, chord map, the hand-over event shape, the
  resolved `turnEngine` (with the studio token's SOURCE, never its value) and the
  `nativeAudio` contract (sample rate, frame size, VAD constants, chunk cap, mic
  permission status).
* `pet-talk-hotkey --self-test --turn-engine native` exercises the turn engine
  with no hardware: the VAD's constants and end-of-turn arithmetic, the WAV
  parser, the barge generation counter, token resolution, engine precedence.
  Adding `--fake-server <ws-url>` runs one whole turn against
  `qa/fixtures/fake_ws_server.py` over a real socket — synthetic mic, silent
  sink. Both print one `NATIVE-METRICS {json}` line for a test to assert on.
  The only real-microphone path is behind `PET_TALK_REAL_MIC=1`: it records one
  second and plays nothing.
* `PET_TALK_HEADLESS=1` routes every show path through
  `HUDController.orderFrontUnlessHeadless()`, so a QA run can never pop the
  capsule onto the screen someone is working on. The visual `test-hud` /
  `test-breadcrumbs` sequences are opt-in behind `PET_TALK_HUD_VISUAL=1`.

---

## 4c. Archie's small form

Lane 5a shipped `cli/hotkey/ArchieGlyph.swift` (`ArchieGlyphView`, the native
Swift twin of `web/src/components/ArchieGlyph.tsx`); this capsule wires exactly
one instance into itself, per agentworth's placement rule — "once per screen,
he arrives bare" (`agentworth/docs/DESIGN.md`, "Archie").

* **Where**: `HUDCapsuleView.archieGlyph`, leading edge, colourway **C4**
  ("Quiet" — dense chrome, must not out-shout the data). Small form only —
  never the full hound, never a torch — because `ArchieGlyphView` draws
  nothing else.
* **Sizing**: 16 pt square, `archieGlyphLeadingPadding` (8 pt) from the
  leading edge, vertically centred in the capsule's current height.
* **State mapping** (`HUDCapsuleView.archieGlyphState(for:badge:)`):

  | HUD state / signal | Glyph state | Note |
  | :--- | :--- | :--- |
  | `.listening` | `.listening` | bright halo |
  | `.thinking` | `.idle` | the light stays steady — Archie is not the gold spinner |
  | `.speaking` | `.speaking` | `beat()` fires once per call (see below) |
  | error (`triggerErrorShake`) | `.error` | lamp off for the shake, restored after |
  | sleep (badge `"PAUSED"`) | `.error` | same "off" appearance as an error — the badge always wins over the state passed alongside it |
* **`beat()`**: word-level timing is not delivered to the HUD today — the CLI
  forwards whole spoken sentences, not per-word events. `HUDController.showBreadcrumb`
  is the one sentence-arrival hook (main.swift calls it once per `[SPEAKING]` line),
  so it calls `archieGlyph.beat()` once per call when `state == .speaking`: one
  beat per sentence, not per word.
* **Hidden**: below `archieGlyphMinHeight` (24 pt), and in the no-notch
  fallback pill when the pill is too narrow to fit the glyph without crowding
  the label (`minPillWidthForGlyph`). Visible in every notch-mode compact and
  expanded layout otherwise; the rest of the row (indicator, label, persona
  badge) shifts right by the glyph's own footprint only while it's shown.
* **`--dump-state`** carries the live read under `glyph`: `{state, colourway,
  visible}` — no window shown, same headless contract as the rest of the dump.

---

## 4d. Motion, per the AppleMotion table

| moment | duration | curve | what moves | reduced motion |
| --- | --- | --- | --- | --- |
| the quiet line | continuous | none — direct write per frame | the line's displacement is the live level | stays live, smoothed. Suppressing it would hide whether she can hear |
| wake (drip) | `duration` = spring settle | spring 220 / 21 / 1 | 37pt -> 52pt, one material | crossfade at final height, zero travel |
| size change (blossom) | spring settle | same spring | width and height together | snap to the new pose |
| sentence arrives | `duration.sentenceArrive` (160 ms) | ease-out | the sentence crossfades in | same, stagger zeroed |
| receipt follows | +`duration.receiptStagger` (60 ms) | ease-out | the eye lands on words first, evidence second | arrives with the sentence |
| barge / Escape | **0 ms** | **none at any duration** | audio, line and queue die on the same frame | identical — there was never anything to soften |
| error | `error.shakeDuration` (120 ms) | ease-in-out | `error.shakeCycles` x +/-`error.shakeAmplitude`, decaying | skipped entirely; the glyph still goes dark and Basso still plays |
| sleep retract | `duration.sleepRetract` (180 ms) | ease-in | the capsule collapses around the bead | fade at full height, zero travel |

Reduced motion, stated once: **distance goes to zero, durations stay, and live
microphone data is smoothed rather than suppressed.** `--dump-state` exports this
as `reducedMotion`, and the suite asserts it. **Nothing loops while idle** —
`repeatCount = .infinity` and `autoreverses` are both absent from the HUD, by test.

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
| Mic -> capsule level | `AudioCapture` tap -> `PCMEnergy.normalise` -> `HUDController.updateAudioLevel` (`AudioCapture.swift`) |
| Turn state machine | `TurnController` (`TurnController.swift`), engine choice `TurnEngine.resolve` |
| Studio socket | `WSClient` (`WSClient.swift`), token `StudioToken.resolve()` |
| Reply audio + barge cut | `ChunkPlayback.stop()` (`ChunkPlayback.swift`), generation counter |
| Glyph beat from word timings | `ChunkPlayback.scheduleBeats(wordTimes:)` -> `ArchieGlyphView.beat()` |
| Error line on the capsule | `TurnController.capsuleText(for:)` |
| Tests | `qa/test_hud.py`, `qa/test_hotkey.py` (both headless), fixture `qa/fixtures/fake_ws_server.py` |
