# AGENTS.md — pet-talk

## What this is
Full-duplex voice loop (v0.2): WS protocol, provider tyres, personas,
humanizer, QA gates. Local-first; cloud tyres explicit, never silent.

## Where things live
- Contract: `docs/SPEC.md` (read first, and only — `TECH-SPEC.md` and
  `docs/TECH-DESIGN.md` are pointer stubs now, their content lives there).
- Server: `server/app.py` is a thin composer. The actual logic is split:
  `runtime.py` (live provider set + swap lock), `settings.py` (config),
  `provider_factory.py` (tyre construction), `frames.py` (wire frames),
  `speak_queue.py` + `speech.py` (producer/consumer TTS pipeline),
  `turn.py` (one turn end to end), `stall.py`, `control.py`, `grounding.py`,
  `ws.py` (the `/ws` endpoint), `routes_http.py` (HTTP routes), `eyes.py`
  (zero-vision attach lane). Providers are a package: `server/providers/`
  (`_shared.py`, `llm.py`, `stt.py`, `tts.py`, `vad.py`).
- Frontend: `web/` (Vite, `VITE_WS_URL`, default `:8089`).
- Personas: `personas/*.md` (frontmatter spec) + `voices.yaml`.
- QA: `qa/run_all.sh` — green with honest SKIP/NOT-MEASURED only. Latency
  source of truth is `qa/budgets.json`, never a hardcoded copy.

## Laws
1. Fail closed with named reasons. No silent fallbacks, no fake greens.
2. Provider swaps are config-only. For STT, LLM, TTS: `STT_PROVIDER`/
   `LLM_PROVIDER`/`TTS_PROVIDER` env at boot, or `POST /settings` at
   runtime — every named provider constructs from that alone, no code
   change; an unknown name fails closed as `<layer>_unknown_provider`, a
   missing credential as `missing_api_key:<VAR>`. The eyes OCR engine
   swaps the same way but through its own `EYES_ENGINE` (`server/eyes.py`),
   not `RuntimeSettings`/`/settings`. VAD is NOT yet covered by this law —
   it has one provider and no env switch today. Full per-provider table,
   and exactly what's proven vs. not: `docs/CAPABILITY-MATRIX.md`.
3. No absolute paths, no secrets in tree (Doppler + env only).
4. Heavy compute leaves the MacBook; node/python checks stay local. Exception: `make build-hotkey` (five Swift files, ~8 s) builds locally at nice 19; `air` is Intel and cannot produce the arm64 binary.
5. Subagents report back in ONE message; never spawn sideways.
6. Taste (personas' character) is human-approved; tone changes are PRs.
7. **Night mode: `PET_TALK_SILENT=1`** — nothing plays audio, nothing starts
   an audio daemon. A Kokoro daemon may already be running on `:8088` for an
   unrelated reason, and any test that can reach it can make it speak — gate
   on the flag, not on the port. Every suite in `qa/run_all.sh` tagged
   `SILENT` must SKIP under this flag rather than probe the port and proceed.

## Run it
`make build-hotkey` (builds `bin/pet-talk-hotkey` via `cli/hotkey/build.sh`,
another lane's script, on `ssh air` — never locally). Daemon `:8088` →
duplex `:8089` → web `:5173`. `make qa` (or `make qa-silent` overnight)
before every merge. Latency gates: stall ≤400ms, barge ≤100ms, turn ≤1200ms
worst — see `docs/SPEC.md` §9 for what's actually measured today vs.
NOT MEASURED.

**The duplex server (`:8089`) as a launchd user agent is the recommended
way to run it** — `make install-agent` (see README "Run it"); the raw
`uvicorn` command stays for development. The hotkey daemon probes `/health`
on wake (300ms timeout) and shows the existing error path with "server is
not running" rather than spawning `pet-talk-cli` into a dead server.
