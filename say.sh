#!/usr/bin/env bash
# say.sh — streaming speak: synthesize chunk N+1 while chunk N plays.
# Usage: ./say.sh "First sentence. Second sentence." [voice]
# Env: VOICE (default af_heart), SPEED (default 1.0), PORT (default 8088),
#      STUDIO_TOKEN_FILE (default: pipx spacepilot venv .studio_token),
#      PET_TALK_SILENT=1 to SKIP without any network call or playback,
#      POLL_TIMEOUT_S (default 120) wall-clock bound on the synth poll loop.
# Needs: spacepilot daemon (./serve.sh) with kokoro_onnx installed.
set -u

if [ "${PET_TALK_SILENT:-0}" = "1" ]; then
  echo "SKIP  say.sh — PET_TALK_SILENT=1 (no network call, no playback)"
  exit 0
fi

TEXT="${1:?usage: ./say.sh \"text to speak\" [voice]}"
VOICE="${2:-${VOICE:-af_heart}}"
PORT="${PORT:-8088}"
SPEED="${SPEED:-1.0}"
TOKEN_FILE="${STUDIO_TOKEN_FILE:-$HOME/Library/Application Support/pipx/venvs/spacepilot/lib/python3.14/.studio_token}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-120}"
CURL_MAX_TIME="${CURL_MAX_TIME:-10}"
BASE="http://127.0.0.1:${PORT}"
TOKEN="$(cat "$TOKEN_FILE")"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

printf '%s' "$TEXT" | python3 -c "
import sys, re
t = sys.stdin.read().strip()
for s in re.split(r'(?<=[.!?])\s+', t):
    s = s.strip()
    if s: print(s)
" > "$TMP/sentences.txt"

synth_chunk() { # $1=text $2=outfile: POST, poll (wall-clock bounded), download.
  local text="$1" out="$2" job status fpath deadline
  job=$(curl -sf --max-time "$CURL_MAX_TIME" -X POST "$BASE/api/generate/voice" \
    -H 'Content-Type: application/json' \
    -H "X-SpacePilot-Token: $TOKEN" \
    --data @- <<EOF
{"text": $(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), "voice": "$VOICE", "speed": $SPEED}
EOF
  ) || return 1
  job=$(printf '%s' "$job" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

  deadline=$(( $(date +%s) + POLL_TIMEOUT_S ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    status=$(curl -sf --max-time "$CURL_MAX_TIME" "$BASE/api/jobs/$job" -H "X-SpacePilot-Token: $TOKEN") || return 1
    st=$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    if [ "$st" = "completed" ]; then
      fpath=$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["file_path"])')
      cp "$fpath" "$out" && printf '%s' "$out" && return 0
    fi
    [ "$st" = "failed" ] && { printf '%s\n' "$status" >&2; return 1; }
    sleep 2
  done
  echo "timeout waiting for $job after ${POLL_TIMEOUT_S}s" >&2
  return 1
}

i=0
prev=""
while IFS= read -r sent; do
  i=$((i + 1))
  synth_chunk "$sent" "$TMP/c$i.wav" > "$TMP/got$i.txt" 2>"$TMP/err$i.txt" &
  render_pid=$!
  if [ -n "$prev" ]; then afplay "$prev"; fi
  wait $render_pid || { cat "$TMP/err$i.txt" >&2; exit 1; }
  prev=$(cat "$TMP/got$i.txt")
done < "$TMP/sentences.txt"
[ -n "$prev" ] && afplay "$prev"
