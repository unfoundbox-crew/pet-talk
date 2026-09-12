"""The WS ``/ws`` endpoint: one reader loop, many cancellable turn tasks.

The reader loop does nothing slow. STT, LLM and TTS all live inside per-turn
tasks, so a ``barge`` frame is readable at any moment — it cancels the
in-flight turn task and flushes the speak queue, and the ack carries the true
number of sentences dropped.
"""
from __future__ import annotations

import asyncio
import base64
import collections
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
#: Barged turn ids remembered at once. Bounded so a socket that barges all
#: night cannot grow the set without end; the newest ids are the ones that
#: matter, because only a turn that has not started yet can still be racing.
MAX_BARGED_IDS = 64


@dataclass
class Session:
    """Per-socket state. One speak queue PER TURN, one live turn at a time.

    The queue used to be per-socket and reused across turns via
    ``reopen()``, which raced: a new turn's ``reopen()`` could clear (and
    reset the high-water of) a queue the previous turn's producer was still
    pushing to. A turn now owns its queue outright; a barge flushes the
    queues of exactly the turns it kills.
    """

    ws: WebSocket
    turn_queues: dict[str, SpeakQueue] = field(default_factory=dict)
    turn_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    turn_persona: dict[str, Persona] = field(default_factory=dict)
    #: Turn ids a barge has killed. A barge can arrive before the turn task
    #: exists; the turn checks this set before its first await and gives up.
    barged: set[str] = field(default_factory=set)
    #: Tasks the last barge actually cancelled — the honest receipt.
    last_barge_cancelled: int = 0
    _barged_order: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=MAX_BARGED_IDS)
    )
    turn_id: str = field(default_factory=new_turn_id)
    chunks: list[bytes] = field(default_factory=list)
    #: Returned by ``queue`` when no turn is live. Never spoken through.
    _idle_queue: SpeakQueue = field(default_factory=SpeakQueue)

    @property
    def queue(self) -> SpeakQueue:
        """The newest turn's queue. Compatibility seam for older callers."""
        if self.turn_queues:
            return next(reversed(list(self.turn_queues.values())))
        return self._idle_queue

    def track(self, task: asyncio.Task) -> asyncio.Task:
        self.active_tasks.add(task)
        task.add_done_callback(self.active_tasks.discard)
        return task

    def mark_barged(self, turn_id: str) -> None:
        """Remember a killed turn id, bounded to ``MAX_BARGED_IDS``."""
        if turn_id in self.barged:
            return
        if len(self._barged_order) == self._barged_order.maxlen:
            self.barged.discard(self._barged_order[0])
        self._barged_order.append(turn_id)
        self.barged.add(turn_id)

    def start_turn(self, turn_id: str, coro_factory) -> asyncio.Task:
        """Create a turn task and register it SYNCHRONOUSLY.

        Registration used to happen inside the turn coroutine, after an
        await — so a barge arriving in that window found ``turn_tasks``
        empty and was lost. Registering here, in the same tick as
        ``create_task``, closes that window; ``barged`` closes the rest of
        it (the barge that beats the coroutine's first line).
        """
        task = asyncio.create_task(coro_factory(), name=f"turn:{turn_id}")
        self.turn_tasks[turn_id] = task

        def _reap(_t: asyncio.Task) -> None:
            # Per-turn state dies with the turn; both of these used to
            # accumulate one entry per turn for the life of the socket.
            self.turn_persona.pop(turn_id, None)
            self.turn_queues.pop(turn_id, None)

        task.add_done_callback(_reap)
        return self.track(task)

    def queue_for(self, turn_id: str) -> SpeakQueue:
        """This turn's own queue, created on first ask."""
        queue = self.turn_queues.get(turn_id)
        if queue is None:
            queue = SpeakQueue()
            self.turn_queues[turn_id] = queue
        return queue

    async def barge(self, ref: Optional[str] = None, exclude: Optional[str] = None) -> int:
        """Kill playback: cancel the live turn, then flush. Returns dropped.

        Cancelling first freezes the queue — nothing else runs on this loop
        between the cancel and the flush — so the count is exact. ``exclude``
        spares one turn, which is how a spoken "stop" cancels what is playing
        without cancelling the turn that carried the command.
        """
        targets: list[asyncio.Task] = []
        killed: list[str] = []
        if ref and ref in self.turn_tasks and ref != exclude:
            killed.append(ref)
            targets.append(self.turn_tasks.pop(ref))
        else:
            for tid in [t for t in self.turn_tasks if t != exclude]:
                killed.append(tid)
                targets.append(self.turn_tasks.pop(tid))
        for tid in killed:
            self.mark_barged(tid)
        if ref and ref != exclude:
            # Mark the id even when no task is registered yet: a turn whose
            # task has not reached its first line still has to die.
            self.mark_barged(ref)
            if ref not in killed:
                killed.append(ref)
        for task in targets:
            if not task.done():
                task.cancel()
        self.last_barge_cancelled = len(targets)
        dropped = 0
        for tid in killed:
            queue = self.turn_queues.pop(tid, None)
            if queue is not None:
                dropped += await queue.flush()
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

        Two overlapping turns would interleave sentences on one socket, so
        the newer turn wins and the older is barged.
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
        queues = list(self.turn_queues.values())
        self.turn_queues.clear()
        for queue in queues:
            try:
                await asyncio.shield(queue.flush())
            except asyncio.CancelledError:
                # Only a socket that had work in flight is worth a warning; a
                # normal close with nothing live is not an anomaly.
                if pending:
                    swallowed("ws_shutdown_flush_cancelled", None, pending=len(pending))
                else:
                    log.debug("ws_shutdown_flush_cancelled_on_idle_close")


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
    # Create the turn's queue BEFORE the task exists, so a barge landing in
    # the same tick has something to flush.
    queue = session.queue_for(turn_id)
    # Fire and forget: STT runs inside the task, so the reader loop stays free
    # for a mid-turn barge.
    session.start_turn(
        turn_id,
        lambda: handle_turn_task(
            session.ws,
            turn_id,
            None,
            queue,
            session.turn_tasks,
            active_persona=persona,
            on_cancel=session.cancel_others,
            audio=audio,
            sample_rate=sample_rate,
            barged=session.barged,
        ),
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
    queue = session.queue_for(turn_id)
    session.start_turn(
        turn_id,
        lambda: handle_turn_task(
            session.ws,
            turn_id,
            text,
            queue,
            session.turn_tasks,
            active_persona=persona,
            on_cancel=session.cancel_others,
            barged=session.barged,
        ),
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
