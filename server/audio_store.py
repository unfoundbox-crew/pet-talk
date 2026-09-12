"""Bounded in-process WAV store behind ``GET /audio/{id}``.

LRU, capped at :data:`MAX_AUDIO_STORE_ENTRIES` so a long session cannot leak
RAM. The store object itself is shared (tests clear it in place) — never
rebind the name.
"""
from __future__ import annotations

import collections
from typing import Optional

MAX_AUDIO_STORE_ENTRIES = 250

AUDIO_STORE: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()


def store_audio(audio_id: str, wav: bytes) -> None:
    """Insert (or refresh) one WAV, evicting the least recently used."""
    AUDIO_STORE[audio_id] = wav
    AUDIO_STORE.move_to_end(audio_id)
    while len(AUDIO_STORE) > MAX_AUDIO_STORE_ENTRIES:
        AUDIO_STORE.popitem(last=False)


def get_audio(audio_id: str) -> Optional[bytes]:
    """Return the WAV and mark it recently used, or None when absent."""
    wav = AUDIO_STORE.get(audio_id)
    if wav is not None:
        AUDIO_STORE.move_to_end(audio_id)
    return wav


def has_audio(audio_id: str) -> bool:
    return audio_id in AUDIO_STORE
