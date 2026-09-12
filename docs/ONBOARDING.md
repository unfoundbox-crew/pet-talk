# Pet-Talk — Developer & Agent Onboarding Guide

Welcome to **Pet-Talk**. This document is the primary onboarding guide for human engineers and autonomous agents working on the codebase.

---

## 1. Executive Summary & Philosophy

Pet-Talk is a local-first, low-latency, full-duplex conversational voice and sensory presence engine designed for ambient AI companions (e.g. Donna, your Chief of Staff).

Key architectural tenets:
1. **Latency first**: the budgets in `qa/budgets.json` are the target (stall
   400ms, barge 100ms, turn 800ms p50 / 1200ms worst). What's actually
   measured today is stub-provider only — see `docs/SPEC.md` §9.
2. **Notch-anchored HUD**: `cli/hotkey/hud_window.swift` is a floating,
   non-activating panel (`.nonactivatingPanel`) that must never steal
   keyboard focus from an editor or terminal.
3. **The hotkey itself needs no Accessibility permission** (Carbon
   `RegisterEventHotKey`). `PET_TALK_AX=1` grounding is a separate, opt-in
   feature that *does* need it — see `docs/SPEC.md` §7.
4. **Swappable tyres**: STT, LLM, and TTS providers are swappable via
   environment variables and `POST /settings`, zero code changes.
5. **No fake greens**: every feature has a suite in `qa/` that runs
   hermetically where possible, and SKIPs honestly (never silently passes)
   when it needs a daemon or credential that isn't there.

---

## 2. Quickstart

### Step 1: Dependencies & Doppler
```bash
# Python dependencies (virtualenv or conda)
pip install -r server/requirements.txt

# Doppler secrets (provider keys, per server/settings.py)
doppler configure --project unfoundbox --config dev_personal
```

### Step 2: Build the native Swift hotkey & HUD

`bin/` is not tracked in git (`.gitignore`: `bin/*`, kept only via
`bin/.gitkeep`). Build with:

```bash
make build-hotkey    # runs cli/hotkey/build.sh bin/
```

The real `swiftc -O` build never runs on a laptop — the fan rule routes it
to `ssh air`. `cli/hotkey/build.sh` is owned by another lane and, as of this
branch, does not exist yet in this checkout; `make build-hotkey` will fail
until it lands.

### Step 3: Start the servers
```bash
# 1. Kokoro TTS local daemon (:8088)
./serve.sh

# 2. pet-talk FastAPI duplex server (:8089)
doppler run --project unfoundbox --config dev_personal -- \
  python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8089

# 3. Native hotkey & HUD daemon (once built)
./bin/pet-talk-hotkey start

# 4. Optional: web cockpit (:5173)
cd web && npm install && npm run dev
```

See `README.md` for the full environment-variable table (provider selection,
`PET_TALK_SILENT`, `PET_TALK_AX`, `PET_TALK_GROUNDING_REPOS`, `EYES_ENGINE`).

---

## 3. Keyboard & Sensory Controls Cheat Sheet

| Trigger | Context | Action |
| :--- | :--- | :--- |
| **`Option + Tab`** (Single-tap) | Idle | Wakes Donna, displays notch capsule, opens mic. |
| **`Option + Tab`** (Single-tap) | Active (Speaking / Thinking / Listening) | **Instant Kill Switch**: cuts audio, terminates process, dismisses HUD. Does NOT restart! |
| **`Option + Tab`** (Double-tap $\le$ 350ms) | Any | Toggles persistent **Pause / Sleep Mode**. |
| **`Option + Shift + Tab`** | Any | Dedicated instant **Pause / Sleep Mode** toggle. |
| **`Escape`** | HUD hover / active turn | Instant barge-in kill. |

### CLI Management Commands
```bash
./bin/pet-talk-hotkey status       # Shows running PID & status: [ACTIVE] vs [PAUSED / SLEEP MODE]
./bin/pet-talk-hotkey kill         # Emergency kill switch
./bin/pet-talk-hotkey pause        # Put Donna to sleep (voice triggers muted)
./bin/pet-talk-hotkey resume       # Wake Donna back to active mode
./bin/pet-talk-hotkey toggle       # Toggle active <-> paused
./bin/pet-talk-hotkey test-audio   # Benchmark acoustic earcons
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
      v
[ user.stop over /ws ] ---> STT off-loop (server/turn.py:transcribe_off_loop)
      |
      v
[ Deterministic control? ] (server/control.py: stop / status / who-are-you)
      | no
      v
[ Router ] (providers.llm.route(text) -> "stall" | "answer", server/turn.py)
      |
      +--- stall path: agent.stall (cached phrase, server/stall.py) then
      |                run_speech streams the worker's answer behind it
      |
      `--- direct path: run_speech streams one answer, no stall
      |
      v
[ run_speech ] (server/speech.py): LLM producer -> SpeakQueue -> TTS consumer
      |
      +---> agent.sentence per sentence (audio_url, word_times, estimated)
      |
      v
[ Client playback ]
      |
      `---> [barge frame] ---> Session.barge() cancels the turn task,
             flushes SpeakQueue, reports the true dropped count
```

---

## 5. Model Configuration & Provider Tyres

All model choices are runtime configurable via environment variables or
`POST /settings` (`server/settings.py`, `server/provider_factory.py`). No
latency numbers are quoted here — real-engine latency is NOT MEASURED as of
this writing (see `docs/SPEC.md` §9). The identifiers below are exactly the
strings `make_stt`/`make_llm`/`make_tts` accept.

### 1. Speech-to-Text (`STT_PROVIDER`)
| Identifier(s) | Backend | Needs |
| :--- | :--- | :--- |
| `deepgram` | Deepgram Nova-3 | `DEEPGRAM_API_KEY` |
| `groq` | Groq Whisper (`whisper-large-v3-turbo`) | `GROQ_API_KEY` |
| `openai` / `openai-whisper` / `whisper-openai` | OpenAI Whisper (`whisper-1`) | `OPENAI_API_KEY` |
| `whisperkit` / `whisper-kit` / `coreml` / `ane` | WhisperKit CoreML, local | `whisperkit-cli` on PATH |
| `faster-whisper` / `faster_whisper` / `whisper` / `local` / `whisper-local` | faster-whisper, local | nothing |
| `mlx` / `mlx-whisper` | MLX Whisper, local | nothing |
| `sensevoice` / `fleet` / `sovereign` | SenseVoice daemon | `SENSEVOICE_BASE_URL` (default `127.0.0.1:8086`) |
| `stub` | Deterministic canned text | nothing |

### 2. Large Language Model (`LLM_PROVIDER`)
| Identifier(s) | Backend | Needs |
| :--- | :--- | :--- |
| `haiku` / `claude-haiku` / `claude` | Anthropic (default model `claude-3-5-haiku-20241022`) | `ANTHROPIC_API_KEY` |
| `opencode` / `zen` / `opencode-zen` | OpenCode Zen proxy (default model `flash-3.8`) | `OPENCODE_API_KEY` (or `OPENCODE_GO_KEY`/`OPENCODE_LENOVO_KEY`) |
| `gemini` / `google` / `flash` | Gemini via OpenAI-compatible endpoint (default `gemini-2.5-flash`) | `GEMINI_PRIMARY_API_KEY` (or `GOOGLE_API_KEY`) |
| `groq` | Groq (default `groq/compound-mini`) | `GROQ_API_KEY` |
| `openai` / `gpt` | OpenAI (default `gpt-5-nano`) | `OPENAI_API_KEY` |
| `litellm` / `fleet` / `local` | Self-hosted LiteLLM proxy (default `claude-sonnet-4-6`) | `LITELLM_BASE_URL`/`LLM_BASE_URL`, a key |
| `stub` | Deterministic 3-sentence canned stream | nothing |

### 3. Text-to-Speech (`TTS_PROVIDER`)
| Identifier(s) | Backend | Needs |
| :--- | :--- | :--- |
| `smallest` / `smallest-ai` / `smallest_ai` / `waves` | Smallest.ai (voice `meher` default) | `SMALLEST_API_KEY` |
| `kokoro` (default) | Kokoro daemon | `KOKORO_BASE_URL` (default `127.0.0.1:8088`) |
| `elevenlabs` | ElevenLabs | key per `server/providers/tts.py` |
| `deepgram` | Deepgram Aura | key per `server/providers/tts.py` |
| `stub` | Synthetic sine WAV | nothing |

Kokoro's `word_times` are always `estimated: true` — see `docs/SPEC.md` §5.2.

---

## 6. Codebase Directory Structure

See `README.md`'s "Repository layout" for the tree kept in sync with `ls` —
this doc doesn't duplicate it. Read `docs/SPEC.md` §3 for the module map
(file → job, one line each), which is more precise than a tree.

---

## 7. Quality Assurance & Pre-Flight Gate

Before submitting any Pull Request or pushing commits, run the QA gate:

```bash
make qa           # bash qa/run_all.sh
make qa-silent    # PET_TALK_SILENT=1 — no audio, no daemons, safe overnight
make qa-real      # PET_TALK_REAL_ENGINE=1 — also runs real-engine suites
```

`qa/run_all.sh --list` prints the exact, current suite table (label, kind,
path) with no side effects — that list is the authority on suite count and
names, not a hand-copied list in this doc. As of this branch it's 22 entries;
trust the `--list` output over any number written down here, since a suite
gets added or removed without every doc getting a matching edit.
