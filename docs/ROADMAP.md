# Pet-Talk — Master Roadmap & Evolution Plan

This roadmap tracks the completed milestones and forward-looking evolution of **Pet-Talk** from an initial voice loop prototype into a sovereign ambient agent companion.

---

## 1. Wave Status Matrix

| Wave | Theme | Focus | Status |
| :--- | :--- | :--- | :--- |
| **Wave 1** | The Foundation | Full-duplex WebSocket loop, personas (Donna, Zuck), deterministic humanizer, QA test harness. | **SHIPPED** |
| **Wave 2** | Ambient Presence | Native Carbon hotkey (`Option + Tab`), MacBook Notch Dynamic Island HUD, sub-2ms earcons, universal dictation matrix, sub-second latency, kill switch & pause mode. | **SHIPPED** |
| **Wave 3** | Audio Streaming & Sovereign Edge | WebRTC / Opus duplex streaming, local on-device small LLMs (MLX/Ollama), acoustic echo cancellation (AEC), voice activity classification. | **PARTIALLY SHIPPED** — eyes lane (`server/eyes.py`) and AX opt-in grounding (`PET_TALK_AX`) shipped; WebRTC/Opus streaming and on-device SLMs still open |
| **Wave 4** | Autonomous Brain & Multimodal Context | Long-term episodic memory (`agentworth` indexing), OS-level screen grounding via Accessibility AXTree, multi-agent pet delegation, proactive ambient nudges. | **PLANNED** |

---

## 2. Shipped Milestones (Waves 1 & 2 Receipts)

### Wave 1: Core Voice Loop & Persona Architecture
- **Full-Duplex WebSocket Server** (`server/ws.py`, `server/turn.py` today —
  split out of the original single `server/app.py` since Wave 1 shipped):
  - Async WebSocket protocol (`/ws`) with `user.start`/`user.chunk`/`user.stop`/`user.text`, `agent.sentence`, `barge`.
  - Two-tier worker lanes: FAST lane (instant stall audio playback) and WORKER lane (streaming sentence generation).
- **Persona Instruction Specifications** (`personas/`):
  - 12-field deterministic persona schemas (`donna.md`, `zuck.md`, `jarvis.md`).
- **Humanizer Engine** (`humanizer/`):
  - Deterministic fillers, conversational breath markers, and speech rate modulation.
- **TDD Quality Harness** (`qa/`):
  - Hermetic test suites guaranteeing SLA budgets (turn <= 800ms, stall <= 400ms, barge <= 100ms).

### Wave 2: Sensory Ambient Presence & High-Performance Hotkey

`cli/hotkey/*.swift` may be under concurrent edit by another lane as of this
branch — the bullets below describe what the module map says these files are
for (`docs/SPEC.md` §3), not a freshly re-verified line-by-line read. Specific
sub-millisecond latency numbers (kill-switch timing, earcon latency, wake
latency) from the earlier version of this section could not be reverified
against the current Swift source in this pass and were dropped rather than
repeated on faith. Verify against `qa/test_hotkey.py` / `qa/test_earcons.py`
and the live binary before quoting a number here again.

- **Notch Dynamic Island HUD** (`cli/hotkey/hud_window.swift`): floating,
  non-activating panel anchored to the camera notch area; does not steal
  keyboard focus from an editor or terminal.
- **Native macOS global hotkey** (`cli/hotkey/main.swift`): Carbon
  `RegisterEventHotKey` (keycode 48 / Tab, modifier Option) — no
  Accessibility/TCC permission needed for the hotkey itself. (Separately,
  `PET_TALK_AX=1` grounding *does* need the Accessibility permission — see
  `docs/SPEC.md` §7. Those are two different things; don't conflate them.)
  Kill switch, pause/resume, and status subcommands: `pet-talk-hotkey kill
  | pause | resume | toggle | status`.
- **Conversational conciseness**: `VoiceProseFormatter` strips markdown
  (asterisks, backticks, code fences, bullets) and clamps spoken sentences
  to `MAX_SENTENCE_WORDS = 20` (`server/speech.py`). Deterministic control
  fast-path answers `status`/`stop`/`who are you` with no LLM round trip
  (`server/control.py`).
- **Universal dictation matrix**: swappable STT providers — Deepgram,
  Groq Whisper, OpenAI Whisper, WhisperKit (CoreML), MLX Whisper,
  faster-whisper, SenseVoice (see `server/providers/stt.py`).
  `CleanProseFormatter` (`server/dictation.py`) strips fillers and repeated
  stutters from transcripts.
- **Acoustic earcon engine** (`cli/hotkey/earcons.swift`): RAM-cached
  `NSSound` playback for mic-open / cutoff / barge-kill / error events.

---

## 3. Wave 3: Audio Streaming & Sovereign Edge (partially shipped)

**Shipped**: the eyes lane (`server/eyes.py` — `user.attach`, local OCR via
`zrv`, `eyes.received`/`eyes.text` frames) and AX opt-in screen grounding
(`PET_TALK_AX=1`, `server/grounding.py:ax_snapshot`). See `docs/SPEC.md` §7-8
for the actual contract, including the describe-mode latency finding
(`apple-fm` measured at ~102s per screenshot, so `describe` ships disabled).

**Still open**: WebRTC/Opus audio streaming and on-device small LLMs, below.

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

### 4.2 Screen grounding: AX is the default, OCR is only for images/PDF

This shipped, partially, in Wave 3 — this section is no longer a future
design, it is what's actually there plus what's still missing:

- **The AX route is the default answer to "what is Saurabh looking at."**
  `PET_TALK_AX=1` turns on `server/grounding.py:ax_snapshot()`, which shells
  out to `pet-talk-hotkey ax` and gets back one JSON line
  (`{"ok","app","window","selection","path"}`) describing the frontmost
  app/window/selection. This needs the macOS Accessibility permission
  granted to the hotkey binary — see `docs/SPEC.md` §7 for why that's a real
  trade against "zero permissions," not a rounding error.
- **OCR (`server/eyes.py`, the zero-vision lane) is for images and PDFs
  only** — screenshots, receipts, documents the user explicitly attaches
  over `user.attach`. It is not how the agent finds out what's on screen
  ambiently; that's AX's job. Don't route a screenshot through the AX path
  or a "what am I looking at" question through OCR — they answer different
  questions.
- **Still missing**: extracting selected text or a terminal pane's buffer
  via AX beyond what `pet-talk-hotkey ax` already returns, and folding that
  into the ambient prompt context automatically rather than only on request.

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
4. **Barge-in guarantee**: the user must always be able to cut Donna off cleanly, within the `barge_ms` budget in `qa/budgets.json` (100ms).
