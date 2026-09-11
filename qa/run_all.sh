#!/usr/bin/env bash
# qa/run_all.sh — pet-talk v0.2 QA gate runner (TECH-SPEC sections 6-7).
# Runs protocol tests + persona/i18n tests + latency budget + say.sh smoke.
# Appended (QA2): humanizer backchannel contract (sec 8.3) + sec-8.4 budget re-assert.
# Exit 0 iff nothing FAILed (SKIPs are honest, not failures). Daemon-dependent
# steps skip gracefully with a SKIP message when 127.0.0.1:8088 refuses.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8088}"
FAIL=0

say() { printf '%s\n' "== $1"; }

say "1/4 protocol frames (stdlib unittest)"
if python3 "$ROOT/qa/test_protocol.py" -v; then :; else FAIL=1; fi

say "2/4 personas + voices + i18n (stdlib unittest)"
if python3 "$ROOT/qa/test_persona.py" -v; then :; else FAIL=1; fi

say "2b/4 persona studio API & persistence (stdlib unittest)"
if python3 "$ROOT/qa/test_persona_api.py" -v; then :; else FAIL=1; fi

say "2b2/4 runtime tire switching & settings API (stdlib unittest)"
if python3 "$ROOT/qa/test_settings_api.py" -v; then :; else FAIL=1; fi

say "2c/4 hippocampus memory ledger (stdlib unittest)"
if python3 "$ROOT/qa/test_memory.py" -v; then :; else FAIL=1; fi

say "3/4 latency budget (honest stub until backend exists)"
if python3 "$ROOT/qa/latency.py"; then :; else FAIL=1; fi

say "4/4 say.sh smoke (one sentence; daemon optional)"
if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1.0); s.connect(('127.0.0.1', $PORT))" 2>/dev/null; then
  if bash "$ROOT/say.sh" "QA smoke: pet-talk speaks."; then
    echo "PASS  say.sh smoke — one sentence synthesized and played"
  else
    echo "FAIL  say.sh smoke — daemon answered but synth/play failed"
    FAIL=1
  fi
else
  echo "SKIP  say.sh smoke — 127.0.0.1:$PORT refused (daemon not running; start ./serve.sh to prove it)"
fi

say "5/6 humanizer backchannel contract (stdlib unittest; SKIPs conformance until humanizer/ lands)"
if python3 "$ROOT/qa/test_humanizer_gates.py" -v; then :; else FAIL=1; fi

say "6/6 latency sec-8.4 budgets (asserted inside latency.py; drift fails loudly)"
# Checked as part of 3/6 above — this section re-asserts the 8.4 constants
# standalone so a sec-6 pass can never mask a sec-8.4 drift.
if python3 -c "import sys; sys.path.insert(0, '$ROOT/qa'); import latency; sys.exit(0 if latency.check_budgets() else 1)"; then :; else FAIL=1; fi

say "7/7 live WS turn (needs server on :8089 + websockets; SKIP when absent)"
APP_PORT="${APP_PORT:-8089}"
if python3 -c "import socket; s=socket.socket(); s.settimeout(1.0); s.connect(('127.0.0.1', int('$APP_PORT')))" 2>/dev/null; then
  if python3 "$ROOT/qa/live_ws_turn.py" -v; then :; else FAIL=1; fi
else
  echo "SKIP  live WS turn — 127.0.0.1:$APP_PORT refused (boot \`python3 -m uvicorn server.app:app --port $APP_PORT\` from pet-talk/ to prove it)"
fi

say "8/8 terminal CLI client (audio recording, playback, barge-in <=50ms, WS client)"
if python3 "$ROOT/qa/test_cli_client.py" -v; then :; else FAIL=1; fi

say "summary"
if [ "$FAIL" -eq 0 ]; then echo "RESULT: OK (passes + honest SKIP/NOT-MEASURED only)"; else echo "RESULT: FAIL"; fi
exit "$FAIL"
