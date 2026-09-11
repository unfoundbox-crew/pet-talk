"""One turn, end to end: route, stall, stream, speak.

Everything blocking (STT, TTS) runs in ``asyncio.to_thread`` so the WS reader
never stalls and a ``barge`` frame can land mid-turn. The LLM producer and the
TTS consumer are separate tasks joined by a
:class:`~server.speak_queue.SpeakQueue`, so the queue actually buffers ahead
instead of being drained on the push.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket

from .audio_store import store_audio
from .control import check_deterministic_control
from .dictation import VoiceProseFormatter
from .frames import frame, safe_send_json, send_error
from .grounding import collect_grounding
from .logs import log, swallowed
from .persona import Persona
from .persona_runtime import build_system_prompt
from .providers import ProviderError, route_text
from .provider_factory import ProviderSet
from .settings import TURNS_PATH
from .speak_queue import SpeakQueue, SpokenSentence
from .stall import get_or_synth_stall
from . import runtime
from .telemetry import TurnLog

MAX_SENTENCE_WORDS = 20

#: Called with the turn id to cancel; returns the true dropped count.
BargeFn = Callable[[str], Awaitable[int]]


def turn_delay_s() -> float:
    """Test hook: stretch worker turns so barge-kill is observable. 0 in prod."""
    raw = os.environ.get("PET_TALK_TURN_DELAY_MS", "0")
    try:
        return max(0.0, float(raw)) / 1000.0
    except ValueError:
        swallowed("bad_turn_delay_env", None, value=raw)
        return 0.0


@dataclass
class TurnResult:
    path: str
    sentences: int = 0
    chars: int = 0
    spoken: list[str] = field(default_factory=list)


async def speak_sentence(
    ws: WebSocket,
    turn_id: str,
    sentence: str,
    seq: int,
    p: Persona,
    tts: object,
    queue: Optional[SpeakQueue] = None,
) -> Optional[SpokenSentence]:
    """Synthesize one sentence off the loop and send ``agent.sentence``.

    Returns the spoken sentence (with word times) or None when the sentence
    could not be synthesized or delivered.
    """
    clean_text = VoiceProseFormatter.sanitize(sentence, max_words=MAX_SENTENCE_WORDS)
    if not clean_text:
        clean_text = sentence.strip()
    if not clean_text:
        await send_error(ws, turn_id, "tts_empty_text", f"seq={seq}", seq=seq)
        return None
    try:
        wav, word_times = await asyncio.to_thread(
            tts.synth, clean_text, voice=p.voice, speed=p.speed
        )
    except ProviderError as e:
        await send_error(ws, turn_id, e.reason, e.detail, seq=seq)
        return None
    except Exception as e:
        await send_error(ws, turn_id, swallowed("tts_synth_failed", e, seq=seq), str(e), seq=seq)
        return None

    spoken = SpokenSentence.from_synth(seq, clean_text, word_times)
    audio_id = f"{turn_id}-s{seq}"
    store_audio(audio_id, wav)
    sent = await safe_send_json(
        ws,
        frame(
            "agent.sentence",
            turn_id,
            seq=seq,
            text=clean_text,
            audio_url=f"/audio/{audio_id}",
            word_times=spoken.word_times,
            estimated=spoken.estimated,
        ),
    )
    if not sent:
        return None
    if queue is not None:
        await queue.done_with_inflight(spoken)
    return spoken


async def run_speech(
    ws: WebSocket,
    turn_id: str,
    messages: list[dict],
    queue: SpeakQueue,
    p: Persona,
    providers: ProviderSet,
    first_seq: int,
    result: TurnResult,
    log_: Optional[TurnLog] = None,
    max_sentences: int = 0,
) -> None:
    """Run the LLM producer and the TTS consumer concurrently.

    The producer pushes every sentence the LLM streams; the consumer
    synthesizes and sends them in order. When the LLM is faster than TTS the
    queue buffers ahead — ``queue.high_water`` is the receipt.
    """
    await queue.reopen()
    producer_error: Optional[ProviderError] = None

    async def _produce() -> None:
        nonlocal producer_error
        pushed = 0
        try:
            async for sentence in providers.llm.stream(messages):
                if not str(sentence or "").strip():
                    continue
                await queue.push(sentence)
                pushed += 1
                if max_sentences and pushed >= max_sentences:
                    break
        except ProviderError as e:
            if e.reason != "queue_closed":  # barge already tore the turn down
                producer_error = e
        except asyncio.CancelledError:
            raise
        except Exception as e:
            producer_error = ProviderError(swallowed("llm_stream_failed", e), str(e))
        finally:
            try:
                await queue.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover
                swallowed("queue_close_failed", e)

    async def _consume() -> None:
        seq = first_seq
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            spoken = await speak_sentence(
                ws, turn_id, sentence, seq, p, providers.tts, queue=queue
            )
            if spoken is None:
                await queue.done_with_inflight()
                return
            result.sentences += 1
            result.chars += len(spoken.text)
            result.spoken.append(spoken.text)
            if log_ is not None and result.sentences == 1 and first_seq == 0:
                log_.mark("first_sentence")
            seq += 1
            delay = turn_delay_s()
            if delay:
                await asyncio.sleep(delay)

    producer = asyncio.create_task(_produce(), name=f"llm-producer:{turn_id}")
    consumer = asyncio.create_task(_consume(), name=f"tts-consumer:{turn_id}")
    try:
        await consumer
    finally:
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, consumer, return_exceptions=True)

    if producer_error is not None:
        await send_error(ws, turn_id, producer_error.reason, producer_error.detail)

    stats = await queue.stats()
    log.debug(
        "turn_speech turn_id=%s spoken=%d buffered_high_water=%d",
        turn_id,
        result.sentences,
        stats["high_water"],
    )


async def handle_turn(
    ws: WebSocket,
    turn_id: str,
    text: str,
    queue: SpeakQueue,
    log_: Optional[TurnLog] = None,
    active_persona: Optional[Persona] = None,
    providers: Optional[ProviderSet] = None,
    on_cancel: Optional[BargeFn] = None,
) -> TurnResult:
    """Router: deterministic control vs stall+worker vs direct answer."""
    providers = providers or runtime.snapshot()
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
        swallowed("llm_route_failed", e, turn_id=turn_id)
        path = route_text(text)

    history = runtime.memory.get_history_messages(pname, limit=6)
    grounding = await collect_grounding()
    messages = [
        {"role": "system", "content": build_system_prompt(p, grounding)},
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
        if stall_text:
            try:
                audio_id, _wav = await get_or_synth_stall(stall_text, p, providers.tts)
            except ProviderError as e:
                await send_error(ws, turn_id, e.reason, e.detail)
                audio_id = ""
            if not await safe_send_json(
                ws, frame("agent.stall", turn_id, phrase_id="stall-0", text=stall_text)
            ):
                return result
            if log_ is not None:
                log_.mark("stall")
            if not await safe_send_json(ws, frame("state.thinking", turn_id)):
                return result
            if audio_id:
                if not await safe_send_json(
                    ws,
                    frame(
                        "agent.sentence",
                        turn_id,
                        seq=0,
                        text=stall_text,
                        audio_url=f"/audio/{audio_id}",
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
        first_seq=0,
        result=result,
        log_=log_,
        max_sentences=1,  # direct path answers once, no worker fan-out
    )
    if log_ is not None:
        log_.mark("done")
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
) -> None:
    """STT (if needed) then the turn. Cancellable at every await."""
    if audio is not None:
        try:
            text = await transcribe_off_loop(providers, audio, sample_rate)
        except ProviderError as e:
            await send_error(ws, turn_id, e.reason, e.detail)
            log_.end(path="stt_error", chars=0, sentences=0)
            return
        except Exception as e:
            reason = swallowed("stt_failed", e, turn_id=turn_id)
            await send_error(ws, turn_id, reason, str(e))
            log_.end(path="stt_error", chars=0, sentences=0)
            return
        log_.mark("stt")
        await safe_send_json(ws, frame("transcript.user", turn_id, text=text))
    await handle_turn(
        ws,
        turn_id,
        text or "",
        queue,
        log_=log_,
        active_persona=active_persona,
        providers=providers,
        on_cancel=on_cancel,
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
) -> None:
    """Run one turn as a cancellable task so barge can kill it mid-flight.

    Providers are snapshotted once, here, and used for the whole turn: a
    ``POST /settings`` landing halfway cannot swap TTS out from under the
    sentence being spoken.
    """
    providers = providers or await runtime.snapshot_under_lock()
    log_ = TurnLog(path=TURNS_PATH)
    log_.start(turn_id, providers.class_names())
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
        )
    )
    turn_tasks[turn_id] = task
    try:
        await task
    except asyncio.CancelledError:
        log_.end(path="interrupted", chars=0, sentences=0)
        await safe_send_json(ws, frame("agent.done", turn_id, path="interrupted"))
    except ProviderError as e:
        log_.end(path="error", chars=0, sentences=0)
        await send_error(ws, turn_id, e.reason, e.detail)
    except Exception as e:
        log_.end(path="error", chars=0, sentences=0)
        reason = swallowed("turn_task_exception", e, turn_id=turn_id)
        await send_error(ws, turn_id, reason, str(e))
    finally:
        turn_tasks.pop(turn_id, None)
