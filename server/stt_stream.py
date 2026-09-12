"""Streaming STT: transcribe while the user is still speaking.

Why this module exists, in one measurement. 2026-09-12d read ``stall_ms``
p50 **642.2ms** against a **400ms** budget, and the turn's own telemetry
named the whole breach: ``stt 590.0ms``, ``stall 639.3ms`` — the stall landed
49.3ms after STT finished, so the warm stall cache was already doing its job
and the 590ms was one ``faster-whisper`` pass over the **whole utterance**,
started only once ``user.stop`` arrived. The same pass measures 143-164ms in
isolation, so no faster box fixes this: the shape is wrong. Transcribe while
the person is still talking, and end-of-speech has almost nothing left to do.

The shape here
--------------

``user.chunk`` frames already arrive every ~10ms from ``cli/client.py`` and
``web/src/ws.ts``. :class:`SttStreamSession` accumulates them and, every
``PET_TALK_STT_STREAM_MS`` of NEW audio, decodes the prefix accumulated so
far in a worker thread (``asyncio.to_thread``, so the WS reader loop stays
free for a barge). The newest completed decode is the **partial**.

On ``user.stop``:

1. the partial is already computed — reading it is free;
2. ``agent.stall`` goes out from the partial (the router decides on the
   partial), so the person hears the filler ~immediately;
3. only then does the transcript finalize — **one decode of the tail**, the
   audio that arrived after the partial's cut, appended to the partial;
4. ``transcript.user`` follows with ``final: true``.

Two honest costs, named rather than hidden:

* **A prefix/tail seam.** The tail decode starts mid-utterance, so a word cut
  in half by the seam can come out wrong in a way the single whole-utterance
  pass would have got right. ``PET_TALK_STT_FINAL_FULL=1`` buys the old
  accuracy back at the old latency (it re-decodes everything at finalize).
* **Repeated work.** Decoding the prefix every window means the same audio is
  decoded several times. That is CPU spent *during* speech, when nobody is
  waiting, to move work off the moment when somebody is.

Provider support is a flag, not a guess: ``STTProvider.supports_streaming``.
A provider that pays an HTTP round trip per window (Deepgram, Groq, OpenAI,
SenseVoice) says ``False`` — repeated whole-prefix uploads would be slower
AND more expensive. Deepgram's real streaming socket is a separate job.

Env
---

====================================  =======  ====================================
``PET_TALK_STT_STREAM``               ``0``    ``1`` turns streaming on
``PET_TALK_STT_STREAM_MS``            ``700``  decode every N ms of new audio
``PET_TALK_STT_PARTIALS``             ``0``    ``1`` sends ``transcript.user`` partials
``PET_TALK_STT_STREAM_WAIT_MS``      ``1200``  bounded wait for an in-flight decode
``PET_TALK_STT_STREAM_MAX_S``         ``30``   prefix cap (whisper's own window)
``PET_TALK_STT_FINAL_FULL``           ``0``    ``1`` finalizes on the whole utterance
====================================  =======  ====================================
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Optional

from .frames import frame, safe_send_json
from .logs import log, swallowed
from .persona import Persona
from .providers import ProviderError

#: 2 bytes per sample, mono. Every byte<->ms conversion here goes through this.
BYTES_PER_SAMPLE = 2

DEFAULT_STREAM_MS = 700
#: How long finalize waits for an in-flight prefix decode before giving up and
#: decoding the tail anyway. Generous on purpose: the stall has ALREADY gone
#: out by then, so this wait delays only the transcript, and waiting is much
#: cheaper than running a second decode on top of the first — measured
#: 2026-09-12, two concurrent faster-whisper decodes at load 22 produced a
#: 2534ms outlier where waiting produces none.
DEFAULT_WAIT_MS = 1200
DEFAULT_MAX_PREFIX_S = 30


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        swallowed("bad_stt_stream_env", None, var=name, value=raw)
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def streaming_enabled() -> bool:
    """``PET_TALK_STT_STREAM=1``. Off by default until a measurement earns it."""
    return _env_flag("PET_TALK_STT_STREAM", False)


def stream_window_ms() -> int:
    return _env_int("PET_TALK_STT_STREAM_MS", DEFAULT_STREAM_MS)


def partials_enabled() -> bool:
    return _env_flag("PET_TALK_STT_PARTIALS", False)


def final_full_pass() -> bool:
    return _env_flag("PET_TALK_STT_FINAL_FULL", False)


def inflight_wait_ms() -> int:
    return _env_int("PET_TALK_STT_STREAM_WAIT_MS", DEFAULT_WAIT_MS, minimum=0)


def max_prefix_s() -> int:
    return _env_int("PET_TALK_STT_STREAM_MAX_S", DEFAULT_MAX_PREFIX_S)


def provider_supports_streaming(provider: Any) -> bool:
    """Read the provider's own flag. Absent flag means no, never a guess."""
    return bool(getattr(provider, "supports_streaming", False))


def streaming_available(provider: Any) -> tuple[bool, str]:
    """``(usable, reason)``. The reason is always named, never empty.

    A ``PET_TALK_STT_STREAM=1`` that cannot run says so out loud — a silent
    fall-back to the whole-utterance path would let a measurement claim a
    streaming number for a pass that never streamed.
    """
    if not streaming_enabled():
        return False, "stt_stream_disabled"
    if not provider_supports_streaming(provider):
        return False, "stt_stream_unsupported_provider"
    return True, "stt_stream_on"


# ----------------------------------------------------------------- session ---


class SttStreamSession:
    """One turn's rolling transcription. Not reusable across turns.

    Owns no event loop state beyond the single in-flight decode task, so a
    cancelled turn drops it by letting the session go out of scope after
    :meth:`cancel`.
    """

    def __init__(
        self,
        provider: Any,
        turn_id: str,
        sample_rate: int = 16000,
        window_ms: Optional[int] = None,
        on_partial: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.provider = provider
        self.turn_id = turn_id
        self.sample_rate = max(1, int(sample_rate))
        self.window_ms = int(window_ms if window_ms is not None else stream_window_ms())
        self.on_partial = on_partial
        self._buf = bytearray()
        #: Text of the newest COMPLETED decode, and the byte offset it covers.
        self._partial = ""
        self._partial_offset = 0
        #: Offset the in-flight decode will cover when it lands.
        self._inflight_offset = 0
        self._task: Optional[asyncio.Task] = None
        self._cancelled = False
        self._cancel_reason = ""
        #: Every completed decode's wall time, so the real-engine test can
        #: report what streaming actually cost rather than estimating it.
        self.decode_ms: list[float] = []
        self.partials: list[str] = []

    # -- geometry ----------------------------------------------------------

    def _window_bytes(self) -> int:
        return max(
            BYTES_PER_SAMPLE,
            int(self.sample_rate * BYTES_PER_SAMPLE * self.window_ms / 1000.0),
        )

    def _max_bytes(self) -> int:
        return self.sample_rate * BYTES_PER_SAMPLE * max_prefix_s()

    def ms_of(self, n_bytes: int) -> float:
        return n_bytes * 1000.0 / (self.sample_rate * BYTES_PER_SAMPLE)

    @property
    def audio(self) -> bytes:
        return bytes(self._buf)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def latest_partial(self) -> str:
        """The newest completed decode. Free — already computed."""
        return self._partial

    def partial_offset(self) -> int:
        return self._partial_offset

    def tail_ms(self) -> float:
        return self.ms_of(max(0, len(self._buf) - self._partial_offset))

    # -- feeding -----------------------------------------------------------

    async def feed(self, pcm: bytes) -> None:
        """Append a ``user.chunk``'s PCM; start a decode when a window fills.

        Returns as soon as the decode is *scheduled*, never when it finishes:
        this runs on the WS reader loop, which must stay free for a barge.
        """
        if self._cancelled or not pcm:
            return
        self._buf.extend(pcm)
        if self._task is not None and not self._task.done():
            return  # one decode at a time; the next window will catch up
        if len(self._buf) - self._inflight_offset < self._window_bytes():
            return
        self._schedule_decode()

    def _schedule_decode(self) -> None:
        cut = len(self._buf)
        start = max(0, cut - self._max_bytes())
        prefix = bytes(self._buf[start:cut])
        self._inflight_offset = cut
        self._task = asyncio.ensure_future(self._decode(prefix, cut))

    async def _decode(self, prefix: bytes, cut: int) -> None:
        t0 = time.monotonic()
        try:
            text = await asyncio.to_thread(
                self.provider.transcribe, prefix, sample_rate=self.sample_rate
            )
        except ProviderError as e:
            # A partial is an optimization. Losing one is a logged reason, not
            # a turn failure — the finalize pass still produces the transcript.
            swallowed("stt_partial_failed", None, turn_id=self.turn_id, reason=e.reason)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            swallowed("stt_partial_failed", e, turn_id=self.turn_id)
            return
        dt = (time.monotonic() - t0) * 1000.0
        self.decode_ms.append(dt)
        if self._cancelled:
            return
        text = (text or "").strip()
        if not text:
            return
        # A later decode may already have landed (it cannot — one at a time —
        # but a future change could); never move the partial backwards.
        if cut < self._partial_offset:
            return
        self._partial = text
        self._partial_offset = cut
        self.partials.append(text)
        log.debug(
            "stt_partial turn_id=%s ms=%.1f covered_ms=%.0f chars=%d",
            self.turn_id, dt, self.ms_of(cut), len(text),
        )
        if self.on_partial is not None:
            try:
                result = self.on_partial(text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                swallowed("stt_partial_callback_failed", e, turn_id=self.turn_id)

    # -- finishing ---------------------------------------------------------

    async def finalize(self, audio: Optional[bytes] = None) -> str:
        """The turn's transcript. Decodes the TAIL only, by default.

        ``audio`` overrides the accumulated buffer — ``user.stop`` may carry
        the client's own merged ``pcm_b64``, which wins by the existing wire
        contract. Only its tail is decoded, on the same seam.

        Raises :class:`ProviderError` when the turn was barged, so the caller
        never starts a turn for audio nobody is waiting on any more.
        """
        if audio is not None:
            self._buf = bytearray(audio)
        if self._cancelled:
            raise ProviderError("stt_stream_cancelled", self._cancel_reason or "barged")
        full = bytes(self._buf)
        if not full:
            raise ProviderError("stt_empty_audio", "no audio in this turn")

        if final_full_pass():
            return await self._decode_now(full, "final_full")

        # An in-flight decode is already paying for audio the tail would pay
        # for again — and running the tail decode beside it puts two
        # faster-whisper decodes on the same CPU, which is the contention this
        # lane exists to stop. Wait for it, bounded. The stall is already out,
        # so this costs the transcript a little and the listener nothing.
        if self._task is not None and not self._task.done():
            wait_s = inflight_wait_ms() / 1000.0
            if wait_s > 0:
                done, _ = await asyncio.wait({self._task}, timeout=wait_s)
                if not done:
                    log.info(
                        "stt_stream_inflight_wait_expired turn_id=%s after_ms=%d",
                        self.turn_id, inflight_wait_ms(),
                    )
        if self._cancelled:
            raise ProviderError("stt_stream_cancelled", self._cancel_reason or "barged")

        prefix_text = self._partial
        tail = full[self._partial_offset :]
        if not prefix_text:
            # Nothing streamed (no chunks, or every partial failed). This is
            # the old whole-utterance path, reached by name, not by accident.
            log.info(
                "stt_stream_no_partial turn_id=%s reason=stt_stream_no_partial",
                self.turn_id,
            )
            return await self._decode_now(full, "final_whole")
        if not tail:
            return prefix_text
        try:
            tail_text = await self._decode_now(tail, "final_tail")
        except ProviderError as e:
            if e.reason == "stt_empty_result":
                # Whisper heard nothing in the tail (trailing silence is the
                # common case — the VAD's own hangover). The partial stands.
                return prefix_text
            raise
        return _join_seam(prefix_text, tail_text)

    async def _decode_now(self, audio: bytes, what: str) -> str:
        t0 = time.monotonic()
        text = await asyncio.to_thread(
            self.provider.transcribe, audio, sample_rate=self.sample_rate
        )
        log.debug(
            "stt_%s turn_id=%s ms=%.1f audio_ms=%.0f",
            what, self.turn_id, (time.monotonic() - t0) * 1000.0, self.ms_of(len(audio)),
        )
        return (text or "").strip()

    def cancel(self, reason: str = "barged") -> None:
        """Drop this session. A decode already in a thread is left to finish
        and its result discarded — a worker thread cannot be interrupted, and
        pretending otherwise would be the lie, not the leak."""
        self._cancelled = True
        self._cancel_reason = reason
        if self._task is not None and not self._task.done():
            self._task.cancel()
        log.info("stt_stream_cancelled turn_id=%s reason=%s", self.turn_id, reason)


def _join_seam(prefix: str, tail: str) -> str:
    """Join a prefix decode to a tail decode across the seam.

    Whisper often re-emits the last word or two of context at the start of a
    fresh decode, so a naive concatenation stutters ("what is the the
    weather"). Drop the longest word-suffix of ``prefix`` that the tail
    repeats, up to four words — beyond that a genuine repetition is likelier
    than a seam artifact.
    """
    if not prefix:
        return tail
    if not tail:
        return prefix
    pw = prefix.split()
    tw = tail.split()
    for n in range(min(4, len(pw), len(tw)), 0, -1):
        if [w.strip(".,!?").lower() for w in pw[-n:]] == [
            w.strip(".,!?").lower() for w in tw[:n]
        ]:
            tw = tw[n:]
            break
    joined = " ".join(pw + tw).strip()
    return joined


# ------------------------------------------------------------ wire helpers ---


async def send_partial_transcript(ws: Any, turn_id: str, text: str) -> None:
    """``transcript.user`` with ``partial: true``. Off unless
    ``PET_TALK_STT_PARTIALS=1`` — a client that renders every partial flickers,
    so this is opt-in for the ones that want live captions."""
    await safe_send_json(
        ws, frame("transcript.user", turn_id, text=text, partial=True, final=False)
    )


async def send_final_transcript(
    ws: Any, turn_id: str, text: str, handover: bool = False
) -> bool:
    return await safe_send_json(
        ws,
        frame(
            "transcript.user",
            turn_id,
            text=text,
            handover=handover,
            partial=False,
            final=True,
        ),
    )


def routes_to_stall(llm: Any, text: str) -> bool:
    """The router's own verdict on the partial. Never raises — a router that
    blows up here must not cost the turn its stall."""
    from .providers import route_text

    try:
        return llm.route(text) == "stall"
    except Exception as e:
        swallowed("stt_stream_route_failed", e)
        return route_text(text) == "stall"


async def emit_early_stall(
    ws: Any,
    turn_id: str,
    partial: str,
    persona: Persona,
    providers: Any,
) -> bool:
    """``agent.stall`` (with its audio) from the partial, before the final
    transcript exists. Returns whether the stall actually went out.

    This is the whole point of the lane: the frames below are the same ones
    ``turn.py``'s worker path sends, sent from end-of-speech instead of from
    end-of-transcription. ``seq=0`` is the stall's, always: BOTH of
    ``turn.py``'s answer paths start at ``first_seq=1``, so the answer's
    sentences continue the stall's numbering instead of overwriting it in the
    client's read-ahead buffer, which keys on seq.
    """
    from .stall import get_or_synth_stall

    partial = (partial or "").strip()
    if not partial:
        log.info("stt_stream_early_stall_skipped turn_id=%s reason=no_partial", turn_id)
        return False
    if not routes_to_stall(providers.llm, partial):
        log.debug("stt_stream_early_stall_skipped turn_id=%s reason=routed_direct", turn_id)
        return False
    try:
        stall_text = persona.stall_for(0)
    except Exception as e:
        swallowed("persona_no_stalls", e, turn_id=turn_id)
        return False
    if not stall_text:
        return False
    stall_audio = None
    try:
        stall_audio = await get_or_synth_stall(stall_text, persona, providers.tts)
    except ProviderError as e:
        swallowed("stall_synth_failed", None, turn_id=turn_id, reason=e.reason)
    if not await safe_send_json(
        ws, frame("agent.stall", turn_id, phrase_id="stall-0", text=stall_text)
    ):
        return False
    if not await safe_send_json(ws, frame("state.thinking", turn_id)):
        return True
    if stall_audio is not None:
        if not await safe_send_json(ws, frame("state.speaking", turn_id)):
            return True
        await safe_send_json(
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
        )
    return True


async def run_streaming_stop(
    ws: Any,
    turn_id: str,
    session: "SttStreamSession",
    persona: Persona,
    providers: Any,
    start_turn: Callable[[str, bool], Any],
    audio: Optional[bytes] = None,
    handover: bool = False,
    is_barged: Optional[Callable[[], bool]] = None,
    early_stall: Optional[bool] = None,
) -> bool:
    """``user.stop``, streaming: stall first, transcript second, turn third.

    Returns ``False`` when the stop could not be served from the stream and
    the caller must fall back to the whole-utterance path — always for a
    named reason, never silently.

    ``early_stall`` defaults to :func:`turn_accepts_stall_sent`; the hermetic
    test pins it so the ordering contract is provable without waiting on
    another lane's one-line hook.
    """
    if early_stall is None:
        early_stall = turn_accepts_stall_sent()

    stall_sent = False
    if early_stall:
        # Before finalizing, and therefore before the transcript exists. This
        # is the ordering the whole lane is about.
        stall_sent = await emit_early_stall(
            ws, turn_id, session.latest_partial(), persona, providers
        )

    if is_barged is not None and is_barged():
        session.cancel("barged_before_finalize")
        return True

    try:
        text = await session.finalize(audio)
    except ProviderError as e:
        if e.reason == "stt_stream_cancelled":
            log.info("stt_stream_stop_abandoned turn_id=%s reason=%s", turn_id, e.reason)
            return True
        raise

    # A barge that landed WHILE the tail was decoding: the person has moved
    # on, so the transcript is stale and the turn must not start.
    if is_barged is not None and is_barged():
        session.cancel("barged_during_finalize")
        log.info(
            "stt_stream_stop_abandoned turn_id=%s reason=barged_during_finalize", turn_id
        )
        return True

    if not text:
        # Named, and still a turn: handle_turn's empty_transcript path owns
        # what an empty transcript means, and it ends with agent.done.
        log.info("stt_stream_empty_final turn_id=%s reason=stt_empty_result", turn_id)

    await send_final_transcript(ws, turn_id, text, handover=handover)
    result = start_turn(text, stall_sent)
    if asyncio.iscoroutine(result):
        await result
    return True


def handle_turn_task_params() -> frozenset:
    """``turn.py``'s ``handle_turn_task`` parameter names, read live.

    Separate from :func:`turn_accepts_stall_sent` on purpose: that one is the
    POLICY probe (and the hermetic test pins it to prove the ordering
    contract), this one is the FACT about the function about to be called, so
    a forced-on test can never make the caller pass a kwarg that does not
    exist.
    """
    import inspect

    from .turn import handle_turn_task

    try:
        return frozenset(inspect.signature(handle_turn_task).parameters)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return frozenset()


def turn_accepts_stall_sent() -> bool:
    """Does ``turn.py`` know how to not send a second ``agent.stall``?

    The early stall above and ``turn.py``'s own worker-path stall are the same
    frames; sending both would speak two fillers at the person. ``turn.py`` is
    another lane's file, so the suppression is ONE line there
    (``handle_turn_task(..., stall_sent=False)`` threaded into
    ``if stall_text and not stall_sent:``) and this probe decides which of the
    two shapes is safe to run until it lands:

    * hook present  -> early stall, then the turn, no duplicate;
    * hook absent   -> no early stall. Streaming still finalizes on the tail
      instead of the whole utterance, so the stall still arrives far sooner
      than it does today; it just arrives after ``transcript.user``.
    """
    return "stall_sent" in handle_turn_task_params()
