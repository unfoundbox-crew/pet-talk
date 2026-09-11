# AGENTS.md — pet-talk

## What this is
Full-duplex voice loop (v0.2): WS protocol, provider tyres, personas,
humanizer, QA gates. Local-first; cloud tyres explicit, never silent.

## Where things live
- Contract: `docs/SPEC.md` (read first), `TECH-SPEC.md` (wire detail).
- Server: `server/app.py` (WS), `server/providers.py` (tyres), telemetry JSONL.
- Frontend: `web/` (Vite, `VITE_WS_URL`, default `:8089`).
- Personas: `personas/*.md` (12-field spec) + `voices.yaml`.
- QA: `qa/run_all.sh` — green with honest SKIP/NOT-MEASURED only.

## Laws
1. Fail closed with named reasons. No silent fallbacks, no fake greens.
2. Provider swaps are config-only (`TTS_PROVIDER`/`STT_PROVIDER` env).
3. No absolute paths, no secrets in tree (Doppler + `STUDIO_TOKEN_FILE`).
4. Heavy compute leaves the MacBook; node/python checks stay local.
5. Subagents report back in ONE message; never spawn sideways.
6. Taste (personas' character) is human-approved; tone changes are PRs.

## Run it
Daemon `:8088` → duplex `:8089` → web `:5173`. `qa/run_all.sh` before
every merge. Latency gates: stall ≤400ms, barge ≤100ms, turn ≤1200ms worst.
