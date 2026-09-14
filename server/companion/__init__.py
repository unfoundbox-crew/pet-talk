"""Companion state bridge for the 33-orb skin (mvec-browser).

    audio_level.py  int16 PCM -> 0.0..1.0 (bounded-cost RMS / peak)
    packets.py      the ``companion_state`` packet; wire frame -> state mapping
    broadcast.py    non-blocking WebSocket fan-out (per-subscriber queues)
    bridge.py       the state machine: observe frames, publish packets
    events.py       WS ``/events`` and the process-wide ``bridge``
"""
from .bridge import CompanionBridge
from .events import bridge, router

__all__ = ["CompanionBridge", "bridge", "router"]
