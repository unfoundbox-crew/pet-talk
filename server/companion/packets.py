"""The ``companion_state`` packet and the wire-frame -> state mapping.

    {"type": "companion_state", "state": "idle" | "thinking" | "speaking",
     "audio_level": 0.0..1.0, "orb_id": "orb-33", "timestamp": <unix s>,
     "turn_id": "<the turn that caused it>"}

State mapping (docs/SPEC.md 4.2 frames -> companion state):

    state.idle, state.listening, agent.done  -> idle      (level 0)
    transcript.user (final), state.thinking  -> thinking  (level 0)
    state.speaking                           -> speaking  (level kept)
    agent.sentence / agent.chunk             -> speaking  (level = RMS of its audio)

Everything else (stalls, errors, receipts, partial transcripts) says nothing
about the orb and maps to ``None``.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any, Optional

from ..audio_store import get_audio
from ..logs import swallowed
from .audio_level import rms_level

PACKET_TYPE = "companion_state"
STATE_IDLE = "idle"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATES = (STATE_IDLE, STATE_THINKING, STATE_SPEAKING)

#: The orb the packets name. One companion per server today.
ORB_ID = os.environ.get("PET_TALK_ORB_ID", "orb-33")

_IDLE_FRAMES = frozenset({"state.idle", "state.listening", "agent.done"})
_AUDIO_FRAMES = frozenset({"agent.sentence", "agent.chunk"})


def companion_state_packet(
    state: str, audio_level: float, turn_id: str, orb_id: str = ORB_ID
) -> dict[str, Any]:
    """Build one packet. Unknown states fail closed; levels are clamped."""
    if state not in STATES:
        raise ValueError(f"companion_state unknown state {state!r}")
    return {
        "type": PACKET_TYPE,
        "state": state,
        "audio_level": round(min(1.0, max(0.0, float(audio_level))), 4),
        "orb_id": orb_id,
        "timestamp": time.time(),
        "turn_id": turn_id,
    }


def frame_audio(payload: dict[str, Any]) -> Optional[bytes]:
    """The audio an ``agent.sentence`` / ``agent.chunk`` frame describes."""
    b64 = payload.get("audio_b64")
    if b64:
        try:
            return base64.b64decode(str(b64), validate=False)
        except Exception as e:
            swallowed("companion_audio_b64_bad", e)
            return None
    url = str(payload.get("audio_url") or "")
    if url.startswith("/audio/"):
        return get_audio(url[len("/audio/"):])
    return None


def transition_for_frame(
    payload: dict[str, Any], state: str, audio_level: float
) -> Optional[tuple[str, float]]:
    """Map one outgoing wire frame to ``(state, level)`` given the current pair.

    ``None`` means the frame says nothing about the companion.
    """
    ftype = str(payload.get("type") or "")
    if ftype in _IDLE_FRAMES:
        return STATE_IDLE, 0.0
    if ftype == "state.thinking":
        return STATE_THINKING, 0.0
    if ftype == "transcript.user":
        if payload.get("partial"):
            return None  # still listening; the final transcript flips it
        return STATE_THINKING, 0.0
    if ftype == "state.speaking":
        # The level belongs to the audio that follows. Keep the last
        # sentence's rather than blinking to zero between sentences.
        return STATE_SPEAKING, (audio_level if state == STATE_SPEAKING else 0.0)
    if ftype in _AUDIO_FRAMES:
        audio = frame_audio(payload)
        return STATE_SPEAKING, (rms_level(audio) if audio else 0.0)
    return None
