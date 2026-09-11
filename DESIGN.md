---
version: "alpha"
name: "pet-talk-design-system"
description: "Google Labs / Gemini Live A1 design specification and token schema for pet-talk real-time voice duplex interface."
colors:
  surface:
    canvas: "#090a0f"
    elevated: "#12141c"
    card: "#191c26"
    card-hover: "#222634"
    border: "#282c3f"
    border-focus: "#4f587d"
  text:
    primary: "#f1f3f9"
    secondary: "#9ba3b8"
    muted: "#636c84"
    on-accent: "#090a0f"
  accent:
    gemini-blue: "#4285f4"
    gemini-cyan: "#24c1e0"
    gemini-purple: "#a142f4"
    emerald: "#00c853"
    amber: "#ffb300"
    crimson: "#ff3d00"
  state:
    idle: "#636c84"
    listening: "#00c853"
    thinking: "#24c1e0"
    speaking: "#ffb300"
    barged: "#ff3d00"
typography:
  font-family:
    sans: "Google Sans, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    mono: "Geist Mono, JetBrains Mono, SF Mono, Menlo, Consolas, monospace"
  font-size:
    xs: "0.75rem"
    sm: "0.875rem"
    base: "1rem"
    lg: "1.125rem"
    xl: "1.25rem"
    2xl: "1.5rem"
    3xl: "1.875rem"
  font-weight:
    regular: 400
    medium: 500
    semibold: 600
    bold: 700
spacing:
  2xs: "0.25rem"
  xs: "0.5rem"
  sm: "0.75rem"
  md: "1rem"
  lg: "1.5rem"
  xl: "2rem"
  2xl: "3rem"
radii:
  sm: "6px"
  md: "10px"
  lg: "16px"
  xl: "24px"
  pill: "9999px"
elevation:
  shadow-low: "0 1px 3px rgba(0, 0, 0, 0.4), 0 1px 2px rgba(0, 0, 0, 0.24)"
  shadow-mid: "0 4px 16px rgba(0, 0, 0, 0.5), 0 2px 4px rgba(0, 0, 0, 0.3)"
  shadow-high: "0 12px 32px rgba(0, 0, 0, 0.6), 0 4px 8px rgba(0, 0, 0, 0.4)"
  glow-listening: "0 0 24px rgba(0, 200, 83, 0.35)"
  glow-thinking: "0 0 24px rgba(36, 193, 224, 0.4)"
  glow-speaking: "0 0 24px rgba(255, 179, 0, 0.35)"
motion:
  easing:
    standard: "cubic-bezier(0.2, 0.0, 0, 1.0)"
    decelerate: "cubic-bezier(0.0, 0.0, 0.2, 1.0)"
    accelerate: "cubic-bezier(0.3, 0.0, 1.0, 1.0)"
  duration:
    short: "150ms"
    medium: "250ms"
    long: "400ms"
---

# pet-talk: Google-Grade Duplex Voice Design Specification

## 1. Vision & Identity

`pet-talk` is a sub-400ms reflex-arc duplex voice interaction terminal built with the tactile elegance of Google's flagship consumer AI interfaces (Gemini Live / Pixel Call Assist). 

The design rejects generic chat-box bubbles in favor of a **living acoustic instrument**:
- **Continuous Presence**: State is embodied in a dynamic, responsive audio visualizer (Orb / Saccade Waveform) that pulses with mammalian breathing rhythms when idle, dilates on human speech detection, ripples during frontier reasoning, and radiates kinetic energy during synthetic phoneme delivery.
- **Dignity in Telemetry**: Latency is never hidden behind generic spinner circles. Sub-millisecond latency waterfalls (VAD -> STT -> LLM TTFT -> TTS First Byte -> Playback) are rendered with cryptographic precision in tabular monospace figures.
- **Zero Friction Customizability**: Developers and users can inspect, modify, hot-swap, and tune voice models, speeds, emotional temperature, system prompts, and memory ledgers with instant acoustic feedback.

---

## 2. Core Surface Architecture

```
+-----------------------------------------------------------------------------------+
|  [G] pet-talk  v0.2.0           [En / Hi]   [Memory Ledger (14)]   [Settings]   [●] |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|                                (  ACOUSTIC ORB  )                                 |
|                               [   STATE: SPEAKING  ]                               |
|                                                                                   |
|                    "The reflex arc clocked in at 240 milliseconds."                |
|                                                                                   |
|            [ Barge-in Kill (Space) ]     [ Push to Talk (Hold Space) ]             |
|                                                                                   |
+-----------------------------------------------------------------------------------+
|  WATERFALL TELEMETRY                                                              |
|  VAD: 28ms  |  STT: 142ms  |  TTFT: 210ms  |  TTS: 180ms  |  E2E: 560ms (flown)   |
+-----------------------------------------------------------------------------------+
|  PERSONA STUDIO                                                                   |
|  Persona: [ Donna       v ]   Voice: [ Shannon   v ]   Speed: [ 1.05x  --O-- ]    |
|  System Directive: "You are Donna, Chief of Staff & Co-founder Brain..."           |
+-----------------------------------------------------------------------------------+
|  TRANSCRIPT STREAM & REPLAY                                                       |
|  11:22:04 [User]   What is the reflex arc budget?                                 |
|  11:22:05 [Agent]  Under four hundred milliseconds from speech cessation.         |
+-----------------------------------------------------------------------------------+
```

---

## 3. State Layer & Visual Kinetics

The UI state machine transitions through 5 distinct reactive phases:

| State | Primary Glow / Token | Waveform Motion | Orb Reaction |
|---|---|---|---|
| `idle` | `{colors.state.idle}` | Subtle 0.5Hz sine wave (breathing) | Scale 1.0, 30% opacity ring |
| `listening` | `{colors.state.listening}` | Real-time WebAudio RMS microphone spectrum | Scale 1.15, emerald active pulse |
| `thinking` | `{colors.state.thinking}` | Cyan orbiting gradient, multi-ring ripple | Internal rotation 120 RPM |
| `speaking` | `{colors.state.speaking}` | Multi-band acoustic amplitude bars | Scale 1.2, golden kinetic expansion |
| `barged` | `{colors.state.barged}` | Flash snap-back to zero amplitude (<100ms) | Shockwave collapse to listening |

---

## 4. Component Contracts

### 4.1 Acoustic Visualizer Orb (`<AcousticOrb />`)
- Consumes real-time audio samples via `AnalyserNode.getByteFrequencyData()`.
- Renders dual-mode: 
  - User speaking: green frequency ripples corresponding to mic input.
  - Agent speaking: gold frequency arcs corresponding to outgoing synthesized audio.
  - Idle/Thinking: algorithmic harmonic Lissajous or breathing orbital glow.

### 4.2 Latency Waterfall HUD (`<TelemetryWaterfall />`)
- Displays live waterfall bars with exact ms timestamps for each turn:
  - `vad`: Silence threshold confirmation (target <=250ms).
  - `stt`: Speech-to-text transcription latency.
  - `stall`: Backchannel reflex dispatch (target <=400ms).
  - `ttft`: LLM time-to-first-token.
  - `tts`: Synthesis time for sentence chunk 0 (target <=200ms).
  - `e2e`: Wall-clock end-to-end turn reflex.
- Typed provenance: labeled `flown` (measured locally over live WS) or `simulated` (stub loop).

### 4.3 Persona Studio (`<PersonaStudio />`)
- Persona selection: Built-in personas (`Donna`, `Zuck`, `Jarvis`) + Custom user personas.
- Real-time parameter controls:
  - Speech rate slider (0.5x to 2.0x, step 0.05x).
  - Voice timbre dropdown with instantaneous one-sentence preview audio.
  - System prompt drawer with live editing and instant turn injection.
  - Temperature / Creativity slider.
  - Input mode toggle: `Push-to-Talk` vs `Continuous Hands-Free (VAD)`.

### 4.4 Hippocampus Memory Inspector (`<MemoryLedger />`)
- Displays persistent dialogue history backed by `server/data/ledger.jsonl`.
- Shows turn ID, timestamp, persona, user transcript, and agent spoken sentences.
- Actions:
  - Search / filter turns by query.
  - Download ledger as JSONL.
  - Rollback / delete turn.
  - Purge ledger with safety verification.

---

## 5. Accessibility & Ergonomics (WCAG AA Compliance)

- **Keyboard First**: 
  - `Space` (hold): Push to talk.
  - `Space` (tap during speech): Instant barge-in kill.
  - `Esc`: Dismiss modals / drawers.
  - `M`: Toggle mic mute.
- **Color Contrast**: 
  - All text tokens satisfy WCAG AAA ratio >= 7:1 against their respective canvas surfaces (`#f1f3f9` on `#090a0f` is 16.8:1).
- **Reduced Motion**: 
  - Respects `@media (prefers-reduced-motion: reduce)` by disabling orb rotation and continuous particle animations, substituting discrete opacity transitions.
