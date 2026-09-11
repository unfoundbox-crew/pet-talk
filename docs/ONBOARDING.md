# Pet-Talk — Developer & Agent Onboarding Guide

Welcome to **Pet-Talk**. This document is the primary onboarding guide for human engineers and autonomous agents working on the codebase.

---

## 1. Executive Summary & Philosophy

Pet-Talk is a local-first, low-latency, full-duplex conversational voice and sensory presence engine designed for ambient AI companions (e.g. Donna, your Chief of Staff).

Key architectural tenets:
1. **Dignity & Latency First**: Conversational voice is only viable when turn latency is <= 800ms, barge-in kill latency is <= 50ms, and audio earcons respond in < 2ms.
2. **Apple-Grade Sensory Presence**: A physical MacBook notch Dynamic Island HUD (`cli/hotkey/hud_window.swift`) anchored flush to the screen that never steals keyboard focus (`.nonactivatingPanel`).
3. **Zero Invasive Permissions**: The global hotkey uses macOS Carbon `RegisterEventHotKey`, working universally without requiring invasive Accessibility or TCC prompts in macOS System Settings.
4. **Swappable Tyres**: STT, LLM, and TTS providers are swappable via environment variables and runtime settings with zero code changes.
5. **No Hallucinations / Pure Math Gates**: Every feature must have a TDD test suite in `qa/` that runs hermetically without mandatory external network dependencies.

---

## 2. Quickstart (Under 2 Minutes)

### Step 1: Dependencies & Doppler
```bash
git checkout feat/google-grade-design-system

# Python dependencies (virtualenv or conda)
pip install -r requirements.txt

# Doppler secrets (provides GROQ_API_KEY, DEEPGRAM_API_KEY, GITHUB_TOKEN)
doppler configure --project unfoundbox --config dev_personal
```

### Step 2: Compile Native Swift Hotkey & HUD
```bash
# Compile native binary with Swift optimizer
swiftc -O cli/hotkey/*.swift -o bin/pet-talk-hotkey

# Verify Carbon registration (keycode 48 = Tab, mod 0x800 = Option)
./bin/pet-talk-hotkey --check-registration
```

### Step 3: Start the Servers
```bash
# 1. Start Kokoro TTS Local Daemon (:8088)
./serve.sh

# 2. Start Pet-Talk FastAPI Duplex Server (:8089)
doppler run --project unfoundbox --config dev_personal -- \
  python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8089

# 3. Start Native Hotkey & Dynamic Island Daemon
./bin/pet-talk-hotkey start

# 4. Optional: Start Web Cockpit (:5173)
cd web && npm install && npm run dev
```

---

## 3. Keyboard & Sensory Controls Cheat Sheet

| Trigger | Context | Action |
| :--- | :--- | :--- |
| **`Option + Tab`** (Single-tap) | Idle | Wakes Donna immediately (<1.2ms mic open chime), displays notch capsule, opens mic. |
| **`Option + Tab`** (Single-tap) | Active (Speaking / Thinking / Listening) | **Instant Kill Switch**: Cuts audio in <2ms, terminates process, dismisses HUD. Does NOT restart! |
| **`Option + Tab`** (Double-tap $\le$ 350ms) | Any | Toggles persistent **Pause / Sleep Mode**. |
| **`Option + Shift + Tab`** | Any | Dedicated instant **Pause / Sleep Mode** toggle. |
| **`Escape`** | HUD hover / active turn | Instant barge-in kill. |

### CLI Management Commands
```bash
./bin/pet-talk-hotkey status       # Shows running PID & status: [ACTIVE] vs [PAUSED / SLEEP MODE]
./bin/pet-talk-hotkey kill         # Sub-10ms emergency kill switch
./bin/pet-talk-hotkey pause        # Put Donna to sleep (voice triggers muted)
./bin/pet-talk-hotkey resume       # Wake Donna back to active mode
./bin/pet-talk-hotkey toggle       # Toggle active <-> paused
./bin/pet-talk-hotkey test-audio   # Benchmark all acoustic earcons (<2ms SLA)
./bin/pet-talk-hotkey test-hud     # Interactive visual test of Dynamic Island HUD states
```

---

## 4. Architectural Mental Model

```
[ User Speaks ]
      |
      v
[ Mic Stream / Energy VAD ] (cli/audio.py or web AudioWorklet)
      |
      +---> Live RMS / Peak Telemetry ---> Dynamic Island Audio Waveform
      |
      v
[ Speech-to-Text (STT) ] (Deepgram Nova-3 / Groq Whisper / WhisperKit / MLX)
      |
      +---> Transcribed Text ---> HUD Dictation Preview & Wispr Flow Cmd+V
      |
      v
[ Router & Intent Filter ] (server/app.py)
      |
      +---> Deterministic Fast Path ("status", "stop", "who are you") [<50ms]
      |
      v
[ Spoken Brain (LLM) ] (Groq LPU compound-mini / LiteLLM proxy)
      |
      +---> VoiceProseFormatter (clamps to <=20 words, strips markdown/backticks)
      |
      v
[ Speech Synthesis (TTS) ] (Kokoro-82M :8088 / Deepgram Aura)
      |
      +---> Pre-cached RAM Stalls ("let me look into that") [0ms overhead]
      |
      v
[ Native afplay / Web Audio ]
      |
      `---> [User Interrupts / Option+Tab] ---> Barge Kill in <2ms
```

---

## 5. Model Configuration & Provider Tyres

All model choices are runtime configurable via environment variables or `POST /settings`.

### 1. Speech-to-Text (STT)
| Provider | Identifier | Speed | Notes |
| :--- | :--- | :--- | :--- |
| **Deepgram** (Default Cloud) | `deepgram` (`nova-3`) | ~120ms | Premium cloud flagship, Doppler `DEEPGRAM_API_KEY`. |
| **Groq LPU Whisper** | `groq` (`whisper-large-v3-turbo`) | ~110ms | Ultra-fast Groq LPU transcription. |
| **WhisperKit** (Apple Silicon) | `whisperkit` | ~90ms | Local CoreML on Apple Neural Engine (zero cloud dependency). |
| **MLX Whisper** (Apple Silicon) | `mlx` | ~140ms | Local Metal GPU acceleration via Apple MLX. |
| **SenseVoice** (Sovereign Fleet) | `sensevoice` | ~70ms | Hosted on sovereign fleet (`100.99.50.84:8086`). |
| **Stub** | `stub` | <1ms | Deterministic test stub for offline QA. |

### 2. Large Language Model (LLM)
| Provider | Identifier | Model | Latency (TTFT) |
| :--- | :--- | :--- | :--- |
| **Groq LPU** (Default) | `groq` | `groq/compound-mini` | ~210ms |
| **SpacePilot / LiteLLM Proxy** | `litellm` | `claude-sonnet-4-6` | ~450ms |
| **OpenAI Direct** | `openai` | `gpt-4o-mini` | ~380ms |
| **Local Fleet** | `fleet` | `llama-3.3-70b` | ~290ms |

### 3. Text-to-Speech (TTS)
| Provider | Identifier | Model / Endpoint | Quality |
| :--- | :--- | :--- | :--- |
| **Kokoro Local** (Default) | `kokoro` | Kokoro-v0.19 82M (`:8088`) | Warm, natural American female voice (`af_heart`), 24kHz. |
| **Deepgram Aura** | `deepgram` | `aura-asteria-en` | Fast cloud fallback. |
| **Stub** | `stub` | Synthetic sine WAV | <1ms offline test stub. |

---

## 6. Codebase Directory Structure

```
pet-talk/
+-- bin/                         # Compiled executables (pet-talk-cli, pet-talk-hotkey)
+-- cli/
|   +-- audio.py                 # macOS CoreAudio / sox / ffmpeg capture & energy VAD
|   +-- client.py                # Terminal interactive TUI & one-shot CLI client
|   `-- hotkey/                  # Native Swift macOS subsystem
|       +-- main.swift           # Carbon event listener, PID manager, kill switch & CLI
|       +-- hud_window.swift     # Floating Glass Capsule HUD & Hardware Notch Island
|       +-- earcons.swift        # Sub-2ms RAM-cached NSSound acoustic engine
|       +-- paste_injector.swift # Wispr Flow style cursor clipboard & keystroke paste
|       `-- config.swift         # YAML configuration loader & persistence
+-- docs/                        # Architecture specs & roadmaps
|   +-- ONBOARDING.md            # You are here
|   +-- ROADMAP.md               # Shipped milestones and future wave roadmap
|   +-- DYNAMIC-NOTCH-ISLAND-SPEC.md # Hardware notch geometry & spring physics
|   +-- SENSORY-FEEDBACK-SPEC.md # Earcons and audio design language
|   `-- DICTATION-SPEC.md        # Universal STT router and clean prose engine
+-- personas/                    # Persona prompt specifications (donna.md, zuck.md)
+-- qa/                          # TDD test suites & benchmarks
|   +-- run_all.sh               # Master verification gate (runs all 12 test suites)
|   +-- test_hotkey.py           # Carbon hotkey, kill switch & lifecycle tests
|   +-- test_hud.py              # HUD nonactivating window & geometry tests
|   +-- test_earcons.py          # Acoustic earcon latency assertions
|   +-- test_dictation_matrix.py # Universal STT router & CleanProse tests
|   `-- benchmarks/              # Voice forensics & wire latency probes
+-- server/                      # FastAPI WebSocket duplex server
|   +-- app.py                   # Duplex connection loop, router, stall cache
|   +-- providers.py             # STT/LLM/TTS swappable tyre implementations
|   `-- dictation.py             # CleanProseFormatter & VoiceProseFormatter
`-- web/                         # React 18 + Vite Cockpit UI
```

---

## 7. Quality Assurance & Pre-Flight Gate

Before submitting any Pull Request or pushing commits, you **must** run the master test suite:

```bash
bash qa/run_all.sh
```

Ensure all 12 test suites pass with `RESULT: OK`:
1. Protocol schema compliance (`qa/test_protocol.py`)
2. Persona instruction spec validity (`qa/test_persona.py`)
3. Humanizer filler & pace determinism (`qa/test_humanize.py`)
4. Latency SLA budget assertions (`qa/test_latency.py`)
5. Universal Dictation Matrix (`qa/test_dictation_matrix.py`)
6. REST API & Audio store endpoints (`qa/test_api.py`)
7. Realtime live duplex WebSocket loop (`qa/test_live_duplex.py`)
8. Terminal CLI client & energy VAD (`qa/test_cli_client.py`)
9. Native macOS Carbon hotkey & kill switch (`qa/test_hotkey.py`)
10. Hardware Notch Dynamic Island HUD (`qa/test_hud.py`)
11. Acoustic earcons sub-2ms engine (`qa/test_earcons.py`)
12. WebSocket resilience & dead socket safety (`qa/test_ws_resilience.py`)
