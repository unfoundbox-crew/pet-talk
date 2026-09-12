# pet-talk

Local-first, full-duplex conversational voice loop. The user can interrupt any
time, the agent stalls naturally while it thinks, and speech keeps streaming
behind the stall without a gap. Every STT/LLM/TTS backend is a config swap,
never a code change.

Full contract: [`docs/SPEC.md`](docs/SPEC.md). This file is the quickstart.

**pet-talk is public.** Night runs must stay silent — see `PET_TALK_SILENT`
below and law 7 in `AGENTS.md`.

## Quickstart

All three default tyres are local or proxied, and all three were chosen on
measurement rather than on which API key happens to be in the environment
(`docs/SPEC.md` §9.1 has the numbers and the rejected candidates):

| | default | needs |
|---|---|---|
| STT | `faster-whisper` (`tiny.en`) | `faster-whisper`, no key |
| LLM | `litellm` @ `gpt-oss-120b-groq` | a LiteLLM proxy at `$LITELLM_BASE_URL` |
| TTS | `kokoro-local` (in-process Kokoro-82M) | `mlx-audio` + `misaki[en]`, Apple Silicon |

```bash
# 1. Python deps
pip install -r server/requirements.txt

# 2. The default TTS tyre runs in-process, so it needs mlx-audio in the SAME
#    interpreter that runs the server. On this machine that env is
#    ~/miniconda3/envs/local-ml-py311 (never a fresh venv — see ~/code/CLAUDE.md):
pip install mlx-audio 'misaki[en]'     # ~330MB of Kokoro weights on first use
#    No Apple Silicon, or no mlx-audio? TTS_PROVIDER=kokoro uses the SpacePilot
#    daemon below instead (5x slower, measured), and ./serve.sh starts it:
# ./serve.sh

# 3. pet-talk FastAPI duplex server (:8089). LITELLM_BASE_URL names the proxy —
#    no host is baked into the tree.
LITELLM_BASE_URL=http://127.0.0.1:8000/v1 \
  doppler run --project unfoundbox --config dev_personal -- \
  ~/miniconda3/envs/local-ml-py311/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8089

# 4. Optional: web cockpit (:5173)
cd web && npm install && npm run dev
```

### Building the hotkey daemon

`bin/` is not tracked (see `.gitignore`; `bin/*` is ignored except `.gitkeep`).
Build it with:

```bash
make build-hotkey
```

This runs `cli/hotkey/build.sh bin/` — the real `swiftc -O` build never runs
on a laptop (the fan rule), it runs on `ssh air`. As of this branch,
`cli/hotkey/build.sh` does not exist in this checkout yet; `make build-hotkey`
will fail until another lane lands it.

### Studio token

Every mutating HTTP route and the `/ws` handshake require a token
(`server/auth.py`). The server resolves it in order — env `STUDIO_TOKEN` →
the file named by `STUDIO_TOKEN_FILE` → one it generates at startup into
`.qa-scratch/studio.token` (path logged once, value never) — and
`cli/client.py` / the web cockpit read the same three sources, so a local
client picks it up with no extra config. Send it as header
`X-Studio-Token`; the WS handshake also accepts `?token=<token>` since
browsers can't set headers on `new WebSocket()`. Reads (`/health`,
`/voices`, `/audio/{id}`, and the `GET` forms of `/settings`, `/personas`,
`/ledger`) stay open. `*_base_url` fields in `POST /settings` are checked
against an allowlist — loopback, RFC1918, or a host in
`PET_TALK_ALLOWED_HOSTS` — so a mutating request can't repoint a stored
credential at an arbitrary host. Full contract: `docs/SPEC.md` §6.1.

## Environment variables

Provider selection and credentials, from `server/settings.py` and
`server/provider_factory.py`. None of these have baked-in defaults in the
tree — every credential comes from the environment or a `POST /settings` call.

| Variable | Purpose |
|---|---|
| `STT_PROVIDER` | `faster-whisper`/`local` \| `deepgram` \| `groq` \| `openai`/`openai-whisper` \| `whisperkit` \| `mlx` \| `sensevoice` \| `stub`. Defaults to `faster-whisper` — the only tyre measured inside the 150ms budget from this machine (143ms p50, against Groq's 306ms and Deepgram's 1432ms, both of which are paying a real HTTPS round trip). |
| `LLM_PROVIDER` | `litellm`/`fleet`/`local` \| `haiku`/`claude` \| `opencode`/`zen` \| `gemini`/`google`/`flash` \| `groq` \| `openai`/`gpt` \| `stub`. Defaults to `litellm` — the proxy is the house route, and it is no longer conditional on which key is present. |
| `TTS_PROVIDER` | `kokoro-local` \| `kokoro` (SpacePilot daemon) \| `smallest`/`smallest-ai`/`waves` \| `elevenlabs` \| `deepgram` \| `stub`. Defaults to `kokoro-local`: the same Kokoro-82M weights in-process, 237ms p50 against the daemon's 1379ms and Deepgram Aura's 1939ms. |
| `LITELLM_MODEL` | Model the `litellm` provider asks the proxy for. Defaults to `gpt-oss-120b-groq` (512ms p50 to first content delta). `gemini-3.7-flash` was the intended default and is out of quota on the proxy's Gemini key. |
| `KOKORO_LOCAL_MODEL` / `KOKORO_LOCAL_WARM` | Kokoro repo for the in-process tyre (default `prince-canuma/Kokoro-82M`), and `0` to skip the background model warmup at startup. |
| `WHISPER_MODEL` | faster-whisper model (default `tiny.en`). `base.en` is more accurate and ~190ms slower. |
| `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `SMALLEST_API_KEY`, `ANTHROPIC_API_KEY`, `OPENCODE_API_KEY` (or `OPENCODE_GO_KEY`/`OPENCODE_LENOVO_KEY`), `GEMINI_PRIMARY_API_KEY` (or `GOOGLE_API_KEY`) | Per-provider credentials. |
| `LITELLM_BASE_URL` / `LLM_BASE_URL` | Self-hosted LiteLLM proxy base URL, for the `litellm`/`fleet`/`local` LLM provider. Defaults to `http://127.0.0.1:8000/v1` — localhost, never a hardcoded tailnet address. |
| `LLM_MODEL`, `LLM_API_KEY` | Generic overrides that apply to any LLM provider whose own env chain doesn't already claim them. |
| `KOKORO_BASE_URL` | Kokoro TTS daemon base URL. Default `http://127.0.0.1:8088`. |
| `SENSEVOICE_BASE_URL` | SenseVoice STT base URL. Default `http://127.0.0.1:8086`. |
| `VAD_SILENCE_MS` | Silence duration that ends a turn. Default `600`. |
| `PET_TALK_SILENT` | `1` = nothing plays audio, nothing starts an audio daemon. Required for any overnight/CI run. |
| `PET_TALK_REAL_ENGINE` | `1` = also run the real-engine QA suites (`qa/test_real_engine_e2e.py`, `qa/test_voice_analyzer.py`), otherwise SKIP. |
| `PET_TALK_AX` | `1` = enable AX screen grounding via `pet-talk-hotkey ax`. Needs the macOS Accessibility permission for that binary — see `docs/SPEC.md` §7. |
| `PET_TALK_GROUNDING_REPOS` | Colon-separated extra repo paths to report git-head lines for in the system prompt. Empty by default. |
| `EYES_ENGINE` | `zrv` (default) \| `stub`, for the eyes/OCR lane. |
| `PET_TALK_CORS_ORIGINS` | Comma-separated allowed origins. Default `http://localhost:5173`. |
| `STUDIO_TOKEN` | The studio token, checked first. See [Studio token](#studio-token). |
| `STUDIO_TOKEN_FILE` | Path to a file holding the studio token, checked if `STUDIO_TOKEN` is unset. |
| `PET_TALK_ALLOWED_HOSTS` | Comma-separated extra hostnames `POST /settings`'s `*_base_url` egress allowlist accepts, beyond loopback/RFC1918/the built-in provider hosts. |

## Repository layout

```
pet-talk/
+-- AGENTS.md, DESIGN.md, README.md, TECH-SPEC.md, Makefile
+-- bin/                    # built binaries, not tracked (bin/.gitkeep only)
+-- cli/
|   +-- audio.py            # mic capture, energy VAD
|   +-- client.py            # terminal CLI client
|   `-- hotkey/              # native Swift macOS hotkey/HUD daemon
|       +-- main.swift
|       +-- hud_window.swift
|       +-- earcons.swift
|       +-- paste_injector.swift
|       `-- config.swift
+-- docs/
|   +-- SPEC.md               # the one contract — read this
|   +-- ONBOARDING.md
|   +-- ROADMAP.md
|   +-- WAVE3.md
|   +-- TECH-DESIGN.md        # pointer stub, superseded by docs/SPEC.md
|   +-- DYNAMIC-NOTCH-ISLAND-SPEC.md
|   +-- SENSORY-FEEDBACK-SPEC.md
|   +-- DICTATION-SPEC.md
|   `-- DONNA-HERDR-ARCHIE-SPEC.md
+-- humanizer/                # deterministic filler/pace/backchannel engine
+-- personas/                 # donna.md, jarvis.md, zuck.md, voices.yaml
+-- qa/                       # QA suites + qa/run_all.sh + qa/budgets.json
+-- server/
|   +-- app.py                 # thin composer, stable import surface
|   +-- runtime.py, settings.py, provider_factory.py
|   +-- frames.py, speak_queue.py, speech.py, turn.py, stall.py, control.py
|   +-- grounding.py, ws.py, routes_http.py, eyes.py
|   +-- persona.py, persona_runtime.py, memory.py, voices.py
|   +-- dictation.py, audio_store.py, logs.py, telemetry.py
|   `-- providers/             # _shared.py, llm.py, stt.py, tts.py, vad.py
`-- web/src/                   # React + Vite cockpit
```

## Verification

```bash
make qa           # full gate: bash qa/run_all.sh
make qa-silent    # PET_TALK_SILENT=1 — no audio, no daemons started, safe for CI/overnight
make qa-real      # PET_TALK_REAL_ENGINE=1 — also runs the real-engine suites
```

`qa/run_all.sh --list` prints every suite pet-talk actually runs, in order,
with no side effects.

- **`make qa`** runs everything that can run without a live server or daemon.
  Suites gated on a port (`say.sh` smoke, `qa/live_ws_turn.py`) SKIP honestly
  if nothing is listening — that is not a failure.
- **`make qa-silent`** adds the guarantee that nothing produces sound and
  nothing starts a daemon that could later speak. It does not gate on a
  running Kokoro daemon on `:8088` — night mode gates on the flag, not the
  port, because a daemon may already be running for an unrelated reason (see
  `AGENTS.md` law 7).
- **`make qa-real`** additionally exercises real STT/LLM/TTS backends
  (`qa/test_real_engine_e2e.py`, `qa/test_voice_analyzer.py`) instead of
  skipping them. It needs live credentials and a reachable daemon; the
  `qa/budgets.json` numbers it checks against are for the stub-provider path
  unless you're also running with real engines end to end — see
  `docs/SPEC.md` §9 for exactly what "MEASURED" means today.

The only latency numbers this README will quote are the ones in
`qa/budgets.json` or the measured table in `docs/SPEC.md` §9 — both labelled.
No other number in this file is a claim about real-world speed.

## Living docs

Architecture and roadmap: `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`. Rendered page: https://claude.ai/code/artifact/57578976-d952-47ac-b219-2c440af1cd22
Rebuild: `python3 docs/site/build.py --arch docs/ARCHITECTURE.md --roadmap docs/ROADMAP.md --out docs/site/index.html --product-name pet-talk --repo-url https://github.com/unfoundbox-crew/pet-talk`
