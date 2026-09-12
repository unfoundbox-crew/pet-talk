"""The WS ``/ws`` endpoint: one reader loop, many cancellable turn tasks.

The reader loop does nothing slow. STT, LLM and TTS all live inside per-turn
tasks, so a ``barge`` frame is readable at any moment — it cancels the
in-flight turn task and flushes the speak queue, and the ack carries the true
number of sentences dropped.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import ws_token_ok
from .frames import frame, new_turn_id, parse_int_field, safe_send_json, send_error
from .logs import log, swallowed
from .persona import Persona
from .providers import ProviderError
from .speak_queue import SpeakQueue
from . import runtime
from .persona_runtime import apply_persona_overrides, resolve_persona
from .turn import handle_turn_task

router = APIRouter()

EYES_MODULE = "server.eyes"
MAX_PCM_BYTES = 32 * 1024 * 1024


@dataclass
class Session:
    """Per-socket state. One speak queue, one live turn at a time."""

    ws: WebSocket
    queue: SpeakQueue = field(default_factory=SpeakQueue)
    turn_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    turn_persona: dict[str, Persona] = field(default_factory=dict)
    turn_id: str = field(default_factory=new_turn_id)
    chunks: list[bytes] = field(default_factory=list)

    def track(self, task: asyncio.Task) -> asyncio.Task:
        self.active_tasks.add(task)
        task.add_done_callback(self.active_tasks.discard)
        return task

    async def barge(self, ref: Optional[str] = None, exclude: Optional[str] = None) -> int:
        """Kill playback: cancel the live turn, then flush. Returns dropped.

        Cancelling first freezes the queue — nothing else runs on this loop
        between the cancel and the flush — so the count is exact. ``exclude``
        spares one turn, which is how a spoken "stop" cancels what is playing
        without cancelling the turn that carried the command.
        """
        targets: list[asyncio.Task] = []
        if ref and ref in self.turn_tasks and ref != exclude:
            targets.append(self.turn_tasks.pop(ref))
        else:
            for tid in [t for t in self.turn_tasks if t != exclude]:
                targets.append(self.turn_tasks.pop(tid))
        for task in targets:
            if not task.done():
                task.cancel()
        dropped = await self.queue.flush()
        log.info(
            "barge ref=%s exclude=%s cancelled=%d dropped=%d",
            ref, exclude, len(targets), dropped,
        )
        return dropped

    async def cancel_others(self, turn_id: str) -> int:
        """Barge everything except ``turn_id`` — the deterministic stop path."""
        return await self.barge(None, exclude=turn_id)

    async def supersede(self, turn_id: str) -> int:
        """A new user turn supersedes any turn still speaking.

        The speak queue is per-socket, so two overlapping turns would
        interleave sentences. The newer turn wins; the older is barged.
        """
        live = [t for t in self.turn_tasks if t != turn_id]
        if not live:
            return 0
        return await self.barge(None, exclude=turn_id)

    async def shutdown(self) -> None:
        """Cancel and reap every turn task, even if we are cancelled ourselves."""
        pending = list(self.active_tasks) + list(self.turn_tasks.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            try:
                await asyncio.shield(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            except asyncio.CancelledError:
                swallowed("ws_shutdown_cancelled", None, pending=len(pending))
        try:
            await asyncio.shield(self.queue.flush())
        except asyncio.CancelledError:
            swallowed("ws_shutdown_flush_cancelled", None)


def _decode_b64(value: str, field_name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=False)
    except Exception as e:
        raise ProviderError("bad_frame", f"{field_name} is not valid base64: {e}") from e
    if len(raw) > MAX_PCM_BYTES:
        raise ProviderError("bad_frame", f"{field_name} exceeds {MAX_PCM_BYTES} bytes")
    return raw


def _persona_for_frame(msg: dict[str, Any]) -> Persona:
    """Resolve the persona named by a frame, applying per-turn overrides."""
    return apply_persona_overrides(resolve_persona(msg.get("persona")), msg)


def eyes_module() -> Optional[Any]:
    """Import ``server.eyes`` if lane E has landed it, else None."""
    if importlib.util.find_spec(EYES_MODULE) is None:
        return None
    try:
        return importlib.import_module(EYES_MODULE)
    except Exception as e:
        swallowed("eyes_import_failed", e)
        return None


# ------------------------------------------------------------- handlers ---


async def _on_user_start(session: Session, msg: dict[str, Any]) -> None:
    session.chunks = []
    session.turn_id = msg.get("turn_id") or new_turn_id()
    session.turn_persona[session.turn_id] = _persona_for_frame(msg)
    await safe_send_json(session.ws, frame("state.listening", session.turn_id))


async def _on_user_chunk(session: Session, msg: dict[str, Any]) -> None:
    b64 = msg.get("chunk") or ""
    if not b64:
        return
    session.chunks.append(_decode_b64(str(b64), "chunk"))


async def _on_user_stop(session: Session, msg: dict[str, Any]) -> None:
    session.turn_id = msg.get("turn_id") or session.turn_id
    turn_id = session.turn_id
    pcm_b64 = msg.get("pcm_b64") or ""
    if pcm_b64:
        decoded = _decode_b64(str(pcm_b64), "pcm_b64")
        if decoded:
            session.chunks = [decoded]  # client's merged buffer wins
    audio = b"".join(session.chunks)
    session.chunks = []
    if not audio:
        await send_error(session.ws, turn_id, "stt_empty_audio", "no audio in this turn")
        return
    sample_rate = parse_int_field(msg, "sample_rate", 16000, minimum=1)
    persona = session.turn_persona.get(turn_id) or _persona_for_frame(msg)
    session.turn_persona[turn_id] = persona
    await session.supersede(turn_id)
    # Fire and forget: STT runs inside the task, so the reader loop stays free
    # for a mid-turn barge.
    session.track(
        asyncio.create_task(
            handle_turn_task(
                session.ws,
                turn_id,
                None,
                session.queue,
                session.turn_tasks,
                active_persona=persona,
                on_cancel=session.cancel_others,
                audio=audio,
                sample_rate=sample_rate,
            ),
            name=f"turn:{turn_id}",
        )
    )


async def _on_user_text(session: Session, msg: dict[str, Any]) -> None:
    session.turn_id = msg.get("turn_id") or new_turn_id()
    turn_id = session.turn_id
    text = str(msg.get("text") or "").strip()
    if not text:
        await send_error(session.ws, turn_id, "empty_text", "user.text carried no text")
        return
    persona = _persona_for_frame(msg)
    session.turn_persona[turn_id] = persona
    await session.supersede(turn_id)
    await safe_send_json(session.ws, frame("transcript.user", turn_id, text=text))
    session.track(
        asyncio.create_task(
            handle_turn_task(
                session.ws,
                turn_id,
                text,
                session.queue,
                session.turn_tasks,
                active_persona=persona,
                on_cancel=session.cancel_others,
            ),
            name=f"turn:{turn_id}",
        )
    )


async def _on_barge(session: Session, msg: dict[str, Any]) -> None:
    ref = msg.get("turn_id") or session.turn_id
    dropped = await session.barge(ref)
    session.turn_id = new_turn_id()
    await safe_send_json(
        session.ws,
        frame("state.listening", session.turn_id, barged_turn=ref, dropped=dropped),
    )


async def _on_user_attach(session: Session, msg: dict[str, Any]) -> None:
    """Delegate an attachment to ``server.eyes`` (lane E), per docs/WAVE3.md."""
    turn_id = msg.get("turn_id") or session.turn_id
    eyes = eyes_module()
    if eyes is None or not hasattr(eyes, "handle_attach"):
        await send_error(
            session.ws, turn_id, "eyes_disabled", "server.eyes is not installed"
        )
        return
    persona = session.turn_persona.get(turn_id) or _persona_for_frame(msg)
    await eyes.handle_attach(session.ws, msg, persona)


HANDLERS = {
    "user.start": _on_user_start,
    "user.chunk": _on_user_chunk,
    "user.stop": _on_user_stop,
    "user.text": _on_user_text,
    "user.attach": _on_user_attach,
    "barge": _on_barge,
}


# -------------------------------------------------------------- endpoint ---


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Handshake auth before accept: an unauthenticated socket can trigger a
    # turn, which dials the configured LLM base_url with the stored key.
    # Header ``X-Studio-Token``, or ``?token=`` for browsers (the WebSocket
    # API cannot set request headers). Close code 4401 = unauthorized.
    if not ws_token_ok(ws):
        log.warning("ws_rejected reason=unauthorized")
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    session = Session(ws=ws)
    try:
        await safe_send_json(ws, frame("state.idle", session.turn_id))
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except (ValueError, TypeError, UnicodeDecodeError) as e:
                # Undecodable payload: name it and keep the socket.
                await send_error(
                    ws,
                    session.turn_id,
                    swallowed("bad_frame", e),
                    "frame was not decodable JSON",
                )
                continue

            if not isinstance(msg, dict):
                await send_error(
                    ws, session.turn_id, "bad_frame", "frame is not a JSON object"
                )
                continue

            mtype = str(msg.get("type") or "")
            handler = HANDLERS.get(mtype)
            if handler is None:
                await send_error(
                    ws,
                    str(msg.get("turn_id") or session.turn_id),
                    "unknown_frame",
                    mtype or "<missing type>",
                )
                continue
            try:
                await handler(session, msg)
            except ProviderError as e:
                await send_error(
                    ws, str(msg.get("turn_id") or session.turn_id), e.reason, e.detail
                )
            except WebSocketDisconnect:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = swallowed("frame_handler_failed", e, frame_type=mtype)
                await send_error(
                    ws, str(msg.get("turn_id") or session.turn_id), reason, str(e)
                )
    except WebSocketDisconnect:
        log.debug("ws_disconnected turn_id=%s", session.turn_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        swallowed("ws_loop_failed", e, turn_id=session.turn_id)
    finally:
        await session.shutdown()
