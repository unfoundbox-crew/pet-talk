#!/usr/bin/env python3
"""qa/test_stt_streaming.py — streaming STT: the stall stops waiting for STT.

The measurement this suite exists to answer (docs/SPEC.md 9.1, 2026-09-12d):
``stall_ms`` p50 **642.2ms** against a **400ms** budget, of which **590ms was
one faster-whisper pass over the whole utterance**, started only after
``user.stop``. The same pass measures 143-164ms in isolation, so the fix is the
shape, not the box: transcribe while the person is still speaking.

Hermetic (always runs, no model, no network, no sound):

1. ``agent.stall`` goes out BEFORE the final ``transcript.user`` when STT is
   slow — the ordering inversion this lane is for.
2. Every ``partial: true`` transcript precedes the ``final: true`` one, and
   only one frame is final.
3. A barge landing during finalization cancels it: no final transcript, no
   turn started, named reason.
4. Finalize decodes the TAIL only, not the whole utterance, and joins the seam
   without stuttering the repeated word.
5. A tyre that says ``supports_streaming = False`` is refused BY NAME with
   ``PET_TALK_STT_STREAM=1`` — never silently downgraded.
6. Streaming off (the default) leaves ``_on_user_stop`` on its old path.

Real-engine (``PET_TALK_REAL_ENGINE=1`` only): the same ~3s clip through the
REAL faster-whisper tyre, streaming on and off, N=5, and the stop -> stall
latency of each. Reports the machine load it ran at, because 12d proved this
stage is contention-sensitive.

Run: ``python3 qa/test_stt_streaming.py``
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
import wave
from typing import AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("STT_PROVIDER", "stub")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("TTS_PROVIDER", "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")

from starlette.websockets import WebSocketState

from server import runtime, stt_stream, ws as ws_mod
from server.persona import Persona
from server.provider_factory import ProviderSet
from server.providers import LLMProvider, ProviderError, StubSTT, StubTTS
from server.providers.stt import make_stt

REAL_ENGINE = os.environ.get("PET_TALK_REAL_ENGINE") == "1"
FIXTURE = os.path.join(ROOT, "qa", "fixtures", "weather_turn.wav")
STALL_BUDGET_MS = 400.0

TEST_PERSONA = Persona(
    name="default",
    voice="af_heart",
    speed=1.0,
    stalls=["Let me look."],
    tone="plain",
)


# ------------------------------------------------------------------ tyres ---


class RouterLLM(LLMProvider):
    """Routes everything to the stall path, and streams one sentence."""

    def route(self, text: str) -> str:
        return "stall"

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        yield "Done."


class SegmentSTT(StubSTT):
    """Returns text derived from how many BYTES it was handed.

    That makes a decode's coverage visible to an assertion: a prefix decode and
    a tail decode cannot be told apart by a fixed stub, and "which audio did
    finalize actually pay for" is the claim this suite has to prove.
    """

    def __init__(self, delay_s: float = 0.0) -> None:
        super().__init__(delay_s=delay_s)
        self.calls: list[int] = []

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        self.calls.append(len(pcm16_bytes))
        if self.delay_s:
            time.sleep(self.delay_s)
        return f"words {len(pcm16_bytes)}"


def pcm(ms: int, sample_rate: int = 16000) -> bytes:
    """``ms`` of silence as PCM16 mono. Content is irrelevant to a stub."""
    return b"\x00\x01" * int(sample_rate * ms / 1000.0)


def fake_ws() -> MagicMock:
    w = MagicMock()
    w.client_state = WebSocketState.CONNECTED
    w.send_json = AsyncMock()
    return w


class TimedWS:
    """A fake socket that stamps every frame with the monotonic clock.

    The budget is "``agent.stall`` reaches the client within 400ms of
    ``user.stop``". Timing a HANDLER'S RETURN instead measures the finalize
    pass too and reads as a regression where there is none — the first version
    of this harness did exactly that and produced two runs that disagreed
    (186ms then 362ms p50 for the same code).
    """

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.stamped: list[tuple[float, dict]] = []

    async def send_json(self, payload: dict) -> None:
        self.stamped.append((time.monotonic(), payload))

    def first_at(self, ftype: str, **match) -> Optional[float]:
        for t, f in self.stamped:
            if f.get("type") != ftype:
                continue
            if all(f.get(k) == v for k, v in match.items()):
                return t
        return None


def frames(w: MagicMock) -> list[dict]:
    return [call.args[0] for call in w.send_json.await_args_list]


def types_of(w: MagicMock) -> list[str]:
    return [f.get("type") for f in frames(w)]


def index_of(w: MagicMock, ftype: str, **match) -> Optional[int]:
    for i, f in enumerate(frames(w)):
        if f.get("type") != ftype:
            continue
        if all(f.get(k) == v for k, v in match.items()):
            return i
    return None


class StreamEnv:
    """Set the streaming env vars for one test and put them back after."""

    def __init__(self, **env: str) -> None:
        self.env = env
        self.saved: dict[str, Optional[str]] = {}

    def __enter__(self) -> "StreamEnv":
        for k, v in self.env.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> None:
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def install(stt) -> ProviderSet:
    """Make ``stt`` the live tyre, with a stall-routing LLM and stub TTS."""
    pset = ProviderSet(stt=stt, llm=RouterLLM(), tts=StubTTS())
    runtime.install(pset)
    return pset


async def drive(
    session: ws_mod.Session,
    turn_id: str,
    chunk_ms: int = 200,
    n_chunks: int = 15,
    sample_rate: int = 16000,
    pause_s: float = 0.0,
) -> None:
    """Feed ``n_chunks`` of ``chunk_ms`` as ``user.chunk`` frames."""
    for _ in range(n_chunks):
        await ws_mod._on_user_chunk(
            session,
            {
                "type": "user.chunk",
                "turn_id": turn_id,
                "chunk": __import__("base64").b64encode(pcm(chunk_ms, sample_rate)).decode(),
                "sample_rate": sample_rate,
            },
        )
        if pause_s:
            await asyncio.sleep(pause_s)


# --------------------------------------------------------------- ordering ---


class TestStallBeatsFinalTranscript(unittest.TestCase):
    """Assertion 1 — the whole point of the lane."""

    def test_stall_precedes_final_transcript_with_slow_stt(self) -> None:
        async def go():
            stt = SegmentSTT(delay_s=0.30)  # a deliberately slow backend
            install(stt)
            w = fake_ws()
            session = ws_mod.Session(ws=w)
            session.turn_persona["t-order"] = TEST_PERSONA
            with StreamEnv(
                PET_TALK_STT_STREAM="1", PET_TALK_STT_STREAM_MS="400"
            ):
                await drive(session, "t-order", chunk_ms=200, n_chunks=10)
                # Let the scheduled prefix decode land before end-of-speech,
                # exactly as it does when a person is still talking.
                await asyncio.sleep(0.5)
                handled = await ws_mod._stream_stop(
                    session,
                    "t-order",
                    b"".join(session.chunks),
                    16000,
                    TEST_PERSONA,
                    session.queue_for("t-order"),
                    False,
                )
            self.assertTrue(handled, "the streaming path did not serve user.stop")
            i_stall = index_of(w, "agent.stall")
            i_final = index_of(w, "transcript.user", final=True)
            self.assertIsNotNone(i_stall, f"no agent.stall: {types_of(w)}")
            self.assertIsNotNone(i_final, f"no final transcript: {types_of(w)}")
            self.assertLess(
                i_stall,
                i_final,
                f"agent.stall must precede the final transcript: {types_of(w)}",
            )
            await session.shutdown()

        asyncio.run(_with_early_stall(go()))

    def test_partial_frames_all_precede_the_final_one(self) -> None:
        async def go():
            install(SegmentSTT())
            w = fake_ws()
            session = ws_mod.Session(ws=w)
            with StreamEnv(
                PET_TALK_STT_STREAM="1",
                PET_TALK_STT_STREAM_MS="300",
                PET_TALK_STT_PARTIALS="1",
            ):
                await drive(session, "t-part", chunk_ms=150, n_chunks=12, pause_s=0.01)
                await asyncio.sleep(0.2)
                await ws_mod._stream_stop(
                    session,
                    "t-part",
                    b"".join(session.chunks),
                    16000,
                    TEST_PERSONA,
                    session.queue_for("t-part"),
                    False,
                )
            transcripts = [f for f in frames(w) if f.get("type") == "transcript.user"]
            partials = [f for f in transcripts if f.get("partial")]
            finals = [f for f in transcripts if f.get("final")]
            self.assertGreaterEqual(
                len(partials), 1, "no partial transcript frames were sent"
            )
            self.assertEqual(len(finals), 1, "there must be exactly one final transcript")
            self.assertEqual(
                transcripts[-1],
                finals[0],
                "the final transcript must be the last transcript frame",
            )
            for f in partials:
                self.assertFalse(f.get("final"), "a partial frame claimed final=true")
            await session.shutdown()

        asyncio.run(go())


async def _with_early_stall(coro, forced: bool = True):
    """Pin ``turn_accepts_stall_sent`` for one coroutine.

    ``turn.py`` is another lane's file and the one-line ``stall_sent`` guard
    that stops a duplicate stall is reported, not landed here. The ORDERING
    contract above is this module's own, so it is proved against this module
    rather than against whether that line exists yet.
    """
    real = stt_stream.turn_accepts_stall_sent
    stt_stream.turn_accepts_stall_sent = lambda: forced
    try:
        return await coro
    finally:
        stt_stream.turn_accepts_stall_sent = real


# --------------------------------------------------------------- numbering ---


class DivergingRouterLLM(LLMProvider):
    """Routes the PARTIAL to the stall and the FINAL transcript direct.

    Not contrived: the early stall is decided on a partial and the turn is
    routed on the full transcript, so the two verdicts can disagree on any
    real router. That disagreement is what put two frames on ``seq=0``.
    """

    def __init__(self) -> None:
        self.routed: list[str] = []

    def route(self, text: str) -> str:
        self.routed.append(text)
        return "stall" if len(self.routed) == 1 else "direct"

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        yield "Twelve tests pass."


class TestSentenceSeqNeverCollides(unittest.TestCase):
    """``seq=0`` belongs to the stall. Every answer path starts at 1.

    The early stall sends ``agent.sentence seq=0`` and ``turn.py``'s direct
    path also started its answer at ``first_seq=0``. Two frames on one seq is
    not cosmetic: web/src/readAhead.ts ``insertSentence`` keys the read-ahead
    buffer BY SEQ, so the answer overwrote the stall in place and the buffer
    held one sentence where two were spoken.
    """

    def test_early_stall_then_direct_answer_yields_distinct_seqs(self) -> None:
        async def go():
            from server.speak_queue import SpeakQueue
            from server.turn import handle_turn

            llm = DivergingRouterLLM()
            pset = ProviderSet(stt=SegmentSTT(), llm=llm, tts=StubTTS())
            runtime.install(pset)
            w = fake_ws()

            sent = await stt_stream.emit_early_stall(
                w, "t-seq", "did the provider tests pass", TEST_PERSONA, pset
            )
            self.assertTrue(sent, "fixture failed: no early stall to collide with")

            await handle_turn(
                w, "t-seq", "did the provider tests pass?", SpeakQueue(),
                active_persona=TEST_PERSONA, providers=pset, stall_sent=True,
            )
            self.assertEqual(len(llm.routed), 2,
                             "fixture failed: the two routes did not diverge")

            seqs = [
                f.get("seq") for f in frames(w) if f.get("type") == "agent.sentence"
            ]
            self.assertGreaterEqual(len(seqs), 2, f"expected a stall and an answer: {seqs}")
            self.assertEqual(seqs[0], 0, "the stall does not own seq 0")
            self.assertEqual(
                len(seqs), len(set(seqs)),
                f"two agent.sentence frames share a seq: {seqs} — the client keys on it",
            )
            self.assertNotIn(0, seqs[1:], "an answer sentence reused the stall's seq")

        asyncio.run(go())

    def test_both_answer_paths_start_after_the_stall(self) -> None:
        """Read the source, not a comment: neither path may pass first_seq=0."""
        import inspect

        from server import turn as turn_mod

        src = inspect.getsource(turn_mod)
        self.assertEqual(src.count("first_seq=0"), 0,
                         "an answer path still starts at seq 0, which is the stall's")
        self.assertEqual(src.count("first_seq=1"), 2,
                         "expected exactly two answer paths, both at first_seq=1")


# ------------------------------------------------------------------ barge ---


class TestBargeDuringFinalize(unittest.TestCase):
    """Assertion 3 — a barge mid-finalize must not produce a stale turn."""

    def test_barge_during_finalization_cancels_it(self) -> None:
        async def go():
            stt = SegmentSTT(delay_s=0.25)
            install(stt)
            w = fake_ws()
            session = ws_mod.Session(ws=w)
            with StreamEnv(PET_TALK_STT_STREAM="1", PET_TALK_STT_STREAM_MS="300"):
                await drive(session, "t-barge", chunk_ms=150, n_chunks=8)
                await asyncio.sleep(0.4)
                audio = b"".join(session.chunks)
                stop = asyncio.ensure_future(
                    ws_mod._stream_stop(
                        session,
                        "t-barge",
                        audio,
                        16000,
                        TEST_PERSONA,
                        session.queue_for("t-barge"),
                        False,
                    )
                )
                await asyncio.sleep(0.02)  # finalize is now in its tail decode
                session.mark_barged("t-barge")
                handled = await stop
            self.assertTrue(handled, "the streaming path must own the stop it started")
            self.assertIsNone(
                index_of(w, "transcript.user", final=True),
                f"a barged turn sent a final transcript anyway: {types_of(w)}",
            )
            self.assertNotIn(
                "t-barge", session.turn_tasks, "a barged turn was started anyway"
            )
            await session.shutdown()

        asyncio.run(go())

    def test_cancelled_session_finalize_raises_named(self) -> None:
        async def go():
            stt = SegmentSTT()
            s = stt_stream.SttStreamSession(stt, "t-x", sample_rate=16000)
            await s.feed(pcm(100))
            s.cancel("barged")
            with self.assertRaises(ProviderError) as cm:
                await s.finalize()
            self.assertEqual(cm.exception.reason, "stt_stream_cancelled")

        asyncio.run(go())


# ------------------------------------------------------- tail-only decode ---


class TestTailOnlyFinalize(unittest.TestCase):
    """Assertion 4 — the finalize pass must not re-pay for the prefix."""

    def test_finalize_decodes_only_the_tail(self) -> None:
        async def go():
            stt = SegmentSTT()
            with StreamEnv(PET_TALK_STT_STREAM="1", PET_TALK_STT_STREAM_MS="400"):
                s = stt_stream.SttStreamSession(stt, "t-tail", sample_rate=16000)
                # 4 x 400ms of audio: the windows decode as prefixes.
                for _ in range(4):
                    await s.feed(pcm(400))
                    await asyncio.sleep(0.05)
                # 100ms more, under the window, so it schedules no decode and
                # is still waiting for somebody at end-of-speech. This is the
                # normal case: a person stops mid-window.
                await s.feed(pcm(100))
                total = len(s.audio)
                self.assertTrue(stt.calls, "no partial decode ever ran")
                covered = s.partial_offset()
                self.assertGreater(covered, 0, "no partial was recorded")
                before = len(stt.calls)
                await s.finalize()
                self.assertEqual(
                    len(stt.calls), before + 1, "finalize ran more than one decode"
                )
                tail_bytes = stt.calls[-1]
                self.assertEqual(
                    tail_bytes,
                    total - covered,
                    "finalize did not decode exactly the uncovered tail",
                )
                self.assertLess(
                    tail_bytes, total, "finalize decoded the whole utterance again"
                )

        asyncio.run(go())

    def test_final_full_env_restores_the_whole_utterance_pass(self) -> None:
        async def go():
            stt = SegmentSTT()
            with StreamEnv(
                PET_TALK_STT_STREAM="1",
                PET_TALK_STT_STREAM_MS="400",
                PET_TALK_STT_FINAL_FULL="1",
            ):
                s = stt_stream.SttStreamSession(stt, "t-full", sample_rate=16000)
                for _ in range(3):
                    await s.feed(pcm(400))
                    await asyncio.sleep(0.05)
                await s.finalize()
                self.assertEqual(
                    stt.calls[-1],
                    len(s.audio),
                    "PET_TALK_STT_FINAL_FULL=1 must decode the whole utterance",
                )

        asyncio.run(go())

    def test_seam_join_does_not_stutter_the_repeated_word(self) -> None:
        self.assertEqual(
            stt_stream._join_seam("what is the", "the weather today"),
            "what is the weather today",
        )
        self.assertEqual(
            stt_stream._join_seam("hello agent, what is", "What is the weather."),
            "hello agent, what is the weather.",
        )
        # A genuine repetition further back is left alone.
        self.assertEqual(
            stt_stream._join_seam("very very", "good"), "very very good"
        )
        self.assertEqual(stt_stream._join_seam("", "only tail"), "only tail")
        self.assertEqual(stt_stream._join_seam("only prefix", ""), "only prefix")

    def test_no_partial_falls_back_to_the_whole_utterance_by_name(self) -> None:
        async def go():
            stt = SegmentSTT()
            with StreamEnv(PET_TALK_STT_STREAM="1", PET_TALK_STT_STREAM_MS="5000"):
                s = stt_stream.SttStreamSession(stt, "t-none", sample_rate=16000)
                await s.feed(pcm(200))  # never fills a 5s window
                text = await s.finalize()
            self.assertEqual(
                stt.calls, [len(s.audio)], "the fallback must decode the whole utterance"
            )
            self.assertTrue(text)

        asyncio.run(go())


# ----------------------------------------------------- provider capability ---


class TestProviderCapability(unittest.TestCase):
    """Assertion 5 — capability is a flag, and a refusal is named."""

    def test_flags_match_what_each_tyre_can_actually_do(self) -> None:
        expected = {
            "stub": True,
            "faster-whisper": True,
            "whisper-local": True,
            "groq": False,
            "deepgram": False,
            "openai": False,
            "sensevoice": False,
            "whisperkit": False,
            "mlx": False,
        }
        for name, want in expected.items():
            with self.subTest(provider=name):
                self.assertEqual(
                    stt_stream.provider_supports_streaming(make_stt(name, api_key="x")),
                    want,
                )

    def test_unsupported_provider_is_refused_by_name(self) -> None:
        with StreamEnv(PET_TALK_STT_STREAM="1"):
            usable, reason = stt_stream.streaming_available(make_stt("groq", api_key="x"))
        self.assertFalse(usable)
        self.assertEqual(reason, "stt_stream_unsupported_provider")

    def test_disabled_is_its_own_reason(self) -> None:
        with StreamEnv(PET_TALK_STT_STREAM="0"):
            usable, reason = stt_stream.streaming_available(StubSTT())
        self.assertFalse(usable)
        self.assertEqual(reason, "stt_stream_disabled")

    def test_chunk_handler_opens_no_session_on_an_unsupported_tyre(self) -> None:
        async def go():
            install(make_stt("groq", api_key="x"))
            session = ws_mod.Session(ws=fake_ws())
            with StreamEnv(PET_TALK_STT_STREAM="1"):
                await drive(session, "t-unsup", chunk_ms=200, n_chunks=5)
            self.assertIsNone(session.stt_stream)
            self.assertEqual(
                len(b"".join(session.chunks)),
                len(pcm(200)) * 5,
                "the classic buffer must still collect every chunk",
            )

        asyncio.run(go())

    def test_streaming_off_leaves_the_old_path_alone(self) -> None:
        async def go():
            install(SegmentSTT())
            session = ws_mod.Session(ws=fake_ws())
            with StreamEnv(PET_TALK_STT_STREAM="0"):
                await drive(session, "t-off", chunk_ms=200, n_chunks=5)
                handled = await ws_mod._stream_stop(
                    session,
                    "t-off",
                    b"".join(session.chunks),
                    16000,
                    TEST_PERSONA,
                    session.queue_for("t-off"),
                    False,
                )
            self.assertIsNone(session.stt_stream)
            self.assertFalse(
                handled, "with streaming off, user.stop must take the unchanged path"
            )

        asyncio.run(go())

    def test_provider_swap_mid_utterance_refuses_the_partials(self) -> None:
        async def go():
            first = SegmentSTT()
            install(first)
            session = ws_mod.Session(ws=fake_ws())
            with StreamEnv(PET_TALK_STT_STREAM="1", PET_TALK_STT_STREAM_MS="200"):
                await drive(session, "t-swap", chunk_ms=200, n_chunks=3)
                self.assertIsNotNone(session.stt_stream)
                install(SegmentSTT())  # a POST /settings lands mid-utterance
                handled = await ws_mod._stream_stop(
                    session,
                    "t-swap",
                    b"".join(session.chunks),
                    16000,
                    TEST_PERSONA,
                    session.queue_for("t-swap"),
                    False,
                )
            self.assertFalse(
                handled,
                "a tyre swap mid-utterance must not glue one backend's partial "
                "to another's tail",
            )

        asyncio.run(go())


# ------------------------------------------------------------ real engine ---


def load_fixture_pcm(target_s: float = 3.0) -> tuple[bytes, int]:
    """The weather fixture, repeated to ~``target_s``. 1.44s is too short to
    show a streaming win; repeating it keeps the audio real and the length
    honest (it is the same sentence twice, and it is labelled as such)."""
    with wave.open(FIXTURE) as w:
        rate = w.getframerate()
        one = w.readframes(w.getnframes())
    reps = max(1, round(target_s / (len(one) / (rate * 2))))
    return one * reps, rate


class TestRealEngineStreamingLatency(unittest.TestCase):
    """The number. Skipped unless PET_TALK_REAL_ENGINE=1."""

    @unittest.skipUnless(
        REAL_ENGINE, "PET_TALK_REAL_ENGINE!=1 — this test loads the real STT model"
    )
    def test_stall_latency_streaming_on_vs_off(self) -> None:
        audio, rate = load_fixture_pcm(3.0)
        n = int(os.environ.get("STREAM_N", "5"))
        load = os.getloadavg()
        print(
            "\nreal-engine STT streaming measurement\n"
            f"  clip          {len(audio) / (rate * 2):.2f}s @ {rate}Hz "
            f"({os.path.basename(FIXTURE)}, repeated)\n"
            f"  load (1/5/15) {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}\n"
            f"  window        {os.environ.get('PET_TALK_STT_STREAM_MS', '700')}ms, "
            f"N={n} per mode"
        )

        on_stall, on_final = self._run_mode(audio, rate, n, streaming=True)
        # The tier that works TODAY, with turn.py untouched: streaming still
        # finalizes on the tail, but the stall waits for the transcript because
        # turn.py is the only thing allowed to send it.
        tail_stall, tail_final = self._run_mode(
            audio, rate, n, streaming=True, early=False
        )
        off_stall, off_final = self._run_mode(audio, rate, n, streaming=False)
        print("  %-30s %8s %8s %8s" % ("metric", "p50", "p95", "budget"))
        rows = (
            ("stall_ms  ON, early stall", on_stall, STALL_BUDGET_MS),
            ("stall_ms  ON, tail only", tail_stall, STALL_BUDGET_MS),
            ("stall_ms  streaming OFF", off_stall, STALL_BUDGET_MS),
            ("final_ms  ON, early stall", on_final, None),
            ("final_ms  ON, tail only", tail_final, None),
            ("final_ms  streaming OFF", off_final, None),
        )
        for label, vals, budget in rows:
            if not vals:
                print("  %-30s %8s  NOT-MEASURED" % (label, "-"))
                continue
            verdict = ""
            if budget is not None:
                verdict = "PASS" if _p(vals, 95) <= budget else "FAIL"
            print(
                "  %-30s %8.1f %8.1f %8s  %s"
                % (label, _p(vals, 50), _p(vals, 95), budget or "-", verdict)
            )
            print("      samples: %s" % ", ".join("%.0f" % v for v in vals))
        self.assertTrue(on_stall, "streaming mode produced no stall measurement")
        self.assertTrue(off_stall, "non-streaming mode produced no stall measurement")
        # Reported, not silenced: a streaming pass that does not beat the
        # whole-utterance pass is a finding, and this test says so out loud
        # rather than failing the gate on this machine's load.
        if _p(on_stall, 50) >= _p(off_stall, 50):
            print(
                "  FINDING: streaming did NOT beat the whole-utterance stall "
                f"({_p(on_stall, 50):.1f}ms vs {_p(off_stall, 50):.1f}ms p50)"
            )
        if _p(on_stall, 95) > STALL_BUDGET_MS:
            print(
                f"  FINDING: streaming stall p95 {_p(on_stall, 95):.1f}ms is over "
                f"the {STALL_BUDGET_MS:.0f}ms budget at this load"
            )
        if on_final and off_final and _p(on_final, 50) > _p(off_final, 50) * 1.25:
            print(
                "  FINDING: streaming made the FINAL transcript slower "
                f"({_p(on_final, 50):.1f}ms vs {_p(off_final, 50):.1f}ms p50) — "
                "the stall win was bought from the answer"
            )

    def _run_mode(
        self, audio: bytes, rate: int, n: int, streaming: bool, early: bool = True
    ) -> tuple[list[float], list[float]]:
        """``n`` turns, chunks paced in real time. Returns (stall_ms, final_ms).

        Real STT, stub TTS: the stall's audio comes from the same warm cache it
        comes from in production, so this isolates exactly the stage this lane
        changed and does not re-measure Kokoro.

        Both numbers are reported and neither hides the other. ``stall_ms`` is
        the budget (when the person hears the filler); ``final_ms`` is when the
        answer can start, which streaming must not wreck to buy the stall.
        """
        stalls: list[float] = []
        finals: list[float] = []
        stt = make_stt("faster-whisper")
        install(stt)
        chunk = int(rate * 2 * 0.02)  # 20ms, what the CLI client sends

        async def one(turn_no: int) -> tuple[Optional[float], Optional[float]]:
            import base64

            w = TimedWS()
            session = ws_mod.Session(ws=w)
            turn_id = f"t-stream-{turn_no}"
            for off in range(0, len(audio), chunk):
                await ws_mod._on_user_chunk(
                    session,
                    {
                        "type": "user.chunk",
                        "turn_id": turn_id,
                        "chunk": base64.b64encode(audio[off : off + chunk]).decode(),
                        "sample_rate": rate,
                    },
                )
                await asyncio.sleep(0.02)  # real time, like a speaking person
            t0 = time.monotonic()
            if streaming:
                handled = await ws_mod._stream_stop(
                    session, turn_id, audio, rate, TEST_PERSONA,
                    session.queue_for(turn_id), False,
                )
                if not handled:
                    return None, None
                t_final = w.first_at("transcript.user", final=True)
                t_stall = w.first_at("agent.stall")
                if t_stall is None and t_final is not None:
                    # Tier A: turn.py owns the stall and it starts from the
                    # transcript. Add the stall's own cost (route + warm cache)
                    # so the two tiers are compared on the same clock.
                    from server.stall import get_or_synth_stall

                    pset = runtime.snapshot()
                    pset.llm.route(w.stamped[-1][1].get("text") or "x")
                    await get_or_synth_stall(
                        TEST_PERSONA.stall_for(0), TEST_PERSONA, pset.tts
                    )
                    t_stall = time.monotonic()
            else:
                # Today's shape, measured the same way: one whole-utterance
                # pass, THEN the stall.
                from server.stall import get_or_synth_stall
                from server.turn import transcribe_off_loop

                pset = runtime.snapshot()
                await transcribe_off_loop(pset, audio, rate)
                t_final = time.monotonic()
                await get_or_synth_stall(
                    TEST_PERSONA.stall_for(0), TEST_PERSONA, pset.tts
                )
                t_stall = time.monotonic()
            await session.shutdown()
            return (
                None if t_stall is None else (t_stall - t0) * 1000.0,
                None if t_final is None else (t_final - t0) * 1000.0,
            )

        async def go():
            with StreamEnv(
                PET_TALK_STT_STREAM="1" if streaming else "0",
                PET_TALK_STT_STREAM_MS=os.environ.get("PET_TALK_STT_STREAM_MS", "700"),
            ):
                await one(-1)  # warm-up: the model load is not what is measured
                for i in range(n):
                    a, b = await one(i)
                    if a is not None:
                        stalls.append(a)
                    if b is not None:
                        finals.append(b)

        asyncio.run(_with_early_stall(go(), forced=early))
        return stalls, finals


def _p(values: list[float], pct: float) -> float:
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(pct / 100.0 * (len(s) - 1))))
    return s[k]


if __name__ == "__main__":
    unittest.main(verbosity=2)
