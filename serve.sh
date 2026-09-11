#!/usr/bin/env bash
# serve.sh — SpacePilot daemon for pet-talk (Kokoro TTS on 127.0.0.1:8088).
set -u
exec spacepilot serve --host 127.0.0.1 --port 8088
