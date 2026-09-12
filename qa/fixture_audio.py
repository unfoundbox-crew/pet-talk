"""Shared spoken-audio fixture resolution for the live-turn QA scripts.

One place, because `qa/latency.py` and `qa/live_ws_turn.py` measure the same
turn and must send the same bytes. Both used to carry their own copy of this,
and they disagreed: latency.py sent 320 samples of silence while
live_ws_turn.py looked for a real clip in /tmp. Silence transcribes to nothing
against a real STT tyre, so every turn-level number latency.py printed against
real providers was the `empty_transcript` error path, not a turn.

Resolution order:
  1. `$PET_TALK_FIXTURE_WAV` — an explicit override.
  2. `qa/fixtures/weather_turn.wav` — bake it with
     `bash qa/fixtures/make_weather_fixture.sh` (audio is gitignored by
     design, see .gitignore).
  3. `/tmp/weather_turn_fixture.wav` — where an earlier lane left one.
  4. 320 samples of silence — honest last resort. Fine for stub providers,
     useless against a real one, and `is_real` says which you got.

The protocol's `pcm_b64` is RAW pcm16, so the WAV header is stripped here: a
44-byte RIFF header fed to a provider as samples is 44 bytes of noise in front
of the speech, and it also loses the real sample rate.
"""
from __future__ import annotations

import base64
import os
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAKED = os.path.join(ROOT, "qa", "fixtures", "weather_turn.wav")
LEGACY = "/tmp/weather_turn_fixture.wav"
DEFAULT_SAMPLE_RATE = 16000


def fixture_path() -> str:
    for path in (os.environ.get("PET_TALK_FIXTURE_WAV") or "", BAKED, LEGACY):
        if path and os.path.exists(path):
            return path
    return ""


def load_pcm() -> tuple[bytes, int, str]:
    """Return (raw pcm16 bytes, sample_rate, source label)."""
    path = fixture_path()
    if not path:
        return bytes(320 * 2), DEFAULT_SAMPLE_RATE, "silence (no fixture — run qa/fixtures/make_weather_fixture.sh)"
    try:
        with wave.open(path) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise ValueError(
                    "fixture must be mono 16-bit, got %dch %d-bit"
                    % (w.getnchannels(), w.getsampwidth() * 8)
                )
            return w.readframes(w.getnframes()), w.getframerate(), path
    except (OSError, wave.Error, ValueError) as e:
        return bytes(320 * 2), DEFAULT_SAMPLE_RATE, f"silence (fixture unusable: {path}: {e})"


def load_b64() -> tuple[str, int, bool, str]:
    """Return (base64 pcm16, sample_rate, is_real, source label)."""
    pcm, rate, source = load_pcm()
    return base64.b64encode(pcm).decode(), rate, not source.startswith("silence"), source
