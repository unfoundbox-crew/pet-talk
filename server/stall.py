"""Stall-phrase audio cache.

The cache key carries the TTS backend identity (class name plus endpoint) so a
tyre swap can never hand back another backend's audio. A synth failure raises
``stall_synth_failed`` rather than returning an audio id whose bytes do not
exist — a 404 mid-stall is worse than a named error.
"""
from __future__ import annotations

import asyncio
import uuid

from .audio_store import has_audio, store_audio
from .dictation import VoiceProseFormatter
from .persona import Persona
from .providers import ProviderError

MAX_STALL_WORDS = 15

_STALL_AUDIO_CACHE: dict[str, tuple[str, bytes]] = {}


def _tts_identity(tts: object) -> str:
    endpoint = (
        getattr(tts, "base_url", None)
        or getattr(tts, "endpoint", None)
        or getattr(tts, "voice_id", None)
        or ""
    )
    return f"{type(tts).__name__}@{endpoint}"


def stall_cache_key(text: str, p: Persona, tts: object) -> str:
    return f"{p.name}:{p.voice}:{p.speed}:{_tts_identity(tts)}:{text}"


async def get_or_synth_stall(stall_text: str, p: Persona, tts: object) -> tuple[str, bytes]:
    """Cached stall audio, synthesized off the loop on a miss.

    Raises ``ProviderError('stall_synth_failed')`` rather than returning an
    audio id whose bytes do not exist — a 404 mid-stall is worse than a
    named error.
    """
    clean_text = VoiceProseFormatter.sanitize(stall_text, max_words=MAX_STALL_WORDS) or stall_text
    key = stall_cache_key(clean_text, p, tts)
    cached = _STALL_AUDIO_CACHE.get(key)
    if cached is not None and has_audio(cached[0]):
        return cached
    _STALL_AUDIO_CACHE.pop(key, None)

    try:
        wav, _word_times = await asyncio.to_thread(
            tts.synth, clean_text, voice=p.voice, speed=p.speed
        )
    except ProviderError as e:
        raise ProviderError("stall_synth_failed", f"{e.reason}: {e.detail}") from e
    except Exception as e:
        raise ProviderError("stall_synth_failed", str(e)) from e
    if not wav:
        raise ProviderError("stall_synth_failed", "backend returned no audio")

    audio_id = f"stall-{uuid.uuid4().hex[:8]}"
    store_audio(audio_id, wav)
    _STALL_AUDIO_CACHE[key] = (audio_id, wav)
    return audio_id, wav

