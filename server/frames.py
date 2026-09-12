"""WS frame construction and safe delivery (TECH-SPEC section 4).

Every frame carries ``turn_id``. A frame without one is a bug, not a
degraded frame, so :func:`frame` fails closed.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .logs import log, redact_home, swallowed
from .providers import ProviderError

_turn_counter = itertools.count(1)


def new_turn_id() -> str:
    return f"t{next(_turn_counter)}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Frame:
    """One wire frame. ``fields`` carries the per-type payload."""

    type: str
    turn_id: str
    fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ProviderError("frame_no_turn_id", self.type)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "turn_id": self.turn_id, **self.fields}


def frame(ftype: str, turn_id: str, **fields: Any) -> dict[str, Any]:
    """Build a wire-ready frame dict. Fails closed on a missing turn_id."""
    return Frame(type=ftype, turn_id=turn_id, fields=fields).to_dict()


def error_frame(turn_id: str, reason: str, detail: str = "", **fields: Any) -> dict[str, Any]:
    """Build ``agent.error``. ``detail`` is redacted on the way out.

    Details are built from whatever failed — including another process's
    stderr, which names absolute paths. One choke point here means no caller
    has to remember; ``/Users/<name>`` never reaches a client.
    """
    payload = dict(fields)
    if detail:
        payload["detail"] = redact_home(detail)
    return frame("agent.error", turn_id, reason=reason, **payload)


async def safe_send_json(ws: WebSocket, payload: dict[str, Any]) -> bool:
    """Send a frame, never raising. Logs the named reason when it cannot.

    Returns True iff the frame reached the socket.
    """
    try:
        if ws.client_state != WebSocketState.CONNECTED:
            log.debug(
                "send_skipped reason=ws_not_connected type=%s state=%s",
                payload.get("type"),
                getattr(ws.client_state, "name", ws.client_state),
            )
            return False
        await ws.send_json(payload)
        return True
    except WebSocketDisconnect as e:
        swallowed("ws_send_disconnected", e, frame_type=payload.get("type"))
        return False
    except RuntimeError as e:
        # uvicorn: "Unexpected ASGI message 'websocket.send' after close"
        swallowed("ws_send_after_close", e, frame_type=payload.get("type"))
        return False
    except Exception as e:  # never let a dead socket kill a turn
        swallowed("ws_send_failed", e, frame_type=payload.get("type"))
        return False


async def send_error(
    ws: WebSocket, turn_id: str, reason: str, detail: str = "", **fields: Any
) -> bool:
    """Emit ``agent.error`` with a named reason (law 1) and log it."""
    log.info(
        "agent_error turn_id=%s reason=%s detail=%s",
        turn_id, reason, redact_home(detail),
    )
    return await safe_send_json(ws, error_frame(turn_id, reason, detail, **fields))


def parse_int_field(
    msg: dict[str, Any], key: str, default: int, minimum: Optional[int] = None
) -> int:
    """Parse one int field from an untrusted frame. Raises ProviderError."""
    raw = msg.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ProviderError("bad_frame", f"{key}={raw!r} is not an integer") from e
    if minimum is not None and value < minimum:
        raise ProviderError("bad_frame", f"{key}={value} below minimum {minimum}")
    return value


def parse_float_field(msg: dict[str, Any], key: str, default: float) -> float:
    raw = msg.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as e:
        raise ProviderError("bad_frame", f"{key}={raw!r} is not a number") from e
