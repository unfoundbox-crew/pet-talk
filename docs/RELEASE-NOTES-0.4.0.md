# pet-talk 0.4.0 — release notes

2026-09-12

## What it is

pet-talk is a local-first, full-duplex voice loop. You talk, it answers, and
you can interrupt it mid-sentence. STT, LLM, and TTS are each a config swap
— never a code change — and the defaults are chosen by measurement, not by
which API key happens to be in your environment.

## What changed since 0.3

User-visible first:

- The first turn after you start the server is no longer the slow one. It used
  to stall for 1264ms while the speech model loaded on the first call. The
  server now loads it at startup instead, before you ask anything. First turns
  measured 198-288ms after the change.
- Enter sends in the web composer. It always said "Enter" in the hint and only
  the Send button worked. Shift+Enter now inserts a newline, and Cmd+Enter
  sends as well.
- The web cockpit is a proper inspector now. It buffers the answer ahead of the
  audio so you can skip forward and back with the arrow keys, and it shows a
  per-stage timing bar. Anything that reads like engineering — timings, frame
  names, backend names — is behind a Developer toggle and nowhere else.
- Attach a screenshot and its text now reaches the model as part of your
  question, not just as a note in the transcript.
- The hotkey works again on a fresh checkout. The daemon needed a launcher that
  nothing in the tree built, so Option+Tab did nothing at all. `make
  build-hotkey` builds both pieces now.
- Screen grounding is documented and opt-in: `PET_TALK_AX=1` plus a one-time
  permission prompt lets pet-talk see which app and window you are in.
- Archie appears in the notch capsule and in the cockpit, in his small form.

Everything else:

- Streaming speech recognition is in the tree behind `PET_TALK_STT_STREAM`,
  switched off. It is off because we could not measure whether it helps, not
  because it failed — see "Known gaps".
- The sentence-length rules now reach every persona. A persona that writes its
  own prompt used to receive none of them while the server went on refusing
  its long sentences.
- Text read off a screenshot is fenced as untrusted before it reaches the
  model, so a picture of an instruction is data and not an order.
- Six blockers from an adversarial review of the merged tree are closed, each
  with a test: an attachment leaking into the next turn, a sequence-number
  collision, a barge that left speech recognition running, frames from an
  interrupted turn still being drawn, and backend names showing with the
  Developer toggle off.
- `SERVICE_VERSION` is `0.4.0`; `GET /health` reports it.

Full list with commit shas: `CHANGELOG.md`.

## Measured

Real providers on this machine, under Doppler. Machine load was about 6 during
the 0.4.0 pass — not quiet, and that matters for the rows marked so.

| Metric | Number | Date | Note |
|---|---|---|---|
| first turn after boot, time-to-stall | 1264ms | 2026-09-12 | before the STT warm |
| first turn after boot, time-to-stall | 287.9 / 198.4 / 197.8ms | 2026-09-12 | after the STT warm, three separate boots |
| warm turns, time-to-stall | 317-383ms | 2026-09-12 | steady state, same session |
| barge acknowledgement | 1.3ms | 2026-09-12 | budget 100ms |
| STT inside the warmed first turn | 137.0-163.3ms | 2026-09-12 | server's own stage receipt |
| startup STT warm itself | 3.5-3.7s | 2026-09-12 | entirely off the turn path |
| `stall_ms` p50, five-turn run | 642.2ms | 2026-09-12d | budget 400ms, still FAIL at load ~7 |

One honest detail from the measurement. The first attempt read 909.6ms and
looked like a failure. It was not: the turn had raced the stall-phrase warm-up
and was queued behind three of its speech syntheses on the same in-process
model. Speech recognition in that same turn was already 163.3ms. The test now
waits for both warm-ups to report before it measures, and says why in a
comment.

`stall_ms` against its own 400ms budget is unchanged and still failing under
ordinary background load. The server's stage receipt puts that breach in
speech recognition inside the server — 590ms there against 143-164ms measured
in isolation — so it is contention, not the model. Nothing in this release
addresses it.

## How to run

```bash
pip install -r server/requirements.txt

# Default TTS runs in-process and needs mlx-audio in the SAME interpreter
# that runs the server (use ~/miniconda3/envs/local-ml-py311, not a fresh venv):
pip install mlx-audio 'misaki[en]'

# The proxy URL lives in Doppler now, with every other credential.
doppler run --project unfoundbox --config dev_personal -- \
  ~/miniconda3/envs/local-ml-py311/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8089

cd web && npm install && npm run dev   # optional cockpit, :5173
```

```bash
make build-cli      # bin/pet-talk-cli — the launcher, no compiler
make build-hotkey   # build-cli, then the Swift daemon (8s locally, nice 19)
make qa             # full gate
make qa-silent      # no audio, no daemons — safe for CI or overnight runs
make qa-real        # also exercises real STT/LLM/TTS, including the first-turn warm test
```

New environment variables in this release:

- `PET_TALK_STT_WARM` — `0` skips the startup speech-model load and says so in
  the log. On by default.
- `PET_TALK_WARM_PORT` — the port `qa/test_stt_warm_live.py` boots its own
  server on. Default 8090. It refuses to measure a server it did not start.

## Known gaps

- **Whether streaming speech recognition helps a turn is not measured.** The
  test harness hands the whole recording to the server in one message and
  never sends it in pieces, so every measured turn took the whole-recording
  path regardless of the flag. The transcripts were identical either way. The
  flag stays off until the harness can send chunks.
- The developer frame log keeps the raw audio bytes of each frame, so an export
  from it carries audio.
- `qa/budgets.json` is compiled into the web bundle, which tells any page our
  internal latency budgets.
- Two browser tabs can drive two speech decodes at once on the same in-process
  model; there is no lock across sockets.
- `resume_from` exists in the speak queue and nothing calls it.
- Voice activity detection is the one capability layer you cannot swap by
  environment variable — one implementation, no factory. The capability test
  asserts this gap rather than hiding it.
- There is no vision backend in 0.4.0. The `fleet` alias points at the house
  backends; the sovereign router's `fleet/vision` is a separate piece of work
  and is not in this checkout.
- Kokoro word timings are estimated, never measured per word.
- PDF text extraction has no real multi-page test fixture.
- Describe-mode image reading stays disabled: the only local model that can do
  it took 102 seconds for one screenshot.
- macOS ties Accessibility trust to the binary. Rebuilding the hotkey daemon
  revokes the screen-grounding permission, and `ax` answers
  `ax_permission_denied` until you run `ax --request-permission` again. Both
  binaries on this machine were in that state when this release was cut.

## Upgrade notes

- Nothing breaks. There are no wire-protocol or route changes in this release.
- Run `make build-cli` once after pulling. If you were running the hotkey
  daemon and Option+Tab did nothing, this is why, and that is the fix.
- If you had `LITELLM_BASE_URL=...` in front of your server command, you can
  drop it — it comes from Doppler now. Keep it only if you are deliberately
  pointing at a different proxy.
- Startup does a little more work than it used to: one speech-model load and
  the stall phrases, both off the critical path. The socket still accepts
  connections immediately, but a turn started in the first few seconds may
  contend with them. `PET_TALK_STT_WARM=0` and `PET_TALK_STALL_WARM=0` turn
  them off.
