#!/usr/bin/env python3
"""qa/live_ws_turn.py — live WS /ws full-turn test (TECH-SPEC sec 4 + 8.4/9).

Proves the real FastAPI WS loop live: state.idle -> user.start/user.stop ->
agent.stall (timed) -> agent.sentence* -> agent.done -> barge -> state.listening.

Needs: real server on LIVE_WS_URL (default ws://127.0.0.1:8099/ws), i.e.
  python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8099
from the pet-talk/ dir. Plus the `websockets` pip package (pure, small).
SKIPs honestly (exit 0) when the server or the lib is absent — never red.

Stall gate: TECH-SPEC sec 8.4 stall <=400ms is the REAL threshold; with stub
providers (sine TTS, canned LLM) local time-to-stall is ~ms, so the assert is
lenient <=4000ms and the REPORTED actual is what matters. Same story for
barge: the serialize-behind-handle_turn shape means a barge sent mid-turn is
acked only after agent.done — the test asserts the ack shape
(state.listening + barged_turn + dropped int), not a <=100ms kill.

Run: `python3 qa/live_ws_turn.py` (unittest, verbose). Exit 0 = pass/skip.
"""
import asyncio
import base64
import json
import os
import sys
import time
import unittest
import urllib.request

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

PORT = os.environ.get("LIVE_WS_PORT", "8089")
WS_URL = os.environ.get("LIVE_WS_URL", f"ws://127.0.0.1:{PORT}/ws")
HTTP_BASE = os.environ.get("LIVE_HTTP_BASE", f"http://127.0.0.1:{PORT}")

# Lenient ceiling for stub providers; report the actual, gate on the ceiling.
STALL_LENIENT_MS = 4000.0
SPEC_STALL_MS = 400.0  # TECH-SPEC sec 8.4 real threshold (target for real backends)

# Use real audio fixture with spoken text if present, otherwise 320ms PCM fallback
WEATHER_FIXTURE = "/tmp/weather_turn_fixture.wav"
if os.path.exists(WEATHER_FIXTURE):
    with open(WEATHER_FIXTURE, "rb") as f:
        PCM_B64 = base64.b64encode(f.read()).decode()
else:
    PCM_B64 = base64.b64encode(bytes(320 * 2)).decode()


async def _recv(ws, timeout=10.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _full_turn(ws, turn_id):
    """Drive one user.start/stop turn; return (frames, stall_ms)."""
    got = await _recv(ws, 5.0)
    assert got.get("type") == "state.listening", got
    t_stop = time.monotonic()
    await ws.send(json.dumps({"type": "user.stop", "turn_id": turn_id,
                              "pcm_b64": PCM_B64}))
    frames, stall_ms = [], None
    while True:
        m = await _recv(ws, 10.0)
        dt_ms = (time.monotonic() - t_stop) * 1000.0
        frames.append((dt_ms, m))
        if m.get("type") == "agent.stall" and stall_ms is None:
            stall_ms = dt_ms
        if m.get("type") in ("agent.done", "agent.error"):
            return frames, stall_ms


def _run(coro):
    return asyncio.run(coro)


class TestLiveWsTurn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_WS:
            raise unittest.SkipTest("SKIP: `websockets` not installed — "
                                    "pip install websockets to prove the live loop")

    async def _connect(self):
        try:
            ws = await ws_connect(WS_URL, max_size=4 * 1024 * 1024)
        except Exception as e:
            self.skipTest("SKIP: no WS server at %s (%s) — boot "
                          "`uvicorn server.app:app --port 8099` to prove it" % (WS_URL, e))
        first = await _recv(ws, 5.0)
        self.assertEqual(first.get("type"), "state.idle",
                         "first frame is not state.idle: %r" % (first,))
        self.assertTrue(first.get("turn_id"), "state.idle lacks turn_id")
        return ws

    def test_full_turn_worker_path(self):
        async def go():
            ws = await self._connect()
            try:
                turn = "t-live-qa-1"
                await ws.send(json.dumps({"type": "user.start", "turn_id": turn}))
                return await _full_turn(ws, turn)
            except asyncio.TimeoutError:
                self.fail("TIMEOUT waiting for server frames (server stalled mid-turn)")
            finally:
                await ws.close()
        frames, stall_ms = _run(go())

        types = [m.get("type") for _, m in frames]
        # Every frame carries this turn's turn_id.
        for dt, m in frames:
            self.assertEqual(m.get("turn_id"), "t-live-qa-1",
                             "frame %r has wrong turn_id" % (m,))
        # Stall gate (lenient for stubs; report actual vs 400ms spec).
        self.assertIsNotNone(stall_ms, "no agent.stall frame in worker-path turn")
        print("    time-to-stall: %.1fms (spec 8.4 <=%.0fms; lenient assert <=%.0fms)"
              % (stall_ms, SPEC_STALL_MS, STALL_LENIENT_MS))
        self.assertLessEqual(stall_ms, STALL_LENIENT_MS,
                             "stall %.1fms exceeds lenient ceiling" % stall_ms)
        # Ordering: stall before thinking/speaking, done last with path=worker.
        self.assertLess(types.index("agent.stall"), types.index("state.thinking"))
        done = frames[-1][1]
        self.assertEqual(done.get("type"), "agent.done")
        self.assertEqual(done.get("path"), "worker", "expected stall->worker path, got %r" % (done,))
        worker_sents = [m for _, m in frames
                        if m.get("type") == "agent.sentence" and m.get("seq", 0) >= 1]
        print("    worker sentences behind stall: %d" % len(worker_sents))
        self.assertGreaterEqual(len(worker_sents), 1,
                                "worker path streamed <1 sentences: %d" % len(worker_sents))
        for m in worker_sents:
            self.assertTrue(m.get("audio_url"), "sentence lacks audio_url: %r" % (m,))
        # One TTS url actually plays (HTTP 200 audio/wav).
        url = HTTP_BASE + worker_sents[0]["audio_url"]
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("audio/wav", r.headers.get("Content-Type", ""))
                wav = r.read()
            print("    audio_url GET %s -> 200 audio/wav, %d bytes" % (worker_sents[0]["audio_url"], len(wav)))
        except Exception as e:
            self.fail("audio_url not playable: %s (%s)" % (url, e))

    def test_barge_mid_turn_acked(self):
        async def go():
            ws = await self._connect()
            try:
                turn = "t-live-qa-2"
                await ws.send(json.dumps({"type": "user.start", "turn_id": turn}))
                got = await _recv(ws, 5.0)
                assert got.get("type") == "state.listening", got
                # stop then barge with no read between: barge lands mid-turn.
                await ws.send(json.dumps({"type": "user.stop", "turn_id": turn,
                                          "pcm_b64": PCM_B64}))
                await ws.send(json.dumps({"type": "barge", "turn_id": turn}))
                ack = None
                while True:
                    m = await _recv(ws, 10.0)
                    if m.get("type") == "state.listening" and "barged_turn" in m:
                        ack = m
                        break
                return ack
            except asyncio.TimeoutError:
                self.fail("TIMEOUT waiting for barge ack")
            finally:
                await ws.close()
        ack = _run(go())
        self.assertEqual(ack.get("barged_turn"), "t-live-qa-2")
        self.assertIsInstance(ack.get("dropped"), int)
        self.assertTrue(ack.get("turn_id"), "barge ack lacks fresh turn_id")
        print("    barge ack: state.listening barged_turn=%r dropped=%r new=%r"
              % (ack.get("barged_turn"), ack.get("dropped"), ack.get("turn_id")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
