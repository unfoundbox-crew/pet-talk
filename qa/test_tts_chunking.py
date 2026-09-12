#!/usr/bin/env python3
"""qa/test_tts_chunking.py — chunked TTS and the warm first turn, hermetically.

What this proves (no network, no daemon, no audio device, no sound):

1. `agent.chunk` is a real frame on the catalogue and obeys the turn_id law:
   a chunk frame without `turn_id` raises `frame_no_turn_id`.
2. `agent.sentence` with `chunked=True` carries a non-null `stream_url`, and
   a whole-sentence sentence still carries `audio_url` — backward compatible
   for one release (docs/SPEC.md 4.2).
3. A backend WITHOUT `synth_chunks` still speaks: `chunked=False`,
   `stream_url=None`, zero `agent.chunk` frames, and the reason is named in
   telemetry (`tts_no_chunk_support`) — never a silent fallback.
4. A backend WITH `synth_chunks` sends its FIRST `agent.chunk` before the
   whole-sentence WAV exists, every chunk is a standalone playable RIFF, and
   only the last chunk carries `final=True`.
5. The clause splitter never loses or reorders a word.
6. `warm_stall_cache` pre-synthesizes the active persona's stalls and stays
   inside `PET_TALK_STALL_CACHE_MAX`.

Run: `python3 qa/test_tts_chunking.py`
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import struct
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

from server import stall as stall_mod
from server.audio_store import AUDIO_STORE, get_audio
from server.frames import frame
from server.persona import Persona
from server.providers import ProviderError, StubTTS, TTSProvider
from server.providers._shared import concat_wavs, pcm_duration_ms, shift_word_times
from server.providers.tts import StubChunkedTTS, chunk_clauses
from server.speech import speak_sentence

SENTENCE = "The weather today is bright and clear so the afternoon looks genuinely pleasant"


def tiny_wav(duration_s: float = 0.05, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<h", 0) * int(duration_s * sample_rate))
    return buf.getvalue()


def fake_ws() -> MagicMock:
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def sent_frames(ws: MagicMock) -> list[dict]:
    return [call.args[0] for call in ws.send_json.call_args_list]


def persona() -> Persona:
    return Persona(
        name="qa-chunk",
        voice="af_heart",
        speed=1.0,
        stalls=["Let me think.", "One moment.", "Checking that now."],
        tone="plain",
    )


# ------------------------------------------------------------- test tyres ---


class SlowWholeTTS(TTSProvider):
    """No `synth_chunks`. Blocks for `delay_s`, then returns one whole WAV."""

    def __init__(self, delay_s: float = 0.25) -> None:
        self.delay_s = delay_s
        self.base_url = "test://slow-whole"
        self.calls = 0

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0):
        self.calls += 1
        time.sleep(self.delay_s)
        wav = tiny_wav()
        return wav, []


class TimedChunkedTTS(StubChunkedTTS):
    """Chunked tyre that records when each chunk left the provider, so the
    test can prove the first chunk predates the whole sentence."""

    def __init__(self, per_chunk_s: float = 0.08) -> None:
        super().__init__(per_chunk_s=per_chunk_s)
        self.emitted_at: list[float] = []
        self.finished_at: Optional[float] = None

    async def synth_chunks(self, text, voice="af_heart", speed=1.0, cancel=None):
        async for wav, wt, final in super().synth_chunks(
            text, voice=voice, speed=speed, cancel=cancel
        ):
            self.emitted_at.append(time.perf_counter())
            if final:
                self.finished_at = time.perf_counter()
            yield wav, wt, final


class FatChunkTTS(TTSProvider):
    """Chunked tyre whose chunks are deliberately huge — an untrusted backend."""

    def __init__(self, sizes_bytes: list[int]) -> None:
        self.sizes_bytes = sizes_bytes
        self.base_url = "test://fat-chunks"

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0):
        return tiny_wav(), []

    async def synth_chunks(self, text, voice="af_heart", speed=1.0, cancel=None):
        for i, want in enumerate(self.sizes_bytes):
            # 8kHz 16-bit mono: 16000 bytes per second of audio.
            wav = tiny_wav(max(0.01, want / 16000.0), 8000)
            yield wav, [], i == len(self.sizes_bytes) - 1


class LyingFinalTTS(TTSProvider):
    """Chunked tyre that gets `final` wrong on purpose.

    ``finals`` is the flag it puts on each chunk — all False (never terminates
    the stream) or all True (terminates it three times over). Both are what a
    third-party backend does, and neither may reach the client.
    """

    def __init__(self, chunks: int = 3, finals: Optional[list[bool]] = None) -> None:
        self.chunks = chunks
        self.finals = finals if finals is not None else [False] * chunks
        self.base_url = "test://lying-final"

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0):
        return tiny_wav(), []

    async def synth_chunks(self, text, voice="af_heart", speed=1.0, cancel=None):
        for i in range(self.chunks):
            yield tiny_wav(0.02), [], self.finals[i]


# ------------------------------------------------------------------ tests ---


class TestChunkFrameContract(unittest.TestCase):
    def test_chunk_frame_without_turn_id_fails_closed(self) -> None:
        with self.assertRaises(ProviderError) as cm:
            frame("agent.chunk", "", seq=1, chunk_no=0, audio_b64="", final=True)
        self.assertEqual(cm.exception.reason, "frame_no_turn_id")

    def test_chunk_frame_shape(self) -> None:
        f = frame(
            "agent.chunk", "t1-abc", seq=1, chunk_no=0, audio_b64="AAA", final=False
        )
        self.assertEqual(f["type"], "agent.chunk")
        self.assertEqual(f["turn_id"], "t1-abc")
        for key in ("seq", "chunk_no", "audio_b64", "final"):
            self.assertIn(key, f)

    def test_protocol_validator_knows_agent_chunk(self) -> None:
        from qa.test_protocol import validate_frame

        ok, err = validate_frame(
            {
                "type": "agent.chunk",
                "turn_id": "t1",
                "seq": 1,
                "chunk_no": 0,
                "audio_b64": "AAA",
                "final": True,
            }
        )
        self.assertTrue(ok, err)
        ok, err = validate_frame(
            {"type": "agent.chunk", "turn_id": "t1", "seq": 1, "chunk_no": 0}
        )
        self.assertFalse(ok, "a chunk with no audio payload must be rejected")


class TestClauseSplitter(unittest.TestCase):
    def test_splits_and_loses_nothing(self) -> None:
        parts = chunk_clauses(SENTENCE, max_words=5)
        self.assertGreater(len(parts), 1, "a 13-word sentence must split")
        self.assertEqual(" ".join(parts).split(), SENTENCE.split())
        for part in parts:
            self.assertTrue(part.strip())

    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(chunk_clauses("Hello there.", max_words=6), ["Hello there."])

    def test_respects_max_words(self) -> None:
        long_text = " ".join(f"word{i}" for i in range(30))
        for part in chunk_clauses(long_text, max_words=4):
            self.assertLessEqual(len(part.split()), 4)

    def test_first_chunk_is_shorter_than_the_rest(self) -> None:
        long_text = " ".join(f"word{i}" for i in range(24))
        parts = chunk_clauses(long_text, max_words=8, first_max_words=3)
        self.assertEqual(len(parts[0].split()), 3, "chunk 0 is the only one on a budget")
        self.assertGreater(len(parts[1].split()), 3)
        self.assertEqual(" ".join(parts).split(), long_text.split())

    def test_first_chunk_cap_never_exceeds_the_tail_cap(self) -> None:
        parts = chunk_clauses(" ".join(f"w{i}" for i in range(12)), max_words=2, first_max_words=9)
        for part in parts:
            self.assertLessEqual(len(part.split()), 2)

    def test_empty_text_is_no_chunks(self) -> None:
        self.assertEqual(chunk_clauses("   ", max_words=5), [])


class TestRuntimeKeyForwarding(unittest.TestCase):
    """A key supplied at runtime must reach the provider it was posted for.

    Reported by lane 4c: `POST /settings` accepted a key for `elevenlabs` or
    `deepgram` and `make_tts` dropped it, so the swap landed as
    `missing_api_key` even though the key was right there.
    """

    def setUp(self) -> None:
        self._saved = {k: os.environ.pop(k, None) for k in ("ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY")}

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_posted_key_reaches_elevenlabs(self) -> None:
        from server.providers import make_tts

        tts = make_tts("elevenlabs", api_key="posted-eleven-key")
        self.assertEqual(tts._key(), "posted-eleven-key")
        self.assertNotIn("posted-eleven-key", repr(tts), "a repr must never leak the key")

    def test_posted_key_reaches_deepgram(self) -> None:
        from server.providers import make_tts

        tts = make_tts("deepgram", api_key="posted-deepgram-key")
        self.assertEqual(tts._key(), "posted-deepgram-key")
        self.assertNotIn("posted-deepgram-key", repr(tts))

    def test_no_key_anywhere_still_fails_closed_by_name(self) -> None:
        from server.providers import make_tts

        for provider in ("elevenlabs", "deepgram"):
            with self.subTest(provider=provider):
                with self.assertRaises(ProviderError) as cm:
                    make_tts(provider)._key()
                self.assertEqual(cm.exception.reason, "tts_no_key")

    def test_tts_voice_env_selects_the_voice_without_a_code_change(self) -> None:
        from server.providers import make_tts

        os.environ["TTS_VOICE"] = "aura-2-andromeda-en"
        try:
            self.assertEqual(make_tts("deepgram", api_key="k").model, "aura-2-andromeda-en")
        finally:
            os.environ.pop("TTS_VOICE", None)


class TestWavHelpers(unittest.TestCase):
    def test_concat_wavs_sums_duration(self) -> None:
        a, b = tiny_wav(0.10), tiny_wav(0.20)
        joined = concat_wavs([a, b])
        self.assertAlmostEqual(pcm_duration_ms(joined), 300, delta=5)

    def test_concat_wavs_rejects_mismatched_formats(self) -> None:
        with self.assertRaises(ProviderError) as cm:
            concat_wavs([tiny_wav(0.1, 8000), tiny_wav(0.1, 24000)])
        self.assertEqual(cm.exception.reason, "tts_chunk_format_mismatch")

    def test_duration_ignores_a_lying_header(self) -> None:
        # Deepgram's speak endpoint writes a streaming placeholder into the
        # data chunk size; a 100ms clip read back as ~12 hours (2026-09-12).
        wav = bytearray(tiny_wav(0.10, 8000))
        wav[40:44] = (0x7FFF0000).to_bytes(4, "little")
        self.assertAlmostEqual(pcm_duration_ms(bytes(wav)), 100, delta=10)

    def test_shift_word_times_offsets_both_ends(self) -> None:
        wt = [{"word": "hi", "start_ms": 0, "end_ms": 100, "estimated": True}]
        out = shift_word_times(wt, 250)
        self.assertEqual(out[0]["start_ms"], 250)
        self.assertEqual(out[0]["end_ms"], 350)
        self.assertEqual(wt[0]["start_ms"], 0, "must not mutate the input")


class TestSpeakSentenceFallback(unittest.IsolatedAsyncioTestCase):
    async def test_backend_without_synth_chunks_still_speaks(self) -> None:
        ws = fake_ws()
        tts = SlowWholeTTS(delay_s=0.0)
        with self.assertLogs("pet_talk.server", level="INFO") as logs:
            spoken = await speak_sentence(ws, "t-fb", SENTENCE, 1, persona(), tts)
        self.assertIsNotNone(spoken)
        frames = sent_frames(ws)
        self.assertEqual(
            [f for f in frames if f["type"] == "agent.chunk"],
            [],
            "a non-chunking backend must emit no agent.chunk frames",
        )
        sentence = [f for f in frames if f["type"] == "agent.sentence"][0]
        self.assertFalse(sentence["chunked"])
        self.assertIsNone(sentence["stream_url"])
        self.assertTrue(sentence["audio_url"].startswith("/audio/"))
        self.assertTrue(
            any("tts_no_chunk_support" in line for line in logs.output),
            f"the fallback reason must be named in telemetry; got {logs.output}",
        )

    async def test_chunking_can_be_switched_off_by_env(self) -> None:
        ws = fake_ws()
        tts = StubChunkedTTS(per_chunk_s=0.0)
        os.environ["PET_TALK_TTS_CHUNKS"] = "0"
        try:
            with self.assertLogs("pet_talk.server", level="INFO") as logs:
                await speak_sentence(ws, "t-off", SENTENCE, 1, persona(), tts)
        finally:
            os.environ.pop("PET_TALK_TTS_CHUNKS", None)
        frames = sent_frames(ws)
        self.assertEqual([f for f in frames if f["type"] == "agent.chunk"], [])
        self.assertFalse([f for f in frames if f["type"] == "agent.sentence"][0]["chunked"])
        self.assertTrue(any("tts_chunking_disabled" in line for line in logs.output))


class TestSpeakSentenceChunked(unittest.IsolatedAsyncioTestCase):
    async def test_first_chunk_precedes_the_whole_sentence(self) -> None:
        ws = fake_ws()
        tts = TimedChunkedTTS(per_chunk_s=0.08)
        t0 = time.perf_counter()
        first_chunk_at: list[float] = []

        async def record(payload: dict) -> None:
            if payload["type"] == "agent.chunk" and payload["chunk_no"] == 0:
                first_chunk_at.append(time.perf_counter())

        ws.send_json.side_effect = record
        spoken = await speak_sentence(ws, "t-ch", SENTENCE, 2, persona(), tts)
        self.assertIsNotNone(spoken)
        self.assertTrue(first_chunk_at, "no agent.chunk frame was ever sent")
        self.assertIsNotNone(tts.finished_at)
        self.assertLess(
            first_chunk_at[0],
            tts.finished_at,
            "the first chunk must reach the socket before synthesis finishes",
        )

        frames = sent_frames(ws)
        chunks = [f for f in frames if f["type"] == "agent.chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual([c["chunk_no"] for c in chunks], list(range(len(chunks))))
        self.assertEqual([c["final"] for c in chunks], [False] * (len(chunks) - 1) + [True])
        for c in chunks:
            self.assertEqual(c["seq"], 2)
            wav = base64.b64decode(c["audio_b64"])
            self.assertTrue(wav.startswith(b"RIFF"), "every chunk must be playable alone")
            self.assertGreater(pcm_duration_ms(wav), 0)
            self.assertEqual(get_audio(c["url"].rsplit("/", 1)[-1]), wav)

        sentence = [f for f in frames if f["type"] == "agent.sentence"][0]
        self.assertTrue(sentence["chunked"])
        self.assertEqual(sentence["stream_url"], f"/audio/{chunks[0]['url'].rsplit('/', 1)[-1]}")
        self.assertTrue(sentence["audio_url"].startswith("/audio/"))

        # Order on the wire: every chunk lands before the sentence frame.
        types = [f["type"] for f in frames]
        self.assertLess(
            max(i for i, t in enumerate(types) if t == "agent.chunk"),
            types.index("agent.sentence"),
        )

    async def test_whole_sentence_audio_is_the_chunks_joined(self) -> None:
        ws = fake_ws()
        tts = StubChunkedTTS(per_chunk_s=0.0)
        await speak_sentence(ws, "t-join", SENTENCE, 1, persona(), tts)
        frames = sent_frames(ws)
        chunks = [base64.b64decode(f["audio_b64"]) for f in frames if f["type"] == "agent.chunk"]
        sentence = [f for f in frames if f["type"] == "agent.sentence"][0]
        whole = get_audio(sentence["audio_url"].rsplit("/", 1)[-1])
        self.assertIsNotNone(whole)
        self.assertAlmostEqual(
            pcm_duration_ms(whole), sum(pcm_duration_ms(c) for c in chunks), delta=10
        )

    async def test_word_times_are_monotonic_across_chunks(self) -> None:
        ws = fake_ws()
        spoken = await speak_sentence(
            ws, "t-wt", SENTENCE, 1, persona(), StubChunkedTTS(per_chunk_s=0.0)
        )
        self.assertIsNotNone(spoken)
        times = spoken.word_times
        self.assertEqual(len(times), len(SENTENCE.split()))
        for a, b in zip(times, times[1:]):
            self.assertLessEqual(a["end_ms"], b["start_ms"] + 1)

    async def test_chunk_error_is_named_not_silent(self) -> None:
        class BrokenChunkedTTS(StubChunkedTTS):
            async def synth_chunks(self, text, voice="af_heart", speed=1.0, cancel=None):
                yield tiny_wav(), [], False
                raise ProviderError("tts_local_synth_failed", "second clause blew up")

        ws = fake_ws()
        spoken = await speak_sentence(ws, "t-err", SENTENCE, 1, persona(), BrokenChunkedTTS())
        self.assertIsNone(spoken)
        errors = [f for f in sent_frames(ws) if f["type"] == "agent.error"]
        self.assertEqual(errors[0]["reason"], "tts_local_synth_failed")


class TestWarmStallCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        stall_mod._STALL_AUDIO_CACHE.clear()
        AUDIO_STORE.clear()

    async def test_warm_populates_the_cache_for_the_active_persona(self) -> None:
        p = persona()
        tts = StubTTS()
        self.assertEqual(stall_mod.stall_cache_size(), 0)
        n = await stall_mod.warm_stall_cache(p, tts)
        self.assertEqual(n, len(p.stalls))
        self.assertEqual(stall_mod.stall_cache_size(), len(p.stalls))
        for phrase in p.stalls:
            cached = await stall_mod.get_or_synth_stall(phrase, p, tts)
            self.assertTrue(cached.wav.startswith(b"RIFF"))

    async def test_warm_is_a_cache_hit_for_every_later_turn(self) -> None:
        p = persona()

        class CountingTTS(StubTTS):
            calls = 0

            def synth(self, text, voice="af_heart", speed=1.0, cancel=None):
                CountingTTS.calls += 1
                return super().synth(text, voice=voice, speed=speed, cancel=cancel)

        tts = CountingTTS()
        await stall_mod.warm_stall_cache(p, tts)
        after_warm = CountingTTS.calls
        await stall_mod.get_or_synth_stall(p.stalls[0], p, tts)
        self.assertEqual(CountingTTS.calls, after_warm, "a warm stall must not re-synth")

    async def test_warm_stays_inside_the_cache_bound(self) -> None:
        os.environ["PET_TALK_STALL_CACHE_MAX"] = "2"
        try:
            p = Persona(
                name="qa-many",
                voice="af_heart",
                speed=1.0,
                stalls=["One.", "Two.", "Three.", "Four.", "Five."],
                tone="plain",
            )
            await stall_mod.warm_stall_cache(p, StubTTS())
            self.assertLessEqual(stall_mod.stall_cache_size(), 2)
        finally:
            os.environ.pop("PET_TALK_STALL_CACHE_MAX", None)

    async def test_warm_can_be_switched_off_and_says_so(self) -> None:
        os.environ["PET_TALK_STALL_WARM"] = "0"
        try:
            with self.assertLogs("pet_talk.server", level="INFO") as logs:
                n = await stall_mod.warm_stall_cache(persona(), StubTTS())
        finally:
            os.environ.pop("PET_TALK_STALL_WARM", None)
        self.assertEqual(n, 0)
        self.assertEqual(stall_mod.stall_cache_size(), 0)
        self.assertTrue(any("stall_warm_disabled" in line for line in logs.output))

    async def test_a_failing_backend_does_not_kill_startup(self) -> None:
        class DeadTTS(TTSProvider):
            def synth(self, text, voice="af_heart", speed=1.0):
                raise ProviderError("tts_no_key", "no key in this test")

        with self.assertLogs("pet_talk.server", level="INFO") as logs:
            n = await stall_mod.warm_stall_cache(persona(), DeadTTS())
        self.assertEqual(n, 0)
        self.assertTrue(any("stall_warm_failed" in line for line in logs.output))


class TestRealKokoroChunking(unittest.IsolatedAsyncioTestCase):
    """The same claims against the real tyre. Gated on PET_TALK_REAL_ENGINE=1.

    The hermetic tests above prove the wire path; only this one can prove the
    number. `tts_ms` is the time to the first PLAYABLE audio of a sentence,
    which under chunked synthesis is chunk 0, not the whole WAV.
    """

    N = 5

    async def asyncSetUp(self) -> None:
        if os.environ.get("PET_TALK_REAL_ENGINE") != "1":
            raise unittest.SkipTest(
                "SKIP: PET_TALK_REAL_ENGINE!=1 — this test loads real Kokoro "
                "weights and synthesizes audio"
            )
        from server.providers.tts import KokoroLocalTTS

        self.tts = KokoroLocalTTS(warm=False)
        try:
            self.tts.synth("Warming up.")  # pay the cold graph build here
        except ProviderError as e:
            raise unittest.SkipTest(f"SKIP: kokoro-local unavailable: {e.reason}")

    async def test_first_chunk_beats_the_whole_sentence_and_the_budget(self) -> None:
        import json

        budget = json.load(
            open(os.path.join(ROOT, "qa", "budgets.json"), encoding="utf-8")
        )["tts_ms"]
        firsts: list[float] = []
        wholes: list[float] = []
        for _ in range(self.N):
            t0 = time.perf_counter()
            first: Optional[float] = None
            finals = 0
            async for wav, _wt, final in self.tts.synth_chunks(SENTENCE, "af_heart", 1.0):
                if first is None:
                    first = (time.perf_counter() - t0) * 1000.0
                    self.assertTrue(wav.startswith(b"RIFF"))
                finals += int(bool(final))
            firsts.append(first or float("inf"))
            self.assertEqual(finals, 1, "exactly one chunk carries final=True")
            t0 = time.perf_counter()
            self.tts.synth(SENTENCE, "af_heart", 1.0)
            wholes.append((time.perf_counter() - t0) * 1000.0)

        firsts.sort()
        wholes.sort()
        p50_first = firsts[len(firsts) // 2]
        p50_whole = wholes[len(wholes) // 2]
        print(
            f"\n  real kokoro-local N={self.N}: first_chunk p50={p50_first:.1f}ms "
            f"whole_sentence p50={p50_whole:.1f}ms budget={budget}ms"
        )
        self.assertLess(
            p50_first, p50_whole, "chunking must beat whole-sentence synthesis"
        )
        self.assertLess(
            p50_first,
            budget,
            f"first-chunk p50 {p50_first:.1f}ms breaches the {budget}ms tts_ms budget",
        )


class TestChunkPayloadCap(unittest.IsolatedAsyncioTestCase):
    """A chunk is base64'd into a WS frame. An untrusted backend that hands
    back a 40 MB "clause" would push it straight at the browser."""

    def tearDown(self) -> None:
        os.environ.pop("PET_TALK_CHUNK_MAX_BYTES", None)

    async def test_the_default_cap_is_512_kib(self) -> None:
        from server.speech import chunk_max_bytes

        os.environ.pop("PET_TALK_CHUNK_MAX_BYTES", None)
        self.assertEqual(chunk_max_bytes(), 512 * 1024)

    async def test_a_garbage_cap_falls_back_loudly(self) -> None:
        from server.speech import chunk_max_bytes

        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "lots"
        self.assertEqual(chunk_max_bytes(), 512 * 1024)
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "0"
        self.assertEqual(chunk_max_bytes(), 512 * 1024)

    async def test_an_over_cap_chunk_is_refused_by_name(self) -> None:
        ws = fake_ws()
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "4096"
        tts = FatChunkTTS([1024, 200_000])
        spoken = await speak_sentence(ws, "t-cap", SENTENCE, 1, persona(), tts)
        self.assertIsNone(spoken, "an over-cap chunk must not produce a sentence")
        frames = sent_frames(ws)
        errors = [f for f in frames if f["type"] == "agent.error"]
        self.assertTrue(errors, f"no agent.error for the over-cap chunk: {frames}")
        self.assertEqual(errors[0]["reason"], "tts_chunk_too_large")
        sent_chunks = [f for f in frames if f["type"] == "agent.chunk"]
        self.assertEqual(
            len(sent_chunks), 1, "the over-cap chunk itself must never go on the wire"
        )

    async def test_an_under_cap_chunk_still_speaks(self) -> None:
        ws = fake_ws()
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = str(1024 * 1024)
        tts = FatChunkTTS([1024, 2048])
        spoken = await speak_sentence(ws, "t-cap-ok", SENTENCE, 1, persona(), tts)
        self.assertIsNotNone(spoken)
        self.assertEqual(
            len([f for f in sent_frames(ws) if f["type"] == "agent.chunk"]), 2
        )


class TestExactlyOneFinalChunk(unittest.IsolatedAsyncioTestCase):
    """`final` is the client's end-of-stream marker. `stream_chunks` owns it,
    so no backend's flag can send zero or three of them."""

    async def _chunks_for(self, tts, turn_id: str) -> list[dict]:
        ws = fake_ws()
        spoken = await speak_sentence(ws, turn_id, SENTENCE, 1, persona(), tts)
        self.assertIsNotNone(spoken, "the sentence must still be delivered")
        return [f for f in sent_frames(ws) if f["type"] == "agent.chunk"]

    async def test_a_backend_that_never_marks_final_still_gets_exactly_one(self) -> None:
        chunks = await self._chunks_for(LyingFinalTTS(3, [False, False, False]), "t-no-final")
        finals = [c for c in chunks if c["final"]]
        self.assertEqual(len(finals), 1, f"expected one final=true, got {chunks}")
        self.assertIs(finals[0], chunks[-1], "final=true must be the last chunk sent")

    async def test_a_backend_that_marks_every_chunk_final_gets_exactly_one(self) -> None:
        chunks = await self._chunks_for(LyingFinalTTS(3, [True, True, True]), "t-all-final")
        self.assertEqual(
            len([c for c in chunks if c["final"]]), 1, f"expected one final=true, got {chunks}"
        )

    async def test_an_honest_backend_is_unchanged(self) -> None:
        chunks = await self._chunks_for(StubChunkedTTS(per_chunk_s=0.0), "t-honest")
        finals = [c for c in chunks if c["final"]]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0]["audio_b64"], "no empty terminator was needed")
        self.assertIs(finals[0], chunks[-1])


class TestChunkPayloadCap(unittest.IsolatedAsyncioTestCase):
    """A chunk is base64'd into a WS frame. An untrusted backend that hands
    back a 40 MB "clause" would push it straight at the browser."""

    def tearDown(self) -> None:
        os.environ.pop("PET_TALK_CHUNK_MAX_BYTES", None)

    async def test_the_default_cap_is_512_kib(self) -> None:
        from server.speech import chunk_max_bytes

        os.environ.pop("PET_TALK_CHUNK_MAX_BYTES", None)
        self.assertEqual(chunk_max_bytes(), 512 * 1024)

    async def test_a_garbage_cap_falls_back_loudly(self) -> None:
        from server.speech import chunk_max_bytes

        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "lots"
        self.assertEqual(chunk_max_bytes(), 512 * 1024)
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "0"
        self.assertEqual(chunk_max_bytes(), 512 * 1024)

    async def test_an_over_cap_chunk_is_refused_by_name(self) -> None:
        ws = fake_ws()
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = "4096"
        tts = FatChunkTTS([1024, 200_000])
        spoken = await speak_sentence(ws, "t-cap", SENTENCE, 1, persona(), tts)
        self.assertIsNone(spoken, "an over-cap chunk must not produce a sentence")
        frames = sent_frames(ws)
        errors = [f for f in frames if f["type"] == "agent.error"]
        self.assertTrue(errors, f"no agent.error for the over-cap chunk: {frames}")
        self.assertEqual(errors[0]["reason"], "tts_chunk_too_large")
        sent_chunks = [f for f in frames if f["type"] == "agent.chunk"]
        self.assertEqual(
            len(sent_chunks), 1, "the over-cap chunk itself must never go on the wire"
        )

    async def test_an_under_cap_chunk_still_speaks(self) -> None:
        ws = fake_ws()
        os.environ["PET_TALK_CHUNK_MAX_BYTES"] = str(1024 * 1024)
        tts = FatChunkTTS([1024, 2048])
        spoken = await speak_sentence(ws, "t-cap-ok", SENTENCE, 1, persona(), tts)
        self.assertIsNotNone(spoken)
        self.assertEqual(
            len([f for f in sent_frames(ws) if f["type"] == "agent.chunk"]), 2
        )


class TestExactlyOneFinalChunk(unittest.IsolatedAsyncioTestCase):
    """`final` is the client's end-of-stream marker. `stream_chunks` owns it,
    so no backend's flag can send zero or three of them."""

    async def _chunks_for(self, tts, turn_id: str) -> list[dict]:
        ws = fake_ws()
        spoken = await speak_sentence(ws, turn_id, SENTENCE, 1, persona(), tts)
        self.assertIsNotNone(spoken, "the sentence must still be delivered")
        return [f for f in sent_frames(ws) if f["type"] == "agent.chunk"]

    async def test_a_backend_that_never_marks_final_still_gets_exactly_one(self) -> None:
        chunks = await self._chunks_for(LyingFinalTTS(3, [False, False, False]), "t-no-final")
        finals = [c for c in chunks if c["final"]]
        self.assertEqual(len(finals), 1, f"expected one final=true, got {chunks}")
        self.assertIs(finals[0], chunks[-1], "final=true must be the last chunk sent")

    async def test_a_backend_that_marks_every_chunk_final_gets_exactly_one(self) -> None:
        chunks = await self._chunks_for(LyingFinalTTS(3, [True, True, True]), "t-all-final")
        self.assertEqual(
            len([c for c in chunks if c["final"]]), 1, f"expected one final=true, got {chunks}"
        )

    async def test_an_honest_backend_is_unchanged(self) -> None:
        chunks = await self._chunks_for(StubChunkedTTS(per_chunk_s=0.0), "t-honest")
        finals = [c for c in chunks if c["final"]]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0]["audio_b64"], "no empty terminator was needed")
        self.assertIs(finals[0], chunks[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
