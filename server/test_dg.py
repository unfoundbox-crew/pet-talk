"""Lane DG: Deepgram tyre verification. Stdlib only. NEVER prints the key.

Run: python3 server/test_dg.py
- Auth-fail path (bogus key) for STT + TTS: expect ProviderError, zero spend.
- Live test ONLY if DEEPGRAM_API_KEY present in os.environ:
  ONE 2-second STT call + ONE short TTS line. Prints credit math.
"""
from __future__ import annotations

import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.providers import (  # noqa: E402
    DeepgramSTT,
    DeepgramTTS,
    ProviderError,
    make_stt,
    make_tts,
)


def sine_pcm16(seconds: float, sample_rate: int = 16000) -> bytes:
    n = int(seconds * sample_rate)
    out = bytearray()
    for i in range(n):
        s = int(32767 * 0.3 * math.sin(2 * math.pi * 440.0 * i / sample_rate))
        out += struct.pack("<h", s)
    return bytes(out)


def main() -> int:
    failures = 0

    # 1. Factory mapping (no network).
    os.environ["TTS_PROVIDER"] = "deepgram"
    os.environ["STT_PROVIDER"] = "deepgram"
    assert isinstance(make_tts(), DeepgramTTS), "make_tts deepgram mapping broken"
    assert isinstance(make_stt(), DeepgramSTT), "make_stt deepgram mapping broken"
    print("factory: TTS_PROVIDER=deepgram -> DeepgramTTS OK; "
          "STT_PROVIDER=deepgram -> DeepgramSTT OK")
    os.environ["STT_PROVIDER"] = "whisper-local"
    try:
        make_stt()
        print("factory: whisper-local did NOT raise -- FAIL")
        failures += 1
    except ProviderError as e:
        assert "not wired" in str(e).lower(), f"unexpected reason: {e}"
        print(f"factory: whisper-local raises fail-closed OK ({e.reason})")
    finally:
        del os.environ["STT_PROVIDER"]
        del os.environ["TTS_PROVIDER"]

    # 2. Auth-fail path with bogus key (expect ProviderError, zero spend:
    #    401 never transcribes/synthesizes, so nothing billable runs).
    real_key = os.environ.get("DEEPGRAM_API_KEY", "")
    os.environ["DEEPGRAM_API_KEY"] = "bogus-key-for-auth-test"
    try:
        DeepgramSTT().transcribe(sine_pcm16(0.5))
        print("auth-fail STT: NO ERROR -- FAIL (expected ProviderError)")
        failures += 1
    except ProviderError as e:
        print(f"auth-fail STT: ProviderError OK (reason={e.reason}) zero spend")
    try:
        DeepgramTTS().synth("hello")
        print("auth-fail TTS: NO ERROR -- FAIL (expected ProviderError)")
        failures += 1
    except ProviderError as e:
        print(f"auth-fail TTS: ProviderError OK (reason={e.reason}) zero spend")

    # 3. Live test only if a real key is in env (doppler unavailable here).
    if not real_key:
        print("live: SKIP -- DEEPGRAM_API_KEY absent from os.environ "
              "(doppler not available to this lane)")
        print(f"RESULT: {'PASS' if failures == 0 else 'FAIL'} "
              f"(failures={failures}, live=SKIP)")
        return 1 if failures else 0
    os.environ["DEEPGRAM_API_KEY"] = real_key
    audio = sine_pcm16(2.0)
    try:
        text = DeepgramSTT().transcribe(audio, 16000)
        print(f"live STT: 2.0s audio -> {len(text)} chars: {text!r}")
        stt_s = 2.0
    except ProviderError as e:
        print(f"live STT: ProviderError reason={e.reason} detail={e.detail}")
        stt_s = 0.0
    line = "Hello from the pet-talk bake-off."
    try:
        wav, _ = DeepgramTTS().synth(line)
        print(f"live TTS: {len(line)} chars -> {len(wav)} wav bytes")
        tts_c = len(line)
    except ProviderError as e:
        print(f"live TTS: ProviderError reason={e.reason} detail={e.detail}")
        tts_c = 0
    print(f"CREDIT MATH: stt_seconds_used={stt_s} tts_chars_used={tts_c}")
    print(f"RESULT: {'PASS' if failures == 0 else 'FAIL'} (failures={failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
