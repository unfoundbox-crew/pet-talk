"""Stall-phrase audio cache.

The cache key carries the TTS backend identity (class name plus endpoint) so a
tyre swap can never hand back another backend's audio. A synth failure raises
``stall_synth_failed`` rather than returning an audio id whose bytes do not
exist — a 404 mid-stall is worse than a named error.
"""
from __future__ import annotations

import asyncio
import collections
import os
import uuid

from typing import Optional

from .audio_store import has_audio, store_audio
from .dictation import VoiceProseFormatter
from .logs import swallowed
from .persona import Persona
from .providers import ProviderError

MAX_STALL_WORDS = 15

#: The cache key carries persona, voice, speed and backend identity — and
#: voice and speed come from the client. An unbounded dict was therefore a
#: client-controlled memory leak: every distinct speed held another WAV for
#: the life of the process. Bounded LRU, oldest evicted first.
DEFAULT_STALL_CACHE_MAX = 64


def stall_cache_max() -> int:
    raw = os.environ.get("PET_TALK_STALL_CACHE_MAX", str(DEFAULT_STALL_CACHE_MAX))
    try:
        return max(1, int(raw))
    except ValueError:
        swallowed("bad_stall_cache_max_env", None, value=raw)
        return DEFAULT_STALL_CACHE_MAX


_STALL_AUDIO_CACHE: "collections.OrderedDict[str, tuple[str, bytes]]" = (
    collections.OrderedDict()
)


def _cache_get(key: str) -> Optional[tuple[str, bytes]]:
    """Read and mark as most-recently-used."""
    if key in _STALL_AUDIO_CACHE:
        _STALL_AUDIO_CACHE.move_to_end(key)
        return _STALL_AUDIO_CACHE[key]
    return None


def _cache_put(key: str, value: tuple[str, bytes]) -> None:
    _STALL_AUDIO_CACHE[key] = value
    _STALL_AUDIO_CACHE.move_to_end(key)
    cap = stall_cache_max()
    while len(_STALL_AUDIO_CACHE) > cap:
        _STALL_AUDIO_CACHE.popitem(last=False)


def stall_cache_size() -> int:
    """Entries held right now. Used by the QA gate, not by the loop."""
    return len(_STALL_AUDIO_CACHE)


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
    cached = _cache_get(key)
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
    _cache_put(key, (audio_id, wav))
    return audio_id, wav

