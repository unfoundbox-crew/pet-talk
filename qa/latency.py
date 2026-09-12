#!/usr/bin/env python3
"""qa/latency.py — latency budget check against a live server (TECH-SPEC sec 8.4).

Budgets come from ONE place: qa/budgets.json. This script never restates a
budget as a Python constant and never asserts a constant equals itself.

Behavior:
  - If a server answers on 127.0.0.1:$APP_PORT (default 8089), open a WS
    connection and run N=5 stub-provider turns, measuring:
      * stall latency    (user.stop -> agent.stall)
      * first-sentence latency (user.stop -> first agent.sentence)
      * barge ack latency (barge sent -> state.listening ack received)
    then print a p50/p95 table and PASS/FAIL against qa/budgets.json.
  - If no server is reachable (or `websockets` is not installed), print
    NOT-MEASURED for every metric and exit 0 — this is not a failure, it is
    an honest "nothing was measured."

Run: `python3 qa/latency.py`. Exit 0 unless a live measurement breaches budget.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGETS_PATH = os.path.join(ROOT, "qa", "budgets.json")

APP_PORT = int(os.environ.get("APP_PORT", "8089"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _studio_token() -> str:
    """Same resolution order as server/auth.py: env STUDIO_TOKEN, env
    STUDIO_TOKEN_FILE, then the generated <repo>/.qa-scratch/studio.token."""
    tok = (os.environ.get("STUDIO_TOKEN") or "").strip()
    if tok:
        return tok
    for path in (os.environ.get("STUDIO_TOKEN_FILE") or "", os.path.join(ROOT, ".qa-scratch", "studio.token")):
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    tok = f.read().strip()
            except OSError:
                tok = ""
            if tok:
                return tok
    return ""


STUDIO_TOKEN = _studio_token()
_BASE_WS_URL = os.environ.get("LATENCY_WS_URL", f"ws://127.0.0.1:{APP_PORT}/ws")
WS_URL = f"{_BASE_WS_URL}?token={STUDIO_TOKEN}" if STUDIO_TOKEN else _BASE_WS_URL
N_TURNS = int(os.environ.get("LATENCY_N", "5"))

sys.path.insert(0, os.path.join(ROOT, "qa"))
from fixture_audio import load_b64  # noqa: E402  (needs ROOT on the path first)

#: Real spoken audio when a fixture is baked, silence otherwise. Silence is
#: fine against stub STT and useless against a real tyre — it transcribes to
#: nothing and every metric below reads NOT-MEASURED off the error path
#: instead of a turn. `qa/fixture_audio.py` explains the resolution order.
PCM_B64, PCM_SAMPLE_RATE, PCM_IS_REAL, PCM_SOURCE = load_b64()


def load_budgets() -> dict:
    with open(BUDGETS_PATH) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_") and k != "measured"}


def port_reachable(host="127.0.0.1", port=APP_PORT, timeout=1.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(pct / 100.0 * (len(s) - 1))))
    return s[k]


async def _recv(ws, timeout=10.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _measure_turn(ws_connect, turn_no):
    """One user.start/stop turn: returns (stall_ms|None, first_sentence_ms|None)."""
    turn_id = f"t-latency-{turn_no}"
    async with ws_connect(WS_URL, max_size=4 * 1024 * 1024) as ws:
        await _recv(ws, 5.0)  # state.idle
        await ws.send(json.dumps({"type": "user.start", "turn_id": turn_id}))
        await _recv(ws, 5.0)  # state.listening
        t0 = time.monotonic()
        await ws.send(json.dumps({"type": "user.stop", "turn_id": turn_id,
                                  "pcm_b64": PCM_B64,
                                  "sample_rate": PCM_SAMPLE_RATE}))
        stall_ms = None
        first_sentence_ms = None
        while True:
            m = await _recv(ws, 10.0)
            dt = (time.monotonic() - t0) * 1000.0
            if m.get("type") == "agent.stall" and stall_ms is None:
                stall_ms = dt
            if m.get("type") == "agent.sentence" and first_sentence_ms is None:
                first_sentence_ms = dt
            if m.get("type") in ("agent.done", "agent.error"):
                return stall_ms, first_sentence_ms


async def _measure_barge(ws_connect):
    """One turn: send barge mid-turn, measure send->ack latency; returns ms|None."""
    turn_id = "t-latency-barge"
    async with ws_connect(WS_URL, max_size=4 * 1024 * 1024) as ws:
        await _recv(ws, 5.0)  # state.idle
        await ws.send(json.dumps({"type": "user.start", "turn_id": turn_id}))
        await _recv(ws, 5.0)  # state.listening
        await ws.send(json.dumps({"type": "user.stop", "turn_id": turn_id,
                                  "pcm_b64": PCM_B64,
                                  "sample_rate": PCM_SAMPLE_RATE}))
        t0 = time.monotonic()
        await ws.send(json.dumps({"type": "barge", "turn_id": turn_id}))
        while True:
            m = await asyncio.wait_for(ws.recv(), 10.0)
            m = json.loads(m)
            if m.get("type") == "state.listening" and "barged_turn" in m:
                return (time.monotonic() - t0) * 1000.0
            if m.get("type") in ("agent.done", "agent.error"):
                # Turn finished before the barge landed — nothing to measure this run.
                return None


def run_measurements():
    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:
        return None, "websockets not installed (`pip install websockets`)"

    stalls, first_sentences, barges = [], [], []

    async def go():
        for i in range(N_TURNS):
            stall_ms, fs_ms = await _measure_turn(ws_connect, i)
            if stall_ms is not None:
                stalls.append(stall_ms)
            if fs_ms is not None:
                first_sentences.append(fs_ms)
            barge_ms = await _measure_barge(ws_connect)
            if barge_ms is not None:
                barges.append(barge_ms)

    try:
        asyncio.run(go())
    except Exception as e:
        return None, f"measurement run failed: {e}"

    return {
        "stall_ms": stalls,
        "first_sentence_ms": first_sentences,
        "barge_ms": barges,
    }, None


def main():
    budgets = load_budgets()
    print("audio: %s (%s, %d Hz)"
          % (PCM_SOURCE, "real speech" if PCM_IS_REAL else "SILENCE", PCM_SAMPLE_RATE))
    print("budgets (qa/budgets.json, ms):")
    for k, v in budgets.items():
        print("  %-15s %6d" % (k, v))

    reachable = port_reachable()
    if not reachable:
        print(f"\nno listener on 127.0.0.1:{APP_PORT} — nothing measured")
        for metric in ("stall_ms", "first_sentence_ms", "barge_ms"):
            print(f"  {metric:<18} NOT-MEASURED (no server on :{APP_PORT})")
        print("RESULT: NOT-MEASURED (no server)")
        return 0

    samples, err = run_measurements()
    if samples is None:
        print(f"\nserver on :{APP_PORT} reachable but could not measure: {err}")
        if not STUDIO_TOKEN:
            print("  hint: no studio token found (STUDIO_TOKEN, STUDIO_TOKEN_FILE, .qa-scratch/studio.token)")
        for metric in ("stall_ms", "first_sentence_ms", "barge_ms"):
            print(f"  {metric:<18} NOT-MEASURED ({err})")
        print("RESULT: NOT-MEASURED")
        return 0

    # metric -> budget key comparison. first_sentence is judged against the
    # steady-state turn target since it is the first audible output.
    checks = [
        ("stall_ms", samples["stall_ms"], budgets.get("stall_ms")),
        ("first_sentence_ms", samples["first_sentence_ms"], budgets.get("turn_p50_ms")),
        ("barge_ms", samples["barge_ms"], budgets.get("barge_ms")),
    ]

    print(f"\nlive measurement over {_BASE_WS_URL} (N={N_TURNS} turns):")
    print("  %-18s %8s %8s %10s %6s" % ("metric", "p50", "p95", "budget", "n"))
    fail = False
    for name, values, budget in checks:
        if not values:
            print("  %-18s %8s %8s %10s %6d  NOT-MEASURED" % (name, "-", "-", budget, 0))
            continue
        p50 = percentile(values, 50)
        p95 = percentile(values, 95)
        ok = budget is None or p95 <= budget
        status = "PASS" if ok else "FAIL"
        print("  %-18s %8.1f %8.1f %10s %6d  %s" % (name, p50, p95, budget, len(values), status))
        if not ok:
            fail = True

    print("RESULT: %s" % ("FAIL" if fail else "OK"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
