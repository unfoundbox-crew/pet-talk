# Live WS loop report — pet-talk /ws (lane WSTIME)

Date (UTC): 2026-09-10. Server: `server/app.py` unmodified, booted as
`python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8099`
(`GET /health` -> `{"ok":true,"service":"pet-talk-duplex","version":"0.2"}`).
Client: `websockets` 16.1.1 pip package (was ALREADY installed —
`python3 -c "import websockets"` succeeded, so NOTHING was pip-installed).
Test: `qa/live_ws_turn.py` (2 unittest cases, both OK in ~0.4s).

Note on numbering: the brief says "§7 gates". TECH-SPEC.md has no §7 —
the acceptance gates live in §9 (items 1-3) with latency constants in
§6/§8.4. This report maps "gates 1-3" to §9 items 1-3 throughout.

## Measured full turn (user.start/user.stop, 640B PCM, stub STT text -> worker path)

3 timed turns, ms since `user.stop` send:

| turn | agent.stall | state.thinking | state.speaking | agent.done | sentences | path |
|------|-------------|----------------|----------------|------------|-----------|------|
| 1 | 0.4 | 0.5 | 10.9 | 183.1 | 4 (seq0 stall + 3 worker) | worker |
| 2 | 0.3 | 0.4 | 8.2 | 184.9 | 4 | worker |
| 3 | 0.4 | 0.4 | 8.5 | 183.6 | 4 | worker |

Frame order every turn: `agent.stall` -> `state.thinking` ->
`agent.sentence(seq0)` -> `state.speaking` -> `sentence(seq1..3)` ->
`agent.done(path=worker)`. Every frame carries the turn's `turn_id`.
One `audio_url` per turn verified playable:
`GET /audio/<turn>-s1` -> 200 `audio/wav`, 22094 bytes (stub sine).

## Mid-turn barge (user.stop + barge sent back-to-back, no read between)

Ack: `state.listening {barged_turn: 't-live-qa-2', dropped: 0,
new turn_id: 't9-a33576'}`. Shape asserted in-test
(`barged_turn` echoes, `dropped` is int, fresh `turn_id` present).

## Gates (§9 items 1-3) vs stub-adjusted thresholds

| gate | spec threshold | stub-adjusted call | result |
|------|---------------|--------------------|--------|
| 1. stall plays <=400ms after user stops | <=400ms (§8.4) | PASS — measured 0.3-0.4ms (stubs, localhost). Real-backend target stays 400ms. |
| 2. barge kills audio <=100ms, resumes/redirects | <=100ms kill | PARTIAL — ack shape proven (`state.listening` + `barged_turn` + `dropped` int). Kill-time NOT proven: see server bug below. |
| 3. worker streams >=3 sentences gapless | >=3 sentences | PASS (shape) — 3 worker sentences (seq1-3) behind stall seq0, all with playable `audio_url`. "Without gap" is wall-clock vacuous with 50ms stub delays; needs real LLM/TTS to time. |

`live_ws_turn.py` asserts gate 1 leniently (<=4000ms, reports actual) and
gates 2-3 as shape (>=3 sentences, ack fields), so it stays non-flaky.

## Server bugs found (NOT fixed — coordinator owns server/; writes were qa/-only)

1. **Barge cannot interrupt a live turn.** `ws_endpoint` does
   `await handle_turn(...)` inline (`server/app.py`), so the receive loop
   is blocked for the whole turn. A `barge` sent mid-turn is processed only
   AFTER `agent.done`: observed full turn (184ms) THEN the barge ack.
   Consequence: gate-2 kill <=100ms is unprovable against this shape with
   any backend slower than ~100ms/turn — barge needs its own task +
   cancellation (or queue-drain check inside the stream loop). Repro: send
   `user.stop` + `barge` back-to-back; ack arrives after `agent.done`.
2. **`dropped` is always 0 with stubs.** `handle_turn` pushes then
   immediately pops each sentence, so the queue never holds anything for
   `flush()` to drop. Not a protocol bug, but gate-2 "resumes correctly"
   needs a slow-consumer test to ever show `dropped > 0`.

## Files

- `qa/live_ws_turn.py` — the live test (needs server on :8099 + `websockets`;
  SKIP-exit-0 when either is absent).
- `qa/live_ws_report.md` — this file.
- `qa/run_all.sh` section 7/7 — runs the above, SKIP when :8099 refuses.

## Appendix B — barge-kill proof (coordinator, 2026-09-10)
Two server fixes landed after the lane report: (1) endpoint awaits turn
inline (barge starved in socket buffer) → fire-and-forget task; (2) turn
needs a killable await → PET_TALK_TURN_DELAY_MS test hook (0 in prod).
Proof run (delay=300ms, barge after first agent frame):
BARGE-ACK latency 7.6ms, agent.done path=interrupted received.
Gate 2 CLOSED.
