"""Startup warm for the STT tyre.

The stall cache and the Kokoro TTS model were both pre-warmed at startup; STT
was not, so `faster-whisper` loaded its weights on the first real call and the
first turn after a boot paid for it. Measured on this machine 2026-09-12:
first boot turn stalled 1264ms, against 317-383ms for every warm turn after
it. Nothing about that turn was different except the model load.

So: one short transcription at startup, off-thread, through whatever STT the
runtime actually has. Half a second of digital silence is enough — the work
being bought is the model load and the first decode pass, not a result. A
silent buffer usually comes back as `stt_empty_result`, which is exactly the
expected outcome and is logged as a success, because the load is what we came
for.

Three rules this obeys, all of them the same ones `warm_stall_cache` obeys:

* **Never raises.** A missing model, an absent key, an unreachable daemon —
  every one of them logs a named reason and returns. A cold cache is a slow
  first turn; a boot that dies because STT was not ready is a dead product.
* **Never blocks the accept loop.** The caller schedules this as a task and
  does not await it, so the WS port answers immediately.
* **Swappable, like every capability layer.** This calls `transcribe` on the
  provider ABC. It knows nothing about faster-whisper, and warming a stub or
  a cloud tyre is a cheap no-op rather than a special case.

``PET_TALK_STT_WARM=0`` turns it off and says so in the log.
"""
from __future__ import annotations

import asyncio
import os
import time

from typing import Optional

from .logs import log, swallowed
from .providers import ProviderError

#: 16kHz mono int16 — whisper's native rate and the wire protocol's default,
#: so no tyre has to resample the warm buffer.
WARM_SAMPLE_RATE = 16000

#: Long enough that a real engine runs a genuine decode pass, short enough
#: that a cloud tyre's warm costs one small request. 0.5s = 16000 bytes.
WARM_SECONDS = 0.5


def stt_warm_enabled() -> bool:
    """``PET_TALK_STT_WARM=0`` skips the startup transcription, loudly."""
    return os.environ.get("PET_TALK_STT_WARM", "1") != "0"


def silent_pcm16(
    seconds: float = WARM_SECONDS, sample_rate: int = WARM_SAMPLE_RATE
) -> bytes:
    """``seconds`` of 16-bit mono silence — zeroed samples, no numpy needed."""
    frames = max(1, int(seconds * sample_rate))
    return b"\x00\x00" * frames


async def warm_stt(stt: object) -> Optional[float]:
    """Run one throwaway transcription so the first real turn does not load the
    model. Returns the elapsed milliseconds, or ``None`` when it was skipped.

    Never raises.
    """
    if not stt_warm_enabled():
        log.info("stt_warm_disabled reason=PET_TALK_STT_WARM=0 backend=%s", type(stt).__name__)
        return None
    transcribe = getattr(stt, "transcribe", None)
    if not callable(transcribe):
        log.info("stt_warm_skipped reason=backend_has_no_transcribe backend=%s", type(stt).__name__)
        return None

    pcm = silent_pcm16()
    t0 = time.perf_counter()
    try:
        await asyncio.to_thread(transcribe, pcm, sample_rate=WARM_SAMPLE_RATE)
    except ProviderError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # `stt_empty_result` on silence is the expected path, not a failure:
        # the model loaded, decoded, and correctly found no words in silence.
        if e.reason in ("stt_empty_result", "stt_empty_audio"):
            log.info(
                "stt_warm_ms=%.1f backend=%s result=no_speech reason=%s",
                elapsed_ms,
                type(stt).__name__,
                e.reason,
            )
            return elapsed_ms
        log.info(
            "stt_warm_failed backend=%s stt_warm_ms=%.1f reason=%s detail=%s",
            type(stt).__name__,
            elapsed_ms,
            e.reason,
            e.detail,
        )
        return elapsed_ms
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        swallowed(
            "stt_warm_failed",
            e,
            backend=type(stt).__name__,
            stt_warm_ms=round(elapsed_ms, 1),
        )
        return elapsed_ms

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log.info(
        "stt_warm_ms=%.1f backend=%s result=text",
        elapsed_ms,
        type(stt).__name__,
    )
    return elapsed_ms
