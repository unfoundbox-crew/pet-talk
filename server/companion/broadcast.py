"""WebSocket fan-out that never blocks the publisher.

``publish`` is synchronous: it drops the packet into each subscriber's bounded
queue and returns. A sender task per subscriber drains that queue. A slow
subscriber therefore costs the TTS path nothing — its own queue drops the
oldest packet (the orb only ever wants the newest state) and the count is
kept, named. A subscriber whose send raises is dropped, named.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from ..logs import log, swallowed

#: Packets a slow subscriber may have waiting. Small on purpose: the newest
#: state is the only one worth rendering.
QUEUE_DEPTH = 8


@dataclass
class Subscription:
    ws: Any
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_DEPTH))
    task: Optional[asyncio.Task] = None
    dropped: int = 0
    sent: int = 0

    def offer(self, packet: dict[str, Any]) -> None:
        """Enqueue without waiting; evict the oldest when full."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(packet)


class Broadcaster:
    def __init__(self) -> None:
        self._subs: dict[int, Subscription] = {}

    @property
    def count(self) -> int:
        return len(self._subs)

    def subscription(self, ws: Any) -> Optional[Subscription]:
        return self._subs.get(id(ws))

    async def subscribe(self, ws: Any, hello: Optional[dict[str, Any]] = None) -> Subscription:
        """Register ``ws``; ``hello`` (the current state) is sent inline first."""
        sub = Subscription(ws=ws)
        if hello is not None:
            await ws.send_json(hello)
        sub.task = asyncio.create_task(self._drain(sub), name="companion:send")
        self._subs[id(ws)] = sub
        return sub

    async def unsubscribe(self, ws: Any) -> None:
        sub = self._subs.pop(id(ws), None)
        if sub is None or sub.task is None:
            return
        sub.task.cancel()
        try:
            await sub.task
        except (asyncio.CancelledError, Exception):
            pass

    def publish(self, packet: dict[str, Any]) -> int:
        """Offer ``packet`` to every subscriber. Returns how many were offered.

        Sync, no await, no socket I/O: safe to call from the send path.
        """
        for sub in self._subs.values():
            sub.offer(packet)
        return len(self._subs)

    async def _drain(self, sub: Subscription) -> None:
        try:
            while True:
                packet = await sub.queue.get()
                try:
                    await sub.ws.send_json(packet)
                    sub.sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._subs.pop(id(sub.ws), None)
                    swallowed(
                        "companion_subscriber_dropped", e,
                        sent=sub.sent, dropped=sub.dropped,
                    )
                    return
        except asyncio.CancelledError:
            if sub.dropped:
                log.info(
                    "companion_subscriber_closed sent=%d dropped=%d", sub.sent, sub.dropped
                )
            raise
