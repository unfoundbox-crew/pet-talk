# Pet-Talk — Master Roadmap & Evolution Plan

This roadmap tracks the completed milestones and forward-looking evolution of **Pet-Talk** from an initial voice loop prototype into a sovereign ambient agent companion.

---

## 1. Wave Status Matrix

| Wave | Theme | Focus | Status |
| :--- | :--- | :--- | :--- |
| **Wave 1** | The Foundation | Full-duplex WebSocket loop, personas (Donna, Zuck), deterministic humanizer, QA test harness. | **SHIPPED** |
| **Wave 2** | Ambient Presence | Native Carbon hotkey (`Option + Tab`), MacBook Notch Dynamic Island HUD, sub-2ms earcons, universal dictation matrix, sub-second latency, kill switch & pause mode. | **SHIPPED** |
| **Wave 3** | Audio Streaming & Sovereign Edge | WebRTC / Opus duplex streaming, local on-device small LLMs (MLX/Ollama), acoustic echo cancellation (AEC), voice activity classification. | **UP NEXT** |
| **Wave 4** | Autonomous Brain & Multimodal Context | Long-term episodic memory (`agentworth` indexing), OS-level screen grounding via Accessibility AXTree, multi-agent pet delegation, proactive ambient nudges. | **PLANNED** |

---

## 2. Shipped Milestones (Waves 1 & 2 Receipts)

### Wave 1: Core Voice Loop & Persona Architecture
- **Full-Duplex WebSocket Server** (`server/app.py`):
  - Async WebSocket protocol (`/ws`) supporting bidirectional audio frame streaming (`user.audio_chunk`, `agent.sentence`, `barge`).
  - Two-tier worker lanes: FAST lane (instant stall audio playback) and WORKER lane (streaming sentence generation).
- **Persona Instruction Specifications** (`personas/`):
  - 12-field deterministic persona schemas (`donna.md`, `zuck.md`, `jarvis.md`).
- **Humanizer Engine** (`humanizer/`):
  - Deterministic fillers, conversational breath markers, and speech rate modulation.
- **TDD Quality Harness** (`qa/`):
  - Hermetic test suites guaranteeing SLA budgets (turn <= 800ms, stall <= 400ms, barge <= 100ms).

### Wave 2: Sensory Ambient Presence & High-Performance Hotkey
- **Hardware Notch Dynamic Island HUD** (`cli/hotkey/hud_window.swift`):
  - Physical camera notch envelope (`auxiliaryTopLeftArea` / `auxiliaryTopRightArea`), squircle curvature (r=20px), concave ear fillets (r=10px).
  - Apple fluid spring physics ("The Drip" entrance, "Suction" retraction, 3-cycle error shake).
  - Non-activating panel (`.nonactivatingPanel`, zero keyboard focus stealing from IDE or terminal).
  - Live Acoustic Visualizer: Real-time RMS and Peak amplitude rendered dynamically without fake looping animations.
  - Multi-line dynamic height expansion (60px -> 76px -> 96px -> 110px clamp) with semantic breadcrumb sanitization.
- **Native macOS Global Hotkey** (`cli/hotkey/main.swift`):
  - Carbon `RegisterEventHotKey` on keycode 48 (`kVK_Tab`) with modifier `0x0800` (`optionKey`).
  - Zero Accessibility/TCC permission prompts required.
  - Sub-2ms instant barge-in kill via Darwin `libproc` process scanning and `SIGKILL`.
- **Smart Kill Switch & Pause/Sleep Mode**:
  - Single-tap `Option + Tab` when active: cuts audio in <2ms, terminates `pet-talk-cli`, dismisses HUD capsule without restarting turn.
  - Double-tap `Option + Tab` ($\le$ 350ms): toggles persistent Pause / Sleep Mode.
  - Dedicated shortcut: `Option + Shift + Tab` for instant Pause / Resume toggle.
  - CLI subcommands: `pet-talk-hotkey kill`, `pause`, `resume`, `toggle`, `status`.
- **Sub-Second Voice Latency & Conversational Conciseness**:
  - LLM default switched to Groq LPU (`groq/compound-mini`, TTFT ~210ms).
  - Prompt verbosity clamped to <= 20 spoken words, 1–2 punchy sentences.
  - `VoiceProseFormatter`: strips markdown asterisks, backticks, code fences, and bullet points.
  - Pre-cached stall audio in RAM at startup (`_STALL_AUDIO_CACHE`) for 0ms synthesis overhead.
  - Deterministic control fast-path (<50ms for "status", "stop", "who are you").
- **Universal Dictation Matrix & Clean Prose**:
  - Swappable STT providers: Deepgram Nova-3, Groq Whisper LPU, OpenAI Whisper, WhisperKit CoreML, MLX Whisper, SenseVoice fleet.
  - `CleanProseFormatter`: strips fillers ("uh", "um"), cleans repeated stutters, formats code tokens (`app.py`, `git commit`).
  - Wispr Flow style cursor paste injection (`Cmd+V` via `CGEvent`).
- **Acoustic Earcon Engine** (`cli/hotkey/earcons.swift`):
  - Pre-cached NSSound in RAM (<2ms latency): `Tink.aiff`, `Pop.aiff`, `Bottle.aiff`, `Basso.aiff`.

---

## 3. Wave 3: Audio Streaming & Sovereign Edge (Next Up)

### 3.1 WebRTC / Opus Audio Streaming Loop
- **Problem**: Current audio capture buffers PCM16 in small chunks and sends WAV over WebSocket, creating slight turn-taking packetization overhead.
- **Architecture**:
  - Implement bidirectional Opus-encoded WebRTC audio channel or streaming raw PCM WebSockets.
  - Server-side streaming STT: stream phonemes into STT session continuously without waiting for speech endpointing.
- **Target SLA**: First synthesized audio phoneme within 400ms of user speech completion.

### 3.2 Acoustic Echo Cancellation (AEC) & Hardware Mic Decoupling
- **Problem**: When Donna speaks through MacBook built-in speakers while the mic is open, acoustic bleed can falsely re-trigger STT or cancel playback.
- **Architecture**:
  - Native macOS AudioUnit Voice-Processing I/O (`kAudioUnitSubType_VoiceProcessingIO`) with built-in Darwin hardware AEC.
  - Software spectral subtraction fallback when external Bluetooth headphones (AirPods) or external mics are used.

### 3.3 On-Device Small Language Models (Apple Silicon Edge)
- **Problem**: Cloud LLMs (Groq, Anthropic, OpenAI) require internet connectivity and incur network egress.
- **Architecture**:
  - Support on-device quantized LLM via Apple MLX (`mlx-lm`) or local Ollama / SpacePilot fleet proxy.
  - Target model candidates: Llama 3.2 3B Instruct (4-bit), DeepSeek 1.5B Distill, Qwen 2.5 3B.
  - Memory footprint: < 2.5GB Unified Memory on M-series chips with > 65 tokens/sec generation.

---

## 4. Wave 4: Autonomous Brain & Ambient Grounding (Long-Term)

### 4.1 Long-Term Episodic Grounding (`agentworth` & SQLite Vault)
- **Problem**: Conversation state is currently session-scoped. When Donna restarts or a turn finishes, past decisions are not persistently grounded across days.
- **Architecture**:
  - Local SQLite vector and session store indexing past turns, preferences, projects, and commits.
  - Post-compaction grounding protocol integration: inject active project context (`git status`, active tasks, Doppler config) on cold wake.

### 4.2 Zero-Vision OS Screen Grounding (Accessibility AXTree)
- **Problem**: Donna cannot currently see what code or application Saurabh is looking at without burning heavy multimodal vision tokens.
- **Architecture**:
  - Query macOS Accessibility API (`AXUIElementCopyAttributeValue`) to extract focused window title, active file path, selected text, or Cursor terminal buffer in pure text (<5ms, 0 visual tokens).
  - Ambient prompt context: *"Saurabh is currently editing `server/app.py:120` in Cursor and inspecting terminal pane 2"*.

### 4.3 Multi-Agent Pet Fleet & Autonomous Delegation
- **Problem**: Donna is currently a single conversational agent. Complex tasks (running long test suites, git refactoring) block the voice loop.
- **Architecture**:
  - Donna acts as the Chief of Staff dispatcher:
    ```
    "Donna, refactor the database queries in auth.py and let me know when tests pass."
    ```
  - Donna dispatches a background subagent (via AGY CLI or Herdr multiplexer) and displays a subtle `[Running]` breadcrumb in the physical camera notch.
  - When the subagent finishes, Donna plays a soft chime and delivers a 1-sentence spoken summary: *"Tests are green on auth.py, commit is ready."*

---

## 5. Architectural Non-Negotiables

Across all future milestones, the following rules remain invariant:
1. **Never steal focus**: The HUD window must remain `.nonactivatingPanel` and never disrupt active typing in editors or terminals.
2. **Deterministic TDD verification**: No feature merges into `main` without automated tests in `qa/run_all.sh`.
3. **Monochromatic terminal discipline**: No raw Mermaid blocks or heavy ornate Unicode box art that corrupts terminal rendering.
4. **Sub-50ms barge-in guarantee**: The user must always be able to cut Donna off cleanly without latency.
