"""The companion state machine: wire frames in, ``companion_state`` out.

Every wire frame goes out through :func:`server.frames.safe_send_json`, which
calls :meth:`CompanionBridge.observe_frame` first. So the bridge sees every
path (control, stall+worker, direct, streaming STT, barge) without a hook in
each of them, and cannot drift from what the client was told.

``observe_frame`` is sync and bounded (see ``audio_level``); publishing is a
queue drop (see ``broadcast``). Nothing here awaits a socket.
"""
from __future__ import annotations

import collections
from typing import Any, Optional

from .broadcast import Broadcaster
from .packets import ORB_ID, STATE_IDLE, companion_state_packet, transition_for_frame

#: Packets kept for inspection (tests, debugging).
HISTORY_LEN = 256
#: Levels closer than this are the same packet.
LEVEL_EPS = 1e-4


class CompanionBridge:
    def __init__(self, broadcaster: Optional[Broadcaster] = None, orb_id: str = ORB_ID) -> None:
        self.broadcaster = broadcaster or Broadcaster()
        self.orb_id = orb_id
        self.state: str = STATE_IDLE
        self.audio_level: float = 0.0
        self.turn_id: str = ""
        self.last: Optional[dict[str, Any]] = None
        self.history: collections.deque = collections.deque(maxlen=HISTORY_LEN)

    def reset(self) -> None:
        self.state, self.audio_level, self.turn_id, self.last = STATE_IDLE, 0.0, "", None
        self.history.clear()

    def packet(self) -> dict[str, Any]:
        return companion_state_packet(self.state, self.audio_level, self.turn_id, self.orb_id)

    def observe_frame(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Fold one outgoing wire frame in; publish and return the packet, if any."""
        transition = transition_for_frame(payload, self.state, self.audio_level)
        if transition is None:
            return None
        state, level = transition
        turn_id = str(payload.get("turn_id") or self.turn_id)
        if (
            state == self.state
            and abs(level - self.audio_level) < LEVEL_EPS
            and turn_id == self.turn_id
        ):
            return None
        self.state, self.audio_level, self.turn_id = state, level, turn_id
        packet = self.packet()
        self.last = packet
        self.history.append(packet)
        self.broadcaster.publish(packet)
        return packet

    async def subscribe(self, ws: Any) -> None:
        """A late joiner gets the current state at once, never a blank orb."""
        await self.broadcaster.subscribe(ws, hello=self.last or self.packet())

    async def unsubscribe(self, ws: Any) -> None:
        await self.broadcaster.unsubscribe(ws)
