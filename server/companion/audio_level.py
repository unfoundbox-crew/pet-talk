"""Audio level extractor: int16 PCM (raw or RIFF WAV) -> 0.0..1.0.

Runs on the send path of every ``agent.sentence`` / ``agent.chunk`` frame, so
its cost is bounded: at most :data:`MAX_SAMPLES` evenly strided samples are
read whatever the clip length. On a 10 s Kokoro clip (240k samples) that is a
~2% estimate for the price of a 4k-sample loop, and no jitter on TTS delivery.

Never raises. Undecodable audio measures as silence — a wrong level is a dim
orb, not a dropped turn.
"""
from __future__ import annotations

import io
import math
import wave
from array import array

from ..logs import swallowed

#: Upper bound on samples read per measurement (strided across the clip).
MAX_SAMPLES = 4096
#: int16 full scale; RMS is divided by this so a square wave reads 1.0.
FULL_SCALE = 32768.0


def _decode(audio: bytes) -> bytes:
    """Raw little-endian int16 bytes from ``audio``; ``b""`` when unusable."""
    if not audio:
        return b""
    if not audio.startswith(b"RIFF"):
        return audio
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            if w.getsampwidth() != 2:
                return b""
            return w.readframes(w.getnframes())
    except Exception as e:
        swallowed("companion_wav_unreadable", e)
        return b""


def pcm16_samples(audio: bytes, max_samples: int = MAX_SAMPLES) -> array:
    """At most ``max_samples`` int16 samples, evenly strided across the clip.

    Interleaved stereo is read as-is: the stride walks both channels, so the
    level is the mix's, which is what a listener hears.
    """
    pcm = _decode(audio)
    total = len(pcm) // 2
    samples = array("h")
    if total == 0:
        return samples
    if total <= max_samples:
        samples.frombytes(pcm[: total * 2])
        return samples
    stride = total // max_samples
    picked = array("h")
    picked.frombytes(pcm[: total * 2])
    return picked[::stride][:max_samples]


def rms_level(audio: bytes) -> float:
    """RMS amplitude normalised to 0.0..1.0 (full-scale square = 1.0)."""
    samples = pcm16_samples(audio)
    if not samples:
        return 0.0
    acc = 0
    for s in samples:
        acc += s * s
    return _clamp(math.sqrt(acc / len(samples)) / FULL_SCALE)


def peak_level(audio: bytes) -> float:
    """Peak absolute amplitude normalised to 0.0..1.0."""
    samples = pcm16_samples(audio)
    if not samples:
        return 0.0
    return _clamp(max(abs(s) for s in samples) / FULL_SCALE)


def _clamp(level: float) -> float:
    return min(1.0, max(0.0, level))
