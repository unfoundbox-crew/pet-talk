"""The speech pipeline: LLM producer, TTS consumer, one sentence out.

``run_speech`` is where the SpeakQueue earns its keep. The producer pushes
every sentence the LLM streams and the consumer synthesizes and sends them in
order, as two separate tasks — so when the LLM outruns TTS the queue buffers
ahead (TECH-SPEC section 9, gate 3) instead of being drained on the push.
Every TTS call goes through ``asyncio.to_thread``, which is what lets a barge
cancel mid-synthesis.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket

from .audio_store import store_audio
from .dictation import VoiceProseFormatter
from .frames import frame, safe_send_json, send_error
from .logs import log, swallowed
from .persona import Persona
from .provider_factory import ProviderSet
from .providers import ProviderError, concat_wavs, pcm_duration_ms, shift_word_times
from .speak_queue import SpeakQueue, SpokenSentence
from .telemetry import TurnLog

MAX_SENTENCE_WORDS = 20

#: Set for the whole turn (see :func:`server.turn.handle_turn_task`) and
#: fired when that turn is cancelled or barged. A provider call already
#: running in a worker thread cannot be cancelled by asyncio, so the thread
#: reads this event to stop polling early and the wrapper discards whatever
#: a doomed call still returns. Tasks created inside the turn inherit it.
TURN_CANCEL: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar(
    "pet_talk_turn_cancel", default=None
)


def turn_cancel_event() -> Optional[threading.Event]:
    return TURN_CANCEL.get()


def set_turn_cancel_event(event: Optional[threading.Event]) -> None:
    TURN_CANCEL.set(event)


async def synth_off_thread(
    tts: object, text: str, voice: str, speed: float
) -> tuple[bytes, list]:
    """``tts.synth`` in a worker thread, with the turn's cancellation token.

    Raises ``ProviderError('turn_cancelled')`` when the turn died before the
    call started or while it was running — the audio of a barged turn is
    never stored and never sent. Backends that advertise ``supports_cancel``
    also get the event, so their poll/sleep loops exit early instead of
    holding a thread for the full timeout.
    """
    cancel = TURN_CANCEL.get()
    if cancel is not None and cancel.is_set():
        raise ProviderError("turn_cancelled", "synth skipped: turn already barged")
    if cancel is not None and getattr(tts, "supports_cancel", False):
        wav, word_times = await asyncio.to_thread(
            tts.synth, text, voice=voice, speed=speed, cancel=cancel
        )
    else:
        wav, word_times = await asyncio.to_thread(
            tts.synth, text, voice=voice, speed=speed
        )
    if cancel is not None and cancel.is_set():
        raise ProviderError("turn_cancelled", "synth result discarded: turn barged")
    return wav, word_times


def chunk_streaming_enabled() -> bool:
    """``PET_TALK_TTS_CHUNKS=0`` forces the whole-sentence path.

    Default on. The off switch exists so a client that cannot consume
    ``agent.chunk`` can be served by config rather than by a code change, and
    so a chunking regression can be bisected without a revert.
    """
    return os.environ.get("PET_TALK_TTS_CHUNKS", "1") != "0"


async def stream_chunks(
    ws: WebSocket,
    turn_id: str,
    seq: int,
    text: str,
    p: Persona,
    tts: object,
    log_: Optional[TurnLog] = None,
) -> tuple[bytes, list, str]:
    """Synthesize ``text`` clause by clause, sending one ``agent.chunk`` per
    clause as it is produced, and return the joined ``(wav, word_times)``.

    This is the whole point of the lane: the listener hears the first clause
    while the rest of the sentence is still being synthesized, so what has to
    fit inside ``tts_ms`` is one clause rather than one sentence. The chunks
    are also stored individually so a client that cannot decode base64 can
    fetch ``stream_url`` and play chunk 0 immediately.

    Raises ``ProviderError`` on a failed chunk (the caller names it) and
    ``ProviderError('ws_send_failed')`` when the socket dies mid-stream — a
    half-sent stream is never completed as if it arrived.
    """
    import base64

    cancel = TURN_CANCEL.get()
    kwargs = {"cancel": cancel} if cancel is not None else {}
    chunks: list[bytes] = []
    word_times: list = []
    offset_ms = 0
    chunk_no = 0
    first_url = ""
    async for wav_chunk, chunk_times, final in tts.synth_chunks(  # type: ignore[attr-defined]
        text, p.voice, p.speed, **kwargs
    ):
        if cancel is not None and cancel.is_set():
            raise ProviderError("turn_cancelled", "chunk discarded: turn barged")
        if not wav_chunk:
            raise ProviderError("tts_empty_audio", f"chunk {chunk_no} was empty")
        chunk_id = f"{turn_id}-s{seq}-c{chunk_no}"
        store_audio(chunk_id, wav_chunk)
        url = f"/audio/{chunk_id}"
        if chunk_no == 0:
            first_url = url
        sent = await safe_send_json(
            ws,
            frame(
                "agent.chunk",
                turn_id,
                seq=seq,
                chunk_no=chunk_no,
                audio_b64=base64.b64encode(wav_chunk).decode("ascii"),
                url=url,
                final=bool(final),
            ),
        )
        if not sent:
            raise ProviderError("ws_send_failed", f"chunk {chunk_no} of seq {seq}")
        if chunk_no == 0 and log_ is not None:
            log_.mark("first_chunk")
        chunks.append(wav_chunk)
        word_times.extend(shift_word_times(chunk_times, offset_ms))
        offset_ms += pcm_duration_ms(wav_chunk)
        chunk_no += 1
    if not chunks:
        raise ProviderError("tts_empty_audio", "synth_chunks yielded nothing")
    return concat_wavs(chunks), word_times, first_url


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
    log_: Optional[TurnLog] = None,
) -> Optional[SpokenSentence]:
    """Synthesize one sentence off the loop and send ``agent.sentence``.

    Two paths, and which one ran is always on the record:

    * **Chunked** — the backend defines ``synth_chunks`` and
      ``PET_TALK_TTS_CHUNKS`` is not 0. One ``agent.chunk`` goes out per clause
      as it is synthesized, so the first audio is audible before the sentence
      is finished, then ``agent.sentence`` follows with ``chunked=true`` and
      ``stream_url`` pointing at chunk 0.
    * **Whole sentence** — everything else. ``chunked=false``,
      ``stream_url=null``, and the reason the stream was not used
      (``tts_no_chunk_support`` or ``tts_chunking_disabled``) is logged. Never
      a silent fallback (AGENTS.md law 1).

    ``audio_url`` carries the complete sentence on both paths, so a client
    written against the old contract keeps working (docs/SPEC.md 4.2).

    Returns the spoken sentence (with word times) or None when the sentence
    could not be synthesized or delivered.
    """
    clean_text = VoiceProseFormatter.sanitize(sentence, max_words=MAX_SENTENCE_WORDS)
    if not clean_text:
        clean_text = sentence.strip()
    if not clean_text:
        await send_error(ws, turn_id, "tts_empty_text", f"seq={seq}", seq=seq)
        return None

    chunker = getattr(tts, "synth_chunks", None)
    if chunker is None:
        chunk_reason = "tts_no_chunk_support"
    elif not chunk_streaming_enabled():
        chunk_reason = "tts_chunking_disabled"
    else:
        chunk_reason = ""
    if chunk_reason:
        log.info(
            "tts_whole_sentence turn_id=%s seq=%d reason=%s backend=%s",
            turn_id,
            seq,
            chunk_reason,
            type(tts).__name__,
        )

    stream_url: Optional[str] = None
    try:
        if chunk_reason:
            wav, word_times = await synth_off_thread(tts, clean_text, p.voice, p.speed)
        else:
            wav, word_times, first_chunk_url = await stream_chunks(
                ws, turn_id, seq, clean_text, p, tts, log_=log_
            )
            stream_url = first_chunk_url or None
    except ProviderError as e:
        if e.reason in ("turn_cancelled", "tts_cancelled"):
            # Expected on a barge: the client already has the ack. Do not add
            # an error frame for audio nobody is waiting for.
            log.debug("synth_discarded turn_id=%s seq=%d reason=%s", turn_id, seq, e.reason)
            return None
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
            stream_url=stream_url,
            chunked=stream_url is not None,
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

    Fails closed: zero sentences with no other named reason sends
    ``agent.error reason=llm_no_sentences`` before the caller's
    ``agent.done``, so an empty stream is never reported as a good turn.
    """
    await queue.reopen()
    producer_error: Optional[ProviderError] = None
    consumer_error = False

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
        nonlocal consumer_error
        seq = first_seq
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            spoken = await speak_sentence(
                ws, turn_id, sentence, seq, p, providers.tts, queue=queue, log_=log_
            )
            if spoken is None:
                consumer_error = True  # speak_sentence already named the reason
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
    elif result.sentences == 0 and not consumer_error:
        # Law 1: a stream that yielded nothing is a failure, not a quiet
        # success. Without this the client saw agent.done sentences=0 and no
        # reason — silence reported as a good turn.
        await send_error(
            ws,
            turn_id,
            "llm_no_sentences",
            "the LLM stream produced no sentences",
        )

    stats = await queue.stats()
    log.debug(
        "turn_speech turn_id=%s spoken=%d buffered_high_water=%d",
        turn_id,
        result.sentences,
        stats["high_water"],
    )


