#!/usr/bin/env bash
# qa/run_all.sh — pet-talk QA gate runner (TECH-SPEC sections 6-9).
#
# Honest, bounded, silent-capable, cannot hang:
#   - Suites are discovered from ONE ordered array below; "i/N" numbering is
#     computed from that array's length, never hand-typed.
#   - Every step runs under a bounded timeout (perl alarm — macOS has no
#     `timeout` binary). Default 120s; override per-suite, see TIMEOUT
#     OVERRIDES below.
#   - Steps that need a daemon probe the port with a 1s connect and SKIP
#     with the reason when it refuses — never hang waiting for one.
#   - Steps that produce sound SKIP under PET_TALK_SILENT=1, printed reason.
#   - qa/test_real_engine_e2e.py and qa/test_voice_analyzer.py run only
#     under PET_TALK_REAL_ENGINE=1, else SKIP (so the pass count is honest).
#   - A final summary table reports PASS/FAIL/SKIP counts and every SKIP
#     reason. Exit non-zero iff any suite FAILed.
#
# Usage:
#   bash qa/run_all.sh              # full gate
#   PET_TALK_SILENT=1 bash qa/run_all.sh    # no sound, no daemons started
#   PET_TALK_REAL_ENGINE=1 bash qa/run_all.sh   # also run the real-engine suites
#   bash qa/run_all.sh --list       # print suites and exit, run nothing
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

APP_PORT="${APP_PORT:-8089}"
TTS_PORT="${PORT:-8088}"
DEFAULT_TIMEOUT="${QA_DEFAULT_TIMEOUT:-120}"
SILENT="${PET_TALK_SILENT:-0}"
REAL_ENGINE="${PET_TALK_REAL_ENGINE:-0}"

# ---------------------------------------------------------------------------
# Suite table: "label|path|kind|flags"
#   kind:  unit  = python3 <path> -v   (stdlib unittest, verbose)
#          main  = python3 <path>      (script has its own main(), no -v)
#          shell = bash <path> <args>
#   flags: comma-separated, any of:
#          SILENT     - SKIP entirely when PET_TALK_SILENT=1 (produces sound
#                       or starts a daemon that could)
#          REAL       - SKIP entirely unless PET_TALK_REAL_ENGINE=1
#          OPTIONAL   - SKIP (not FAIL) if the file doesn't exist yet
#                       (another lane creates it concurrently)
#          PORT:<n>   - SKIP unless 127.0.0.1:<n> accepts a 1s connection
# ---------------------------------------------------------------------------
SUITES=(
  "protocol frames|qa/test_protocol.py|unit|"
  "design tokens (AgentWorth vendor + pet-talk layer)|qa/test_design_tokens.py|unit|"
  "personas + voices + i18n|qa/test_persona.py|unit|"
  "persona studio API & persistence|qa/test_persona_api.py|unit|"
  "runtime tire switching & settings API|qa/test_settings_api.py|unit|"
  "transcribe REST API & WS user.text turn frames|qa/test_transcribe_api.py|unit|"
  "universal dictation matrix & clean prose|qa/test_dictation_matrix.py|unit|"
  "hippocampus memory ledger|qa/test_memory.py|unit|"
  "humanizer backchannel contract|qa/test_humanizer_gates.py|unit|"
  "provider ABCs & factories|qa/test_providers.py|unit|"
  "no-vendor-lock-in capability matrix (STT/LLM/TTS/VAD/eyes)|qa/test_capability_matrix.py|unit|"
  "chunked TTS wire path & warm stall cache|qa/test_tts_chunking.py|unit|"
  "turn lifecycle|qa/test_turn_lifecycle.py|unit|OPTIONAL"
  "studio token + egress allowlist (exfiltration refused)|qa/test_security.py|unit|OPTIONAL"
  "zero-vision eyes lane (attach, OCR, named errors)|qa/test_eyes.py|unit|"
  "hand-over chord frame (user.handover)|qa/test_handover.py|unit|"
  "screen line fenced as untrusted (AX grounding)|qa/test_grounding_fence.py|unit|"
  "terminal CLI client|qa/test_cli_client.py|unit|"
  "native macOS global hotkey listener|qa/test_hotkey.py|unit|SILENT"
  "floating glass capsule HUD|qa/test_hud.py|unit|"
  "acoustic earcons & config engine|qa/test_earcons.py|unit|SILENT"
  "WebSocket resilience & bounded audio LRU|qa/test_socket_resilience.py|unit|"
  "Smallest.ai Lightning TTS & OpenAI reasoning tyres|qa/test_new_tyres.py|unit|"
  "voice forensics & latency analyzer|qa/test_voice_analyzer.py|unit|REAL"
  "real engine e2e (Kokoro/STT/LLM)|qa/test_real_engine_e2e.py|unit|REAL"
  "first-turn STT warm on a live server (:8090, its own)|qa/test_stt_warm_live.py|unit|REAL"
  "archie receipts, freshness law & chip rules|qa/test_receipts.py|unit|"
  "streaming STT (partials, early stall, tail finalize)|qa/test_stt_streaming.py|unit|"
  "archie small-form glyph, states & placement rules|qa/test_archie_glyph.py|unit|"
  "latency budget vs qa/budgets.json|qa/latency.py|main|"
  "say.sh smoke (one sentence)|say.sh|shell|SILENT,PORT:${TTS_PORT}"
  "live WS turn (needs server on :${APP_PORT})|qa/live_ws_turn.py|unit|PORT:${APP_PORT}"
)
N=${#SUITES[@]}

# ---------------------------------------------------------------------------
# --list: print the suite table and exit, run nothing.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--list" ]; then
  i=0
  for entry in "${SUITES[@]}"; do
    i=$((i + 1))
    IFS='|' read -r label path kind flags <<< "$entry"
    printf '%2d/%-2d  %-55s %-6s %s\n' "$i" "$N" "$label" "$kind" "$path"
  done
  exit 0
fi

port_open() { # host port -> 0 if a connection succeeds within 1s
  python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.0)
try:
    s.connect(('$1', $2))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null
}

run_bounded() { # timeout_s cmd... -> runs cmd in its own process group under a hard wall-clock alarm
  # On timeout the WHOLE group is killed (TERM, then KILL after 2s), so a suite
  # that forked a server or an OCR subprocess cannot leave orphans behind.
  # Exit status 124 on timeout, else the command's own status.
  local t="$1"; shift
  perl -e '
    use POSIX qw(setsid);
    my $t = shift;
    my $pid = fork();
    die "fork: $!" unless defined $pid;
    if ($pid == 0) { setsid(); exec @ARGV or die "exec: $!"; }
    local $SIG{ALRM} = sub {
      kill "TERM", -$pid; select(undef, undef, undef, 2);
      kill "KILL", -$pid; waitpid($pid, 0); exit 124;
    };
    alarm $t;
    waitpid($pid, 0);
    my $st = $?;
    exit(($st & 127) ? 128 + ($st & 127) : $st >> 8);
  ' "$t" "$@"
}

timeout_for() { # path -> per-suite override via env, else DEFAULT_TIMEOUT
  local base key override
  base="$(basename "$1")"
  base="${base%.py}"
  base="${base%.sh}"
  key="$(echo "$base" | tr '[:lower:].-' '[:upper:]__')_TIMEOUT"
  override="${!key:-}"
  echo "${override:-$DEFAULT_TIMEOUT}"
}

NAMES=()
STATUSES=()
REASONS=()
FAIL=0
i=0

for entry in "${SUITES[@]}"; do
  i=$((i + 1))
  IFS='|' read -r label path kind flags <<< "$entry"
  printf '== %d/%d %s (%s)\n' "$i" "$N" "$label" "$path"

  skip_reason=""

  if [[ ",$flags," == *",OPTIONAL,"* ]] && [ ! -f "$ROOT/$path" ]; then
    skip_reason="$path not created yet (another lane owns it)"
  fi

  if [ -z "$skip_reason" ] && [[ ",$flags," == *",SILENT,"* ]] && [ "$SILENT" = "1" ]; then
    skip_reason="PET_TALK_SILENT=1 — this suite produces sound or starts an audio-capable daemon"
  fi

  if [ -z "$skip_reason" ] && [[ ",$flags," == *",REAL,"* ]] && [ "$REAL_ENGINE" != "1" ]; then
    skip_reason="PET_TALK_REAL_ENGINE!=1 — this suite hits the real STT/LLM/TTS stack; set the flag to run it"
  fi

  if [ -z "$skip_reason" ]; then
    port_flag="$(grep -oE 'PORT:[0-9]+' <<< "$flags" || true)"
    if [ -n "$port_flag" ]; then
      port="${port_flag#PORT:}"
      if ! port_open 127.0.0.1 "$port"; then
        skip_reason="127.0.0.1:$port refused (daemon not running)"
      fi
    fi
  fi

  if [ -n "$skip_reason" ]; then
    echo "SKIP  $label — $skip_reason"
    NAMES+=("$label"); STATUSES+=("SKIP"); REASONS+=("$skip_reason")
    continue
  fi

  t="$(timeout_for "$path")"
  case "$kind" in
    unit)  run_bounded "$t" python3 "$ROOT/$path" -v ;;
    main)  run_bounded "$t" python3 "$ROOT/$path" ;;
    shell) run_bounded "$t" bash "$ROOT/$path" "QA smoke: pet-talk speaks." ;;
    *) echo "FAIL  unknown suite kind: $kind"; rc=1 ;;
  esac
  rc=$?

  if [ "$rc" -eq 0 ]; then
    echo "PASS  $label"
    NAMES+=("$label"); STATUSES+=("PASS"); REASONS+=("")
  elif [ "$rc" -eq 142 ] || [ "$rc" -eq 124 ]; then
    echo "FAIL  $label — TIMEOUT after ${t}s"
    NAMES+=("$label"); STATUSES+=("FAIL"); REASONS+=("TIMEOUT after ${t}s")
    FAIL=1
  else
    echo "FAIL  $label — exit $rc"
    NAMES+=("$label"); STATUSES+=("FAIL"); REASONS+=("exit $rc")
    FAIL=1
  fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
PASS_N=0; FAIL_N=0; SKIP_N=0
for s in "${STATUSES[@]}"; do
  case "$s" in
    PASS) PASS_N=$((PASS_N + 1)) ;;
    FAIL) FAIL_N=$((FAIL_N + 1)) ;;
    SKIP) SKIP_N=$((SKIP_N + 1)) ;;
  esac
done

echo ""
echo "== summary =========================================================="
printf '%-3s  %-55s %s\n' "St" "suite" "reason"
for idx in "${!NAMES[@]}"; do
  printf '%-3s  %-55s %s\n' "${STATUSES[$idx]}" "${NAMES[$idx]}" "${REASONS[$idx]}"
done
echo "----------------------------------------------------------------------"
echo "PASS=$PASS_N  FAIL=$FAIL_N  SKIP=$SKIP_N  (total=$N)"
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: OK (passes + honest SKIP only)"
else
  echo "RESULT: FAIL"
fi
exit "$FAIL"
