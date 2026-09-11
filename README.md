# Pet-Talk — Full-Duplex Conversational Voice Loop & Ambient Presence

**Pet-Talk** is a local-first, full-duplex conversational voice loop and sensory presence engine designed for ambient AI companions (e.g. Donna, your Chief of Staff).

User interrupts anytime, the agent stalls naturally (`"let me look into that"`), synthesizes streaming speech via Kokoro 82M, and renders real-time acoustic feedback into an Apple-grade MacBook camera notch Dynamic Island.

---

## Quickstart (Under 2 Minutes)

```bash
# 1. Start Kokoro TTS Local Daemon (:8088)
./serve.sh

# 2. Start Pet-Talk FastAPI Duplex Server (:8089)
doppler run --project unfoundbox --config dev_personal -- \
  python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8089

# 3. Start Native Hotkey & Dynamic Island Daemon (<0.1% CPU)
./bin/pet-talk-hotkey start

# 4. Optional: Start Web Cockpit (:5173)
cd web && npm install && npm run dev
```

---

## Global Hotkey & Sensory Controls

Uses native macOS Carbon `RegisterEventHotKey` (works globally with **zero** invasive Accessibility or TCC permissions in System Settings):

| Shortcut | Context | Behavior |
| :--- | :--- | :--- |
| **`Option + Tab`** (Single-tap) | Idle | Wakes Donna immediately (<1.2ms), opens mic, displays notch capsule. |
| **`Option + Tab`** (Single-tap) | Active (Speaking / Thinking / Listening) | **Emergency Kill Switch**: Cuts audio in <2ms via Darwin libproc, terminates turn, dismisses HUD. Does NOT restart! |
| **`Option + Tab`** (Double-tap $\le$ 350ms) | Any | Toggles persistent **Pause / Sleep Mode**. |
| **`Option + Shift + Tab`** | Any | Dedicated instant **Pause / Sleep Mode** toggle. |
| **`Escape`** | HUD hover / active turn | Instant barge-in kill. |

### CLI Management Commands
```bash
./bin/pet-talk-hotkey status       # Shows PID & state: [ACTIVE] vs [PAUSED / SLEEP MODE]
./bin/pet-talk-hotkey kill         # Sub-10ms emergency audio & process kill switch
./bin/pet-talk-hotkey pause        # Put Donna to sleep (voice triggers muted)
./bin/pet-talk-hotkey resume       # Wake Donna back to active mode
./bin/pet-talk-hotkey toggle       # Toggle active <-> paused
./bin/pet-talk-hotkey test-audio   # Benchmark RAM-cached acoustic earcons (<2ms SLA)
./bin/pet-talk-hotkey test-hud     # Interactive visual test of Dynamic Island HUD states
```

---

## Core Capabilities

1. **Sub-Second Conversational Latency**:
   - Groq LPU LLM inference (`groq/compound-mini`, TTFT ~210ms).
   - Clamped verbosity: <= 20 spoken words, 1–2 punchy sentences.
   - `VoiceProseFormatter`: strips markdown asterisks, backticks, code fences, and bullet points.
   - 0ms stall playback via pre-cached RAM audio buffers.
   - Deterministic fast path (<50ms for control commands).

2. **MacBook Camera Notch Dynamic Island HUD** (`cli/hotkey/hud_window.swift`):
   - Physical camera notch envelope (`auxiliaryTopLeftArea` / `auxiliaryTopRightArea`), squircle curvature (r=20px), concave ear fillets (r=10px).
   - Apple fluid spring physics ("The Drip" entrance, "Suction" retraction, 3-cycle error shake).
   - Non-activating panel (`.nonactivatingPanel`, zero keyboard focus stealing from IDE or terminal).
   - Live Acoustic Visualizer: Real-time RMS and Peak amplitude rendered dynamically without fake looping animations.
   - Multi-line dynamic height expansion (60px -> 76px -> 96px -> 110px clamp) with semantic breadcrumb sanitization.

3. **Universal Dictation Matrix & Clean Prose**:
   - Swappable STT providers: Deepgram Nova-3, Groq Whisper LPU, OpenAI Whisper, WhisperKit CoreML (Apple Neural Engine), MLX Whisper (Metal GPU), SenseVoice fleet.
   - `CleanProseFormatter`: strips fillers ("uh", "um"), cleans repeated stutters, formats code tokens (`app.py`, `git commit`).
   - Wispr Flow style cursor paste injection (`Cmd+V` via `CGEvent`).

4. **Acoustic Earcon Engine** (`cli/hotkey/earcons.swift`):
   - Pre-cached NSSound in RAM (<2ms latency): `Tink.aiff` (Mic Open), `Pop.aiff` (Cutoff), `Bottle.aiff` (Barge Kill), `Basso.aiff` (Error).

---

## Repository Layout

```
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
+-- docs/                        # Specifications & developer guides
|   +-- ONBOARDING.md            # Master onboarding guide for new engineers & agents
|   +-- ROADMAP.md               # Shipped milestones and future wave roadmap
|   +-- DYNAMIC-NOTCH-ISLAND-SPEC.md # Hardware notch geometry & spring physics
|   +-- SENSORY-FEEDBACK-SPEC.md # Earcons and audio design language
|   +-- DICTATION-SPEC.md        # Universal STT router and clean prose engine
|   `-- TECH-DESIGN.md           # Core wire protocol & duplex pipeline
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

## Documentation & Deep Dives

- **[Developer & Agent Onboarding Guide](docs/ONBOARDING.md)**: Setup, architecture, model switches, and pre-flight checklist.
- **[Master Roadmap & Evolution Plan](docs/ROADMAP.md)**: Shipped Wave 1 & 2 receipts, upcoming Wave 3 (WebRTC/Opus streaming, on-device SLMs) & Wave 4 (screen grounding, autonomous delegation).
- **[Hardware Notch Dynamic Island Specification](docs/DYNAMIC-NOTCH-ISLAND-SPEC.md)**: Physical notch geometry, fluid spring physics, and acoustic truth rendering.
- **[Sensory Feedback Specification](docs/SENSORY-FEEDBACK-SPEC.md)**: Sub-2ms acoustic earcon design and sound packs.
- **[Universal Dictation Matrix Specification](docs/DICTATION-SPEC.md)**: Swappable STT matrix, CleanProse grammar, and Wispr Flow injection.

---

## Verification & QA Gate

To verify all 12 test suites:

```bash
bash qa/run_all.sh
```
