"""WS ``/events``: subscribe to ``companion_state`` packets. Same token as ``/ws``."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import ws_token_ok
from ..logs import log, swallowed
from .bridge import CompanionBridge

router = APIRouter()

#: The one bridge. Shared object — tests ``reset()`` it, never rebind it.
bridge = CompanionBridge()


@router.websocket("/events")
async def events_endpoint(ws: WebSocket) -> None:
    """Read-only for the client: anything it sends is ignored."""
    if not ws_token_ok(ws):
        log.warning("events_rejected reason=unauthorized")
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    await bridge.subscribe(ws)
    log.info("companion_subscribed subscribers=%d", bridge.broadcaster.count)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        swallowed("events_loop_failed", e)
    finally:
        await bridge.unsubscribe(ws)
        log.debug("companion_unsubscribed subscribers=%d", bridge.broadcaster.count)
