"""Stall-phrase audio cache.

The cache key carries the TTS backend identity (class name plus endpoint) so a
tyre swap can never hand back another backend's audio. A synth failure raises
``stall_synth_failed`` rather than returning an audio id whose bytes do not
exist — a 404 mid-stall is worse than a named error.
"""
from __future__ import annotations

import collections
import os
import uuid

from typing import NamedTuple, Optional

from .audio_store import has_audio, store_audio
from .dictation import VoiceProseFormatter
from .logs import log, swallowed
from .persona import Persona
from .providers import ProviderError
from .speak_queue import SpokenSentence
from .speech import synth_off_thread

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


class StallAudio(NamedTuple):
    """Cached stall audio. ``word_times`` rides along so the stall's
    ``agent.sentence`` carries timings like every other sentence frame."""

    audio_id: str
    wav: bytes
    word_times: list


_STALL_AUDIO_CACHE: "collections.OrderedDict[str, StallAudio]" = collections.OrderedDict()


def _cache_get(key: str) -> Optional[StallAudio]:
    """Read and mark as most-recently-used."""
    if key in _STALL_AUDIO_CACHE:
        _STALL_AUDIO_CACHE.move_to_end(key)
        return _STALL_AUDIO_CACHE[key]
    return None


def _cache_put(key: str, value: StallAudio) -> None:
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


def stall_warm_enabled() -> bool:
    """``PET_TALK_STALL_WARM=0`` skips the startup pre-synth, loudly."""
    return os.environ.get("PET_TALK_STALL_WARM", "1") != "0"


async def warm_stall_cache(p: Persona, tts: object) -> int:
    """Pre-synthesize the persona's stall phrases into the LRU. Returns the
    count actually synthesized.

    Why this exists: the first turn of a fresh process paid one full stall
    synth, and that single cold miss was the whole of the ``turn_worst_ms``
    breach (measured 2026-09-12b: 1455.3ms cold against 291.2ms warm, budget
    1200ms). ``KokoroLocalTTS`` already warms its model this way; the stall
    cache did not.

    Never raises. A backend that cannot synthesize at startup — no key, no
    daemon, no weights — logs ``stall_warm_failed`` with the provider's own
    named reason and returns however many phrases did land. A cold cache is a
    slow first turn; a startup that dies because TTS was not ready yet is a
    dead product. Nothing here blocks the WS port: the caller schedules it as a
    task, it never awaits the accept loop.
    """
    if not stall_warm_enabled():
        log.info("stall_warm_disabled reason=PET_TALK_STALL_WARM=0 persona=%s", p.name)
        return 0
    phrases = [s for s in (p.stalls or []) if str(s).strip()]
    if not phrases:
        log.info("stall_warm_skipped reason=persona_no_stalls persona=%s", p.name)
        return 0
    warmed = 0
    for phrase in phrases:
        try:
            await get_or_synth_stall(phrase, p, tts)
            warmed += 1
        except ProviderError as e:
            log.info(
                "stall_warm_failed persona=%s reason=%s detail=%s",
                p.name,
                e.reason,
                e.detail,
            )
        except Exception as e:
            swallowed("stall_warm_failed", e, persona=p.name)
    log.info(
        "stall_warm_done persona=%s warmed=%d of=%d cache_size=%d backend=%s",
        p.name,
        warmed,
        len(phrases),
        stall_cache_size(),
        type(tts).__name__,
    )
    return warmed


async def get_or_synth_stall(stall_text: str, p: Persona, tts: object) -> StallAudio:
    """Cached stall audio, synthesized off the loop on a miss.

    Raises ``ProviderError('stall_synth_failed')`` rather than returning an
    audio id whose bytes do not exist — a 404 mid-stall is worse than a
    named error. Returns a :class:`StallAudio` (audio_id, wav, word_times).
    """
    clean_text = VoiceProseFormatter.sanitize(stall_text, max_words=MAX_STALL_WORDS) or stall_text
    key = stall_cache_key(clean_text, p, tts)
    cached = _cache_get(key)
    if cached is not None and has_audio(cached.audio_id):
        return cached
    _STALL_AUDIO_CACHE.pop(key, None)

    try:
        wav, word_times = await synth_off_thread(tts, clean_text, p.voice, p.speed)
    except ProviderError as e:
        raise ProviderError("stall_synth_failed", f"{e.reason}: {e.detail}") from e
    except Exception as e:
        raise ProviderError("stall_synth_failed", str(e)) from e
    if not wav:
        raise ProviderError("stall_synth_failed", "backend returned no audio")

    audio_id = f"stall-{uuid.uuid4().hex[:8]}"
    store_audio(audio_id, wav)
    entry = StallAudio(
        audio_id=audio_id,
        wav=wav,
        word_times=SpokenSentence.from_synth(0, clean_text, word_times).word_times,
    )
    _cache_put(key, entry)
    return entry

