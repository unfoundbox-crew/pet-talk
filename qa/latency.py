#!/usr/bin/env python3
"""qa/latency.py — latency budget check (TECH-SPEC section 6) + gate status.

HONEST STUB, not a fake green: with no backend in the repo there is no real
loop to time. What this script DOES do (exit 0 on success):
  1. asserts the section-6 budget table sums to the claimed ~=1.1s naive total
  2. asserts the streaming target (~600ms) sits under the CI smoke limit
     (p50 turn <1200ms)
  3. reports every acceptance-gate measurement it could NOT take and exactly
     what backend would make it provable

When a measurable backend exists (WS /ws + STT/LLM/TTS), replace probe_backend
with real turn timing and keep the arithmetic assertions as the budget guard.
Stdlib only. Run: `python3 qa/latency.py`.
"""

import socket
import sys

# TECH-SPEC section 6, verbatim figures (ms).
BUDGET_MS = {
    "vad": 30,
    "stt": 300,
    "llm_first_token": 400,
    "tts_first_audio": 300,
    "playback": 20,
    "loopback": 2,
}
NAIVE_CLAIM_MS = 1100          # spec: "1.1s naive"
STREAMING_TARGET_MS = 600      # spec: "~600ms with partial streaming"
SMOKE_P50_LIMIT_MS = 1200      # spec: "p50 turn <1200ms on M1 Max or fails loudly"

# TECH-SPEC section 7 gate thresholds that need a live loop to measure.
GATES_NEEDING_BACKEND = [
    ("gate 1: stall plays <=400ms after user stops", "timed research-question turn over WS /ws"),
    ("gate 2: barge-in kills audio <=100ms", "barge frame + audio-kill timestamp from server log"),
     ("gate 3: worker streams >=3 sentences gapless", "3+ agent.sentence frames behind playing audio"),
]

# TECH-SPEC section 8.4, verbatim figures (ms) — QA gates from field data.
# NOTE: a DIFFERENT budget from the section-6 naive table above
# (sec 6: VAD30/STT300/LLM400/TTS300 naive sum; sec 8.4: streaming targets).
# Both blocks coexist; neither may drift from its own spec section.
SPEC_84_MS = {  # the spec text itself — edit only when TECH-SPEC changes
    "stt": 150,
    "llm": 800,
    "tts": 200,
    "turn_target": 800,
    "turn_worst": 1200,
    "silero_confirm": 250,
    "silero_silence": 500,
    "stall": 400,
    "barge_kill": 100,
}

BUDGET_84_MS = dict(SPEC_84_MS)  # working constants under test


def check_budgets():
    """Assert the working 8.4 constants match the spec verbatim. Fail loudly."""
    ok = True
    for k, want in SPEC_84_MS.items():
        got = BUDGET_84_MS.get(k)
        match = (got == want)
        check("8.4 %-14s == spec %4dms" % (k, want), match,
              "working=%sms" % (got,))
        ok = ok and match
    # Relational gates the spec states as inequalities.
    check("8.4 turn target <=800ms steady",
          BUDGET_84_MS["turn_target"] <= 800,
          "turn_target=%dms" % BUDGET_84_MS["turn_target"])
    ok = ok and BUDGET_84_MS["turn_target"] <= 800
    check("8.4 turn worst <=1200ms",
          BUDGET_84_MS["turn_worst"] <= 1200,
          "turn_worst=%dms" % BUDGET_84_MS["turn_worst"])
    ok = ok and BUDGET_84_MS["turn_worst"] <= 1200
    return ok

FAILURES = []


def check(name, cond, detail):
    print(("PASS  " if cond else "FAIL  ") + name + " — " + detail)
    if not cond:
        FAILURES.append(name)


def probe_backend(host="127.0.0.1", port=8088, timeout=0.7):
    """True if anything listens on the daemon port (TTS HTTP today, WS later)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    print("latency budget (TECH-SPEC sec 6, all figures ms):")
    for k, v in BUDGET_MS.items():
        print("  %-15s %4d" % (k, v))
    naive = sum(BUDGET_MS.values())
    print("  %-15s %4d" % ("naive total", naive))

    check("budget sums to ~=1.1s",
          abs(naive - 1052) == 0 and abs(naive - NAIVE_CLAIM_MS) <= 100,
          "sum=%dms vs claimed ~%dms" % (naive, NAIVE_CLAIM_MS))
    check("streaming target under smoke limit",
          STREAMING_TARGET_MS < SMOKE_P50_LIMIT_MS,
          "target ~%dms < p50 limit %dms" % (STREAMING_TARGET_MS, SMOKE_P50_LIMIT_MS))

    print("latency constants (TECH-SPEC sec 8.4, all figures ms):")
    for k, v in BUDGET_84_MS.items():
        print("  %-15s %4d" % (k, v))
    check_budgets()

    print("live loop measurement:")
    if probe_backend():
        print("  listener on 127.0.0.1:8088 — but no WS /ws turn timer exists yet, "
              "so no turn was timed")
    else:
        print("  no listener on 127.0.0.1:8088 — no turn timed (expected: no backend yet)")
    for gate, needs in GATES_NEEDING_BACKEND:
        print("  NOT MEASURED: %s [would need: %s]" % (gate, needs))

    if FAILURES:
        print("RESULT: FAIL (%d)" % len(FAILURES))
        return 1
    print("RESULT: budget arithmetic OK; live-loop timing NOT MEASURED (no backend)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
