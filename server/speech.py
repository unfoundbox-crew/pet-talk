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
import os
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket

from .audio_store import store_audio
from .dictation import VoiceProseFormatter
from .frames import frame, safe_send_json, send_error
from .logs import log, swallowed
from .persona import Persona
from .provider_factory import ProviderSet
from .providers import ProviderError
from .speak_queue import SpeakQueue, SpokenSentence
from .telemetry import TurnLog

MAX_SENTENCE_WORDS = 20


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


