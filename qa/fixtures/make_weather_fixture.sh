#!/usr/bin/env bash
# Regenerate the spoken-audio fixture the live-turn QA scripts use.
#
# The repo keeps audio out of git ("audio evidence (reproducible via bake
# scripts)" — see .gitignore), so this is the bake script. It writes
# qa/fixtures/weather_turn.wav: one real English question, 16kHz mono 16-bit
# PCM, which is exactly what the `/ws` protocol carries in `pcm_b64`.
#
# Why a real clip at all: `qa/latency.py` used to send 320 samples of silence.
# Against stub STT that is fine (the stub answers regardless), but against any
# real STT it transcribes to nothing, the turn ends in `empty_transcript`, and
# every turn-level metric reads NOT-MEASURED. A real clip is the difference
# between measuring the turn and measuring the error path.
#
# macOS only (uses `say`). Writes no audio to the speakers.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/weather_turn.wav"
TEXT="${FIXTURE_TEXT:-What is the weather today?}"
VOICE="${FIXTURE_VOICE:-Samantha}"

command -v say >/dev/null || { echo "need macOS \`say\`" >&2; exit 1; }
say -v "$VOICE" -o "$OUT" --data-format=LEI16@16000 --channels=1 "$TEXT"
python3 - "$OUT" <<'PY'
import sys, wave
w = wave.open(sys.argv[1])
print("wrote %s: %dch %dHz %d-bit %.2fs" % (
    sys.argv[1], w.getnchannels(), w.getframerate(), w.getsampwidth() * 8,
    w.getnframes() / w.getframerate()))
PY
