# pet-talk 0.3.0 — release notes

2026-09-12

## What it is

pet-talk is a local-first, full-duplex voice loop. You talk, it answers, and
you can interrupt it mid-sentence. STT, LLM, and TTS are each a config swap
— never a code change — and the defaults are chosen by measurement, not by
which API key happens to be in your environment.

## What changed since 0.2

User-visible first:

- You can now interrupt (barge) reliably. The old event loop blocked on
  STT/TTS calls, so a barge frame sent while the agent was mid-turn could be
  dropped. Fixed: provider calls run off-thread, and the reader loop stays
  live at all times.
- Speech starts sooner. TTS used to synthesize a whole sentence before
  playing any of it. It now synthesizes in clauses and plays the first one
  as soon as it's ready (`agent.chunk` on the wire). Only the `kokoro-local`
  tyre streams; cloud tyres still return one file and say so.
- New hand-over chord: Option+Shift+Tab marks the next turn as delegated
  work. Pause is now a double-tap of Option+Tab within 400ms (was a single
  tap).
- New: attach a screenshot, image, or PDF (paste, drag-drop, or the file
  picker) and pet-talk OCRs it locally and folds the text into the next
  turn. No pixels leave the machine.
- New: spoken claims about work done ("tests are green", "that shipped")
  now carry a receipt naming the session or commit that proves it, or they
  get refused instead of spoken bare.
- Every mutating HTTP route and the WebSocket handshake now require a
  studio token. If you're running your own client against this server,
  read "How to run" below before you upgrade.
- `POST /settings` no longer echoes your API keys back in plaintext, and no
  longer accepts an arbitrary base URL for the LLM (that combination let a
  local page point the LLM at its own host and exfiltrate your key).

Everything else:

- `app.py` (948 lines) is now 16 modules, each under ~400 lines.
- `docs/SPEC.md` is the one source of truth for the wire protocol, provider
  contract, and latency budgets. `TECH-SPEC.md` is a pointer to it now.
- STT default is a tuned `faster-whisper tiny.en` (143ms p50, was 193ms).
- TTS default is in-process Kokoro via `mlx-audio` (237ms p50 for a whole
  sentence, was 1286ms through the old daemon).
- LLM default is the LiteLLM proxy, unconditionally — not "groq if you
  happen to have a key."
- The kill switch no longer SIGKILLs every `afplay` process on your machine
  by name match; it tracks its own children by PID and verifies ownership
  before touching anything else.
- No hardcoded LiteLLM key, tailnet IP, or `/Users/...` path anywhere in the
  tree.

## Measured latency

Live WS, real speech, real providers, 2026-09-12. One caveat: three other
build lanes were running on this machine during the run — load average hit
186 — so the two FAIL rows are a measured ceiling under load, not a proven
regression; the turn's own telemetry traces both to the STT stage.

| Metric | p50 | p95 | Budget | Result |
|---|---|---|---|---|
| first playable audio of a sentence (chunked, kokoro-local) | 127.3ms | 246.1ms | 200ms | PASS at p50 |
| first audio, live turn | 696.0ms | 775.8ms | 800ms | PASS |
| cold first turn | — | 433.9ms | 1200ms | PASS |
| barge ack | 1.1ms | 1.2ms | 100ms | PASS |
| STT (faster-whisper tiny.en) | 164.1ms | 285.6ms | 150ms | FAIL (machine load) |
| stall-to-speech gap | 695.6ms | 775.6ms | 400ms | FAIL (machine load) |

LLM first-content-delta was not re-measured this pass; last measured
2026-09-12 (an earlier run that day) at 512.4ms p50 / 805.4ms p95 against an
800ms budget (PASS at p50).

## How to run

```bash
pip install -r server/requirements.txt

# Default TTS runs in-process and needs mlx-audio in the SAME interpreter
# that runs the server (use ~/miniconda3/envs/local-ml-py311, not a fresh venv):
pip install mlx-audio 'misaki[en]'

LITELLM_BASE_URL=http://127.0.0.1:8000/v1 \
  doppler run --project unfoundbox --config dev_personal -- \
  ~/miniconda3/envs/local-ml-py311/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8089

cd web && npm install && npm run dev   # optional cockpit, :5173
```

Provider defaults and how to swap them:

- STT: `faster-whisper tiny.en`, no key needed. Swap with `STT_PROVIDER`.
- LLM: `litellm` at `gpt-oss-120b-groq` through `$LITELLM_BASE_URL` (no
  host baked in — you must point this at a running proxy). Swap with
  `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL`.
- TTS: `kokoro-local`, needs `mlx-audio` + `misaki[en]` and Apple Silicon.
  No Apple Silicon, or don't want the dependency? `TTS_PROVIDER=kokoro`
  runs the same weights behind the SpacePilot daemon instead (about 5x
  slower, measured) — start it with `./serve.sh`.

Studio token: every mutating route and the WS handshake need one. The
server resolves it from env `STUDIO_TOKEN`, else `STUDIO_TOKEN_FILE`, else
generates one at startup into `.qa-scratch/studio.token` (path logged once,
value never). Send it as header `X-Studio-Token`, or `?token=` on the WS
URL (browsers can't set headers on `new WebSocket()`). The CLI client and
web cockpit already read the same three sources, so a local client picks
it up with no extra config.

```bash
make qa           # full gate
make qa-silent    # no audio, no daemons — safe for CI or overnight runs
make qa-real      # also exercises real STT/LLM/TTS backends
make build-hotkey # builds the Swift hotkey daemon (runs on ssh air, not locally)
```

## Known gaps

- The Swift hotkey binary was built and typechecked locally on 2026-09-12
  morning, but the daemon, HUD, earcons, and kill switch have not been
  exercised live end-to-end yet.
- Only `kokoro-local` streams chunked audio. Deepgram, Smallest, and
  ElevenLabs still return one finished file per sentence and report
  `chunked=false`.
- Kokoro word timings are estimated, not measured from the model.
- PDF OCR is untested beyond a one-page fixture; multi-page truncation
  (`pdf_max_pages=5`) is unexercised.
- Describe-mode OCR is disabled — the on-device model took ~102s per
  screenshot, so `EYES_DESCRIBE_ENGINE` fails closed by name instead of
  running it.
- STT and the stall-to-speech gap both read FAIL under the machine-load
  conditions of this measurement pass (see table above); a clean re-measure
  on a quiet machine is owed.
- A LiteLLM key leaked into git history earlier is left as-is, per a
  standing decision — it is not in this release's tree.

## Upgrade notes

- If you have a client talking to this server directly, it now needs a
  studio token on every mutating call and on the WS handshake — see "How to
  run" above.
- `LLM_PROVIDER` defaults to `litellm` now, not `groq`. If you were relying
  on the `groq`-when-`GROQ_API_KEY`-is-set fallback, set `LLM_PROVIDER=groq`
  explicitly.
- If you're on Apple Silicon and want the faster default TTS path, install
  `mlx-audio` and `misaki[en]` into the same interpreter running the
  server. Otherwise keep `TTS_PROVIDER=kokoro` and run `./serve.sh`.
- The hand-over/pause hotkeys moved — see "What changed" above.
