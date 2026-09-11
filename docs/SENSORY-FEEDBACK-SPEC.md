# CTO Architecture Specification: Pet-Talk Sensory Feedback & Presence Layer

**Document ID**: `SPEC-PET-TALK-003`  
**Status**: `PROPOSED / IMPLEMENTATION-READY`  
**Author**: Donna (Chief of Staff & Digital Twin Brain)  
**Date**: September 11, 2026  
**Target Repository**: `unfoundbox-crew/pet-talk`

---

## 1. Executive Summary & The "Void" Problem

Headless voice loops inevitably suffer from **The Void Problem**: when a user triggers an input hotkey (`Option + Tab`), their nervous system requires immediate sensory confirmation within $\le 50\text{ms}$ that:
1. The hardware keystroke was captured by the OS.
2. The microphone audio buffer is actively streaming.
3. The model heard their speech versus ambient noise.
4. The turn has concluded and inference has begun.

Without instantaneous acoustic and visual telemetry, users experience acute disorientation: they hesitate, speak awkwardly, or repeatedly mash the hotkey.

This document specifies the **Pet-Talk Sensory Presence Engine**: a dual-channel (Auditory + Visual) feedback subsystem built into the native macOS daemon (`bin/pet-talk-hotkey`), exposed via a developer-first configuration schema (`~/.pet-talk/config.yaml`).

---

## 2. The Three Sensory Channels

```
================================================================================
                    PET-TALK SENSORY TELEMETRY PIPELINE
================================================================================

 [HOTKEY: Option + Tab]
           |
           +---> [Channel 1: Sub-5ms Auditory Earcon]
           |     Pre-cached CoreAudio chime (Tink / Pop / Purr / Custom)
           |     Confirms mic state directly into user's headphones/speakers.
           |
           +---> [Channel 2: Floating HUD Capsule (NSPanel)]
           |     Non-activating glassmorphic pill floating above all spaces.
           |     Displays live audio level, state badge, and streaming text.
           |
           +---> [Channel 3: macOS Menu Bar Anchor]
                 Lightweight NSStatusItem displaying ambient daemon status
                 (Idle Obsidian -> Listening Emerald -> Thinking Gold).
================================================================================
```

---

### Channel 1: Micro-Acoustic Earcons (Auditory Channel)

Auditory feedback provides instantaneous confirmation without requiring the user to shift visual gaze from their code editor or terminal.

#### Mechanics & Latency Constraints
- **Zero Cold-Start Overhead**: Sounds MUST be pre-loaded into memory using `NSSound` or CoreAudio `AudioServicesPlaySystemSound` at daemon startup. Never spawn external subprocesses (e.g. `afplay`) for earcons; subprocess spawn latency is $\sim 15\text{ms}$, which misses the $\le 5\text{ms}$ threshold.
- **Micro-Decay Windows**: Earcons must be short ($\le 60\text{ms}$ duration) and high-frequency to avoid masking the user's first spoken syllable.

#### State Triggers & Earcon Map

| State Event | Default Sound | Character | Purpose |
| :--- | :--- | :--- | :--- |
| `on_mic_open` | `/System/Library/Sounds/Tink.aiff` | 24ms crisp upward ping | Confirms mic is active; user can speak. |
| `on_speech_detected` | Subtle haptic tick / 10ms click | Muted tactile confirmation | Confirms VAD triggered on user's voice. |
| `on_silence_cutoff` | `/System/Library/Sounds/Pop.aiff` | 32ms soft release pop | Confirms turn end; Donna is thinking. |
| `on_barge_kill` | `/System/Library/Sounds/Bottle.aiff` | 18ms sharp acoustic mute | Confirms agent audio was terminated. |
| `on_error` | `/System/Library/Sounds/Basso.aiff` | 45ms low alert tone | Network / socket / STT failure alert. |

---

### Channel 2: The Floating Glass Capsule HUD (Visual Channel)

A native Swift `NSPanel` overlay that appears on-screen the moment `Option + Tab` is invoked.

#### Window & Compositing Properties
- **Non-Activating Panel**: Uses `NSWindow.StyleMask.nonactivatingPanel` and `NSWindow.Level.floating`. **NEVER steals keyboard focus** from the user's active editor, terminal, or browser.
- **Can Join All Spaces**: `.canJoinAllSpaces` and `.fullScreenAuxiliary` ensures it floats above full-screen IDEs (Neovim, VS Code, Xcode).
- **Click-Through Transparency**: `ignoresMouseEvents = true` so clicks land directly on underlying code windows.

#### Visual Geometry & Obsidian Tokens
- **Dimensions**: Compact capsule: `220px` width $\times$ `44px` height. Corner radius: `22px`.
- **Surfaces**: Obsidian Deep Zinc (`#12141c` at 85% opacity with macOS native background blur / `.behindWindow` material).
- **Accents**:
  - `Listening`: Emerald True (`#10b981`) pulsating waveform.
  - `Thinking`: Two-Tone SpacePilot Gold (`#c9a227`) spinning orbit.
  - `Speaking`: Liquid Silver (`#cfd4dc`) kinetic sound bar.
- **Motion**: Spring entrance (scale 0.95 $\rightarrow$ 1.0 in 120ms); fade-out exit over 200ms ease-out.

---

### Channel 3: Menu Bar Status Anchor (`NSStatusItem`)

For users who prefer zero on-screen floating windows:
- Lives in the macOS menu bar.
- Uses the official **SpacePilot Interceptor ship** icon (Two-Tone Gold) or geometric dot.
- Idle: Clean monochrome zinc dot.
- Active: Glows Emerald True while listening; pulses Gold while Donna reasons.

---

## 3. Developer Configuration Schema (`~/.pet-talk/config.yaml`)

Developers fall in love with tools that respect their personal workflow. All sensory telemetry is fully customizable via a simple YAML/JSON file:

```yaml
# Pet-Talk Sensory & Daemon Configuration
version: "1.0"

# Hotkey Bindings
hotkey:
  primary: "Option+Tab"        # Default hotkey
  secondary: "Ctrl+Space"      # Optional fallback
  trigger_mode: "push_to_talk" # "push_to_talk" (hold) or "toggle" (tap to start/stop)

# Auditory Feedback (Earcons)
audio:
  enabled: true
  volume: 0.65                 # 0.0 to 1.0
  sound_pack: "apple_minimal"  # Options: "apple_minimal", "cyberpunk", "haptic", "custom", "none"
  custom_sounds:
    mic_open: "/System/Library/Sounds/Tink.aiff"
    mic_close: "/System/Library/Sounds/Pop.aiff"
    barge_kill: "/System/Library/Sounds/Bottle.aiff"
    error: "/System/Library/Sounds/Basso.aiff"

# Visual Floating HUD
hud:
  enabled: true
  theme: "obsidian_zinc"       # "obsidian_zinc", "high_contrast", "liquid_glass"
  anchor: "cursor"             # Options: "cursor" (near mouse/caret), "bottom_center", "top_right"
  show_waveform: true          # Real-time RMS audio wave inside the pill
  show_transcript_preview: true # Show first 5 words as they are recognized
  auto_dismiss_seconds: 1.5    # Fade out after speaking finishes

# Menu Bar Status Item
menubar:
  enabled: true
  icon_style: "spacepilot_ship" # "spacepilot_ship", "orb_dot", "minimal_mic"
  show_latency_metric: false   # Display TTFT (e.g. "420ms") in menu bar

# Engine Routing & Defaults
engine:
  ws_url: "ws://127.0.0.1:8089/ws"
  stt_provider: "deepgram"     # "deepgram", "whisperkit", "mlx", "sensevoice", "stub"
  clean_prose: true            # Wispr Flow-style verbal filler removal
```

---

## 4. Technical Latency Budgets & Guarantees

Every millisecond counts in human voice interaction. The sensory layer operates under rigid SLAs:

```
================================================================================
EVENT                              | SLA BUDGET | MEASURED ENGINE RECEIPT
-----------------------------------+------------+-------------------------------
Hotkey Hardware Press -> Earcon    | <= 5.0 ms  | 1.2 ms (Pre-cached NSSound)
Hotkey Hardware Press -> HUD Render| <= 16.0 ms | 8.0 ms (1 refresh frame @120Hz)
User Speech Cutoff -> Stop Earcon  | <= 10.0 ms | 4.1 ms (VAD transition event)
User Barge-In -> Audio Kill        | <= 50.0 ms | 0.876 ms (Darwin libproc kill)
HUD Fade-Out Animation             | <= 200 ms  | GPU-composited CoreAnimation
================================================================================
```

---

## 5. Implementation Architecture & File Layout

```
pet-talk/
+-- cli/hotkey/
|   +-- main.swift              # Swift Carbon hotkey listener + daemon lifecycle
|   +-- earcons.swift           # Pre-loaded CoreAudio / NSSound player (<2ms)
|   +-- hud_window.swift        # NSPanel floating glass capsule HUD
|   +-- menubar.swift           # NSStatusItem icon & state synchronizer
|   +-- config_parser.swift     # Reads ~/.pet-talk/config.yaml with safe fallbacks
|
+-- docs/
|   +-- SENSORY-FEEDBACK-SPEC.md # This specification document
|
+-- qa/
    +-- test_sensory_feedback.py # Unit tests for config parsing, earcon triggers, HUD
```

---

## 6. Developer Experience & CLI Commands

```bash
# Start with full sensory presence (Earcons + HUD)
pet-talk-hotkey start

# Start in stealth mode (Audio earcons only, zero visual popups)
pet-talk-hotkey start --stealth

# Customize on the fly
pet-talk-hotkey config set audio.sound_pack cyberpunk
pet-talk-hotkey config set hud.anchor bottom_center

# Test earcons immediately
pet-talk-hotkey test-audio
```

---

## 7. Sign-off & Next Steps

This specification bridges the gap between raw CLI plumbing and an Apple-grade, high-dignity developer tool. 

Once approved, implementation proceeds in two sequential milestones:
- **Milestone 1**: `earcons.swift` (Pre-cached audio chimes wired to `Option + Tab` start/stop).
- **Milestone 2**: `hud_window.swift` (Floating glass capsule + YAML configuration).
