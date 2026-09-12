"""One turn, end to end: route, control, stall, speak.

The blocking provider calls live in :mod:`server.speech` and
:mod:`server.stall`, both of which go through ``asyncio.to_thread``. STT runs
inside the turn task (not the WS reader loop), so a ``barge`` frame stays
readable while a turn is in flight and cancelling the task kills the turn
mid-transcribe.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket

from . import eyes
from .control import check_deterministic_control
from .frames import frame, safe_send_json, send_error
from .grounding import collect_grounding
from .logs import swallowed
from .persona import Persona
from .persona_runtime import build_system_prompt
from .provider_factory import ProviderSet
from . import receipts
from .providers import ProviderError, route_text
from .settings import TURNS_PATH
from .speak_queue import SpeakQueue
from .speech import (
    MAX_SENTENCE_WORDS,
    TurnResult,
    run_speech,
    set_turn_cancel_event,
    speak_sentence,
    turn_delay_s,
)
from .stall import get_or_synth_stall
from . import runtime
from .telemetry import TurnLog

log = logging.getLogger("pet_talk.server")

#: Called with the turn id to cancel; returns the true dropped count.
BargeFn = Callable[[str], Awaitable[int]]


def _drain_eyes_context() -> str:
    """Pull whatever server/eyes.py queued for THIS turn's prompt.

    ``EyesProvider.take_context()`` drains and clears its own queue, so an
    attachment's OCR text reaches exactly one turn — the very next one —
    and never a later one. Empty on every turn with no pending attachment,
    which is most turns. Never raises: a drain failure must not sink the
    turn, only drop the (already-optional) eyes context.
    """
    try:
        pending = eyes.get_provider().take_context()
    except Exception as e:
        swallowed("eyes_context_drain_failed", e)
        return ""
    if not pending:
        return ""
    return "\n".join(tag for tag, _truncated in pending)


def discard_eyes_context(reason: str, turn_id: str = "") -> int:
    """Throw away whatever eyes queued, naming why. Returns how much was lost.

    A turn a barge killed before it started never builds a prompt, so it never
    drains the queue — and the attachment the user has already moved on from
    would ride along into the NEXT turn. Silence there is the bug; a named
    line in the log is the fix.
    """
    try:
        pending = eyes.get_provider().take_context()
    except Exception as e:
        swallowed("eyes_context_drain_failed", e)
        return 0
    if pending:
        log.info(
            "eyes_context_discarded turn_id=%s reason=%s lines=%d",
            turn_id, reason, len(pending),
        )
    return len(pending)


async def handle_turn(
    ws: WebSocket,
    turn_id: str,
    text: str,
    queue: SpeakQueue,
    log_: Optional[TurnLog] = None,
    active_persona: Optional[Persona] = None,
    providers: Optional[ProviderSet] = None,
    on_cancel: Optional[BargeFn] = None,
    handover: bool = False,
    stall_sent: bool = False,
) -> TurnResult:
    """Router: deterministic control vs stall+worker vs direct answer.

    ``stall_sent`` is True when the streaming-STT path already spoke the stall
    on end-of-speech (server/stt_stream.py); the worker path then skips its own.

    ``handover`` is the Option+Shift+Tab chord's flag, consumed once by this
    turn: it adds one line to the system prompt (see
    :data:`server.persona_runtime.HANDOVER_LINE`) and changes nothing else.
    """
    providers = providers or runtime.snapshot()
    # Drained FIRST, before the empty-transcript and control returns below.
    # An attachment reaches exactly ONE turn, and this is that turn whatever
    # happens to it: the control and empty paths build no prompt, so they
    # consume the context and drop it rather than leaving it for a later,
    # unrelated question to inherit.
    eyes_context = _drain_eyes_context()
    if not text or not text.strip():
        await send_error(ws, turn_id, "empty_transcript")
        await safe_send_json(ws, frame("agent.done", turn_id, path="empty", sentences=0))
        if log_ is not None:
            log_.end(path="empty", chars=0, sentences=0)
        return TurnResult(path="empty")

    p = active_persona or runtime.default_persona()
    pname = getattr(p, "name", "") or "default"

    # 1. Deterministic control fast path (<20ms, zero LLM latency).
    control = check_deterministic_control(text, p)
    if control.matched:
        if control.cancel:
            dropped = 0
            if on_cancel is not None:
                dropped = await on_cancel(turn_id)
            else:
                dropped = await queue.flush()
            await safe_send_json(
                ws,
                frame(
                    "agent.done", turn_id, path="control_cancel", sentences=0, dropped=dropped
                ),
            )
            if log_ is not None:
                log_.end(path="control_cancel", chars=0, sentences=0, dropped=dropped)
            return TurnResult(path="control_cancel")

        result = TurnResult(path="control")
        if not await safe_send_json(ws, frame("state.speaking", turn_id)):
            return result
        spoken = await speak_sentence(
            ws, turn_id, control.reply or "", 0, p, providers.tts
        )
        if spoken is not None:
            result.sentences = 1
            result.chars = len(spoken.text)
            result.spoken.append(spoken.text)
            if log_ is not None:
                log_.mark("first_sentence")
        await receipts.emit(ws, turn_id, result.spoken, pname)
        await safe_send_json(
            ws, frame("agent.done", turn_id, path="control", sentences=result.sentences)
        )
        if log_ is not None:
            log_.mark("done")
            log_.end(path="control", chars=result.chars, sentences=result.sentences)
        runtime.memory.record_turn(turn_id, pname, text, result.spoken)
        return result

    # 2. Route the general request.
    try:
        path = providers.llm.route(text)
    except Exception as e:
        # Named, not silent: the heuristic fallback still runs, but the client
        # is told the LLM's own router failed (law 1).
        reason = swallowed("llm_route_failed", e, turn_id=turn_id)
        await send_error(ws, turn_id, reason, str(e))
        path = route_text(text)

    history = runtime.memory.get_history_messages(pname, limit=6)
    grounding = await collect_grounding()
    if eyes_context and log_ is not None:
        log_.mark("eyes_ocr_ms")
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                p, grounding, handover=handover, eyes_context=eyes_context
            ),
        },
        *history,
        {"role": "user", "content": text},
    ]

    if path == "stall":
        result = TurnResult(path="worker")
        try:
            stall_text = p.stall_for(0)
        except Exception as e:
            await send_error(ws, turn_id, "persona_no_stalls", str(e))
            stall_text = ""
        if stall_text and not stall_sent:
            stall_audio = None
            try:
                stall_audio = await get_or_synth_stall(stall_text, p, providers.tts)
            except ProviderError as e:
                await send_error(ws, turn_id, e.reason, e.detail)
            if not await safe_send_json(
                ws, frame("agent.stall", turn_id, phrase_id="stall-0", text=stall_text)
            ):
                return result
            if log_ is not None:
                log_.mark("stall")
            if not await safe_send_json(ws, frame("state.thinking", turn_id)):
                return result
            if stall_audio is not None:
                # state.speaking precedes the audio it describes, and the
                # stall's sentence frame carries word_times like every other
                # agent.sentence — a client should not need a special case.
                if not await safe_send_json(ws, frame("state.speaking", turn_id)):
                    return result
                if not await safe_send_json(
                    ws,
                    frame(
                        "agent.sentence",
                        turn_id,
                        seq=0,
                        text=stall_text,
                        audio_url=f"/audio/{stall_audio.audio_id}",
                        word_times=stall_audio.word_times,
                        estimated=True,
                    ),
                ):
                    return result
                if log_ is not None:
                    log_.mark("first_sentence")
                result.chars += len(stall_text)
                result.spoken.append(stall_text)
        if not await safe_send_json(ws, frame("state.speaking", turn_id)):
            return result
        await run_speech(
            ws, turn_id, messages, queue, p, providers, first_seq=1, result=result, log_=log_
        )
        if log_ is not None:
            log_.mark("done")
        await receipts.emit(ws, turn_id, result.spoken, pname)
        await safe_send_json(
            ws, frame("agent.done", turn_id, path="worker", sentences=result.sentences)
        )
        if log_ is not None:
            log_.end(path="worker", chars=result.chars, sentences=result.sentences)
        runtime.memory.record_turn(turn_id, pname, text, result.spoken)
        return result

    result = TurnResult(path="direct")
    if not await safe_send_json(ws, frame("state.thinking", turn_id)):
        return result
    if not await safe_send_json(ws, frame("state.speaking", turn_id)):
        return result
    await run_speech(
        ws,
        turn_id,
        messages,
        queue,
        p,
        providers,
        # seq 0 belongs to the stall, on every path. The streaming-STT lane
        # sends `agent.sentence seq=0` for its early filler and the direct
        # route can follow one (the partial routes to stall, the full
        # transcript routes direct) — so an answer starting at 0 collided with
        # it, and web/src/readAhead.ts keys the read-ahead buffer BY SEQ.
        first_seq=1,
        result=result,
        log_=log_,
        max_sentences=1,  # direct path answers once, no worker fan-out
    )
    if log_ is not None:
        log_.mark("done")
    await receipts.emit(ws, turn_id, result.spoken, pname)
    await safe_send_json(
        ws, frame("agent.done", turn_id, path="direct", sentences=result.sentences)
    )
    if log_ is not None:
        log_.end(path="direct", chars=result.chars, sentences=result.sentences)
    runtime.memory.record_turn(turn_id, pname, text, result.spoken)
    return result


async def transcribe_off_loop(
    providers: ProviderSet, audio: bytes, sample_rate: int
) -> str:
    """STT in a worker thread so the WS reader keeps consuming frames."""
    return await asyncio.to_thread(providers.stt.transcribe, audio, sample_rate=sample_rate)


async def _turn_pipeline(
    ws: WebSocket,
    turn_id: str,
    text: Optional[str],
    queue: SpeakQueue,
    log_: TurnLog,
    active_persona: Optional[Persona],
    providers: ProviderSet,
    on_cancel: Optional[BargeFn],
    audio: Optional[bytes],
    sample_rate: int,
    handover: bool = False,
    stall_sent: bool = False,
) -> None:
    """STT (if needed) then the turn. Cancellable at every await."""
    if audio is not None:
        try:
            text = await transcribe_off_loop(providers, audio, sample_rate)
        except ProviderError as e:
            await send_error(ws, turn_id, e.reason, e.detail)
            log_.end(path="stt_error", chars=0, sentences=0)
            # Every turn ends with agent.done (docs/SPEC.md 4.2). A failed STT
            # used to send agent.error and nothing else, so a client waiting on
            # agent.done stayed in `thinking` for ever. The telemetry keeps its
            # finer `stt_error`; the wire uses the catalogued `error`.
            await safe_send_json(
                ws,
                frame("agent.done", turn_id, path="error", sentences=0, reason=e.reason),
            )
            return
        except Exception as e:
            reason = swallowed("stt_failed", e, turn_id=turn_id)
            await send_error(ws, turn_id, reason, str(e))
            log_.end(path="stt_error", chars=0, sentences=0)
            await safe_send_json(
                ws,
                frame("agent.done", turn_id, path="error", sentences=0, reason=reason),
            )
            return
        log_.mark("stt")
        await safe_send_json(
            ws, frame("transcript.user", turn_id, text=text, handover=handover)
        )
    await handle_turn(
        ws,
        turn_id,
        text or "",
        queue,
        log_=log_,
        active_persona=active_persona,
        providers=providers,
        on_cancel=on_cancel,
        handover=handover,
        stall_sent=stall_sent,
    )


async def handle_turn_task(
    ws: WebSocket,
    turn_id: str,
    text: Optional[str],
    queue: SpeakQueue,
    turn_tasks: dict,
    active_persona: Optional[Persona] = None,
    providers: Optional[ProviderSet] = None,
    on_cancel: Optional[BargeFn] = None,
    audio: Optional[bytes] = None,
    sample_rate: int = 16000,
    barged: Optional[set] = None,
    handover: bool = False,
    stall_sent: bool = False,
) -> None:
    """Run one turn as a cancellable task so barge can kill it mid-flight.

    Providers are snapshotted once, here, and used for the whole turn: a
    ``POST /settings`` landing halfway cannot swap TTS out from under the
    sentence being spoken.

    ``barged`` is the socket's set of turn ids a barge has already killed. A
    barge that lands between ``create_task`` and this coroutine's first line
    has no task to cancel, so it marks the id instead — checked here before
    any await, and again before the pipeline starts, so such a turn never
    speaks. Cancellation alone cannot cover it: there is nothing running yet
    to cancel.
    """
    if barged is not None and turn_id in barged:
        barged.discard(turn_id)
        discard_eyes_context("barged_before_start", turn_id)
        log_ = TurnLog(path=TURNS_PATH)
        log_.start(turn_id, {})
        log_.end(path="interrupted", chars=0, sentences=0)
        await safe_send_json(ws, frame("agent.done", turn_id, path="interrupted"))
        return
    providers = providers or await runtime.snapshot_under_lock()
    log_ = TurnLog(path=TURNS_PATH)
    log_.start(turn_id, providers.class_names())
    # One cancellation token for the whole turn. Set before the pipeline task
    # is created so the pipeline, the LLM producer and the TTS consumer all
    # inherit it; fired below when this turn is cancelled, so a synth already
    # running in a worker thread stops polling and its result is discarded.
    cancel_token = threading.Event()
    set_turn_cancel_event(cancel_token)
    if barged is not None and turn_id in barged:
        # A barge landed while we were waiting on the swap lock.
        discard_eyes_context("barged_awaiting_swap_lock", turn_id)
        log_.end(path="interrupted", chars=0, sentences=0)
        await safe_send_json(ws, frame("agent.done", turn_id, path="interrupted"))
        return
    task = asyncio.ensure_future(
        _turn_pipeline(
            ws,
            turn_id,
            text,
            queue,
            log_,
            active_persona,
            providers,
            on_cancel,
            audio,
            sample_rate,
            handover,
            stall_sent,
        )
    )
    turn_tasks[turn_id] = task
    try:
        await task
    except asyncio.CancelledError:
        cancel_token.set()
        log_.end(path="interrupted", chars=0, sentences=0)
        await safe_send_json(ws, frame("agent.done", turn_id, path="interrupted"))
    except ProviderError as e:
        # docs/SPEC.md 4.2 lists `error` among agent.done's paths, and nothing
        # emitted it: a turn that died here sent agent.error and then silence,
        # so a client waiting on agent.done sat in `thinking` for ever.
        log_.end(path="error", chars=0, sentences=0)
        await send_error(ws, turn_id, e.reason, e.detail)
        await safe_send_json(
            ws, frame("agent.done", turn_id, path="error", sentences=0, reason=e.reason)
        )
    except Exception as e:
        log_.end(path="error", chars=0, sentences=0)
        reason = swallowed("turn_task_exception", e, turn_id=turn_id)
        await send_error(ws, turn_id, reason, str(e))
        await safe_send_json(
            ws, frame("agent.done", turn_id, path="error", sentences=0, reason=reason)
        )
    finally:
        cancel_token.set()  # nothing from this turn may speak after it ends
        turn_tasks.pop(turn_id, None)
        if barged is not None:
            barged.discard(turn_id)
