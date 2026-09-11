#!/usr/bin/env bash
# serve.sh — SpacePilot daemon for pet-talk (Kokoro TTS on 127.0.0.1:$PORT).
# Refuses to start under PET_TALK_SILENT=1 (this is an audio-capable daemon).
# Fails fast (bounded) if the `spacepilot` binary is missing, rather than
# letting `exec` fail with an unclear error. No absolute paths.
set -u

if [ "${PET_TALK_SILENT:-0}" = "1" ]; then
  echo "SKIP  serve.sh — PET_TALK_SILENT=1 (refusing to start an audio-capable daemon)"
  exit 0
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8088}"

if ! command -v spacepilot >/dev/null 2>&1; then
  echo "FAIL  serve.sh — \`spacepilot\` not found on PATH" >&2
  exit 1
fi

exec spacepilot serve --host "$HOST" --port "$PORT"
