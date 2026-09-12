#!/usr/bin/env python3
"""qa/test_turn_lifecycle.py — the turn lifecycle contract, hermetically.

What this proves (no network, no daemon, no audio device, no sound):

1. Barge during a slow TTS cancels the in-flight turn in under 100ms and the
   ack reports the TRUE dropped count (>= 2).
2. The LLM producer buffers at least 3 sentences ahead of TTS when it is the
   faster side (TECH-SPEC section 9, gate 3).
3. An unknown persona name is an error (``unknown_persona``), never a blank
   default that quietly speaks as somebody else.
4. ``GET /settings`` redacts every credential.
5. A malformed frame yields ``agent.error reason=bad_frame`` and the socket
   survives to serve the next frame.

Run: ``python3 qa/test_turn_lifecycle.py``
"""
from __future__ import annotations

import asyncio
import io
import os
import struct
import sys
import time
import unittest
import wave
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Stub tyres before the app builds its provider set — no downloads, no keys.
os.environ.setdefault("STT_PROVIDER", "stub")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("TTS_PROVIDER", "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")
os.environ.pop("PET_TALK_AX", None)

from starlette.websockets import WebSocketState

from server.persona import Persona
from server.provider_factory import ProviderSet
# Public provider surface only — server/providers is becoming a package, so
# nothing here may reach for an underscore-prefixed internal.
from server.providers import LLMProvider, ProviderError, StubSTT, TTSProvider
from server.persona_runtime import build_system_prompt, resolve_persona
from server.speak_queue import SpeakQueue
from server.turn import handle_turn_task

BARGE_BUDGET_S = 0.100
BUFFER_AHEAD_TARGET = 3


def tiny_wav(duration_s: float = 0.05, sample_rate: int = 8000) -> bytes:
    """A silent WAV, built here so the test needs no provider internals."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<h", 0) * int(duration_s * sample_rate))
    return buf.getvalue()


# ------------------------------------------------------------- test tyres ---


class SlowTTS(TTSProvider):
    """Synthesizes slowly, and blocks the calling thread while doing it.

    ``time.sleep`` (not ``asyncio.sleep``) is deliberate: it is exactly the
    blocking provider the server must keep off the event loop.
    """

    def __init__(self, delay_s: float = 0.4) -> None:
        self.delay_s = delay_s
        self.base_url = "test://slow-tts"
        self.calls = 0

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0):
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        self.calls += 1
        time.sleep(self.delay_s)
        return tiny_wav(), []


class FastLLM(LLMProvider):
    """Streams several sentences immediately so the queue must buffer ahead."""

    def __init__(self, sentences: list[str], path: str = "stall") -> None:
        self.sentences = sentences
        self.path = path

    def route(self, text: str) -> str:
        return self.path

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        for sentence in self.sentences:
            await asyncio.sleep(0)  # yield without pacing
            yield sentence


class EmptyLLM(LLMProvider):
    """Streams nothing at all — the fail-closed case for llm_no_sentences."""

    def __init__(self, path: str = "direct") -> None:
        self.path = path

    def route(self, text: str) -> str:
        return self.path

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        if False:  # pragma: no cover - an async generator that yields nothing
            yield ""


def fake_ws() -> MagicMock:
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def sent_frames(ws: MagicMock) -> list[dict]:
    return [call.args[0] for call in ws.send_json.await_args_list]


def _drain_to_done(ws, limit: int = 24) -> bool:
    """Read frames until agent.done so no turn is live at context exit."""
    for _ in range(limit):
        if ws.receive_json().get("type") == "agent.done":
            return True
    return False


TEST_PERSONA = Persona(
    name="default",
    voice="af_heart",
    speed=1.0,
    stalls=["One moment."],
    tone="Plain and brief.",
)


# ------------------------------------------------------------- barge/queue ---


class TestBargeCancelsInFlightTurn(unittest.IsolatedAsyncioTestCase):
    async def _run_until_buffered(self, queue: SpeakQueue, target: int, timeout_s: float = 2.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if queue.high_water >= target:
                return True
            await asyncio.sleep(0.005)
        return False

    async def test_barge_cancels_within_budget_and_counts_dropped(self):
        ws = fake_ws()
        tts = SlowTTS(delay_s=0.4)
        providers = ProviderSet(
            stt=StubSTT(),
            llm=FastLLM(
                [
                    "First sentence of the answer.",
                    "Second sentence of the answer.",
                    "Third sentence of the answer.",
                    "Fourth sentence of the answer.",
                ]
            ),
            tts=tts,
        )
        queue = SpeakQueue()
        turn_tasks: dict = {}

        turn = asyncio.create_task(
            handle_turn_task(
                ws,
                "t-barge-1",
                "research the weather",
                queue,
                turn_tasks,
                active_persona=TEST_PERSONA,
                providers=providers,
            )
        )
        # Let the LLM run ahead of the slow TTS.
        buffered = await self._run_until_buffered(queue, BUFFER_AHEAD_TARGET)
        self.assertTrue(
            buffered,
            f"producer never buffered {BUFFER_AHEAD_TARGET} ahead "
            f"(high_water={queue.high_water})",
        )

        # Barge: cancel the live turn task, then flush. Same order as ws.py.
        started = time.monotonic()
        live = turn_tasks.get("t-barge-1")
        self.assertIsNotNone(live, "turn task was never registered for barge")
        live.cancel()
        dropped = await queue.flush()
        await asyncio.wait_for(asyncio.shield(turn), timeout=1.0)
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            BARGE_BUDGET_S,
            f"barge took {elapsed * 1000:.1f}ms, budget is {BARGE_BUDGET_S * 1000:.0f}ms",
        )
        self.assertGreaterEqual(
            dropped, 2, f"barge under-reported dropped sentences: {dropped}"
        )
        types = [f.get("type") for f in sent_frames(ws)]
        self.assertIn("agent.done", types)
        interrupted = [
            f for f in sent_frames(ws)
            if f.get("type") == "agent.done" and f.get("path") == "interrupted"
        ]
        self.assertTrue(interrupted, f"no interrupted agent.done in {types}")
        print(
            f"    barge: {elapsed * 1000:.1f}ms, dropped={dropped}, "
            f"high_water={queue.high_water}"
        )

    async def test_producer_buffers_three_sentences_ahead(self):
        ws = fake_ws()
        providers = ProviderSet(
            stt=StubSTT(),
            llm=FastLLM([f"Sentence number {i} here." for i in range(1, 6)]),
            tts=SlowTTS(delay_s=0.05),
        )
        queue = SpeakQueue()
        turn_tasks: dict = {}
        await handle_turn_task(
            ws,
            "t-buffer-1",
            "research the weather",
            queue,
            turn_tasks,
            active_persona=TEST_PERSONA,
            providers=providers,
        )
        self.assertGreaterEqual(
            queue.high_water,
            BUFFER_AHEAD_TARGET,
            f"queue only ever held {queue.high_water} sentences; "
            f"the producer is not running ahead of TTS",
        )
        spoken = [f for f in sent_frames(ws) if f.get("type") == "agent.sentence"]
        self.assertGreaterEqual(len(spoken), 5, "worker path did not speak every sentence")
        for f in spoken[1:]:  # seq 0 is the stall phrase
            self.assertIn("word_times", f, "agent.sentence carries no word timings")
            self.assertIn("estimated", f, "agent.sentence does not flag estimated timings")
        print(f"    buffer ahead: high_water={queue.high_water}, spoken={len(spoken)}")


class TestEmptyLlmStreamFailsClosed(unittest.IsolatedAsyncioTestCase):
    """Zero sentences is a failure with a name, never a quiet agent.done."""

    async def _run(self, path: str):
        ws = fake_ws()
        providers = ProviderSet(
            stt=StubSTT(), llm=EmptyLLM(path=path), tts=SlowTTS(delay_s=0.01)
        )
        await handle_turn_task(
            ws,
            f"t-empty-{path}",
            "what is the weather",
            SpeakQueue(),
            {},
            active_persona=TEST_PERSONA,
            providers=providers,
        )
        return sent_frames(ws)

    async def test_direct_path_reports_llm_no_sentences(self):
        frames = await self._run("direct")
        errors = [f for f in frames if f.get("type") == "agent.error"]
        self.assertTrue(errors, [f.get("type") for f in frames])
        self.assertEqual(errors[-1].get("reason"), "llm_no_sentences")
        types = [f.get("type") for f in frames]
        self.assertLess(
            types.index("agent.error"),
            types.index("agent.done"),
            "the reason must arrive before agent.done",
        )

    async def test_worker_path_reports_llm_no_sentences(self):
        frames = await self._run("stall")
        reasons = [f.get("reason") for f in frames if f.get("type") == "agent.error"]
        self.assertIn("llm_no_sentences", reasons, reasons)
        done = [f for f in frames if f.get("type") == "agent.done"]
        self.assertEqual(done[-1].get("sentences"), 0)


class TestBargeBeforeTheTurnRegisters(unittest.IsolatedAsyncioTestCase):
    """A barge that lands before the turn task registers must still kill it.

    Registration used to happen inside the turn coroutine, after an await, so
    a barge in that window found ``turn_tasks`` empty, cancelled nothing, and
    the turn went on speaking after the ack.
    """

    async def _session(self, providers):
        from server import ws as ws_module

        session = ws_module.Session(ws=fake_ws())
        self._patched = ws_module
        self._old_snapshot = ws_module.runtime.snapshot_under_lock

        async def _snapshot():
            return providers

        ws_module.runtime.snapshot_under_lock = _snapshot
        self.addCleanup(
            setattr, ws_module.runtime, "snapshot_under_lock", self._old_snapshot
        )
        return session

    async def test_barge_with_no_yield_between_cancels_the_turn(self):
        from server import ws as ws_module

        providers = ProviderSet(
            stt=StubSTT(),
            llm=FastLLM(["First answer sentence.", "Second answer sentence."]),
            tts=SlowTTS(delay_s=0.3),
        )
        session = await self._session(providers)
        turn_id = "t-race-1"

        # No await between the two handlers beyond what the handlers do
        # themselves: this is exactly the reader loop's tightest window.
        await ws_module._on_user_text(
            session, {"type": "user.text", "turn_id": turn_id, "text": "research this"}
        )
        await ws_module._on_barge(session, {"type": "barge", "turn_id": turn_id})

        self.assertEqual(
            session.last_barge_cancelled,
            1,
            "barge cancelled nothing — the turn task was not registered in time",
        )
        ack_index = max(
            i for i, f in enumerate(sent_frames(session.ws))
            if f.get("type") == "state.listening" and "barged_turn" in f
        )
        # Let anything still running have its chance to misbehave.
        await asyncio.sleep(0.05)
        await session.shutdown()
        after_ack = sent_frames(session.ws)[ack_index + 1:]
        self.assertEqual(
            [f for f in after_ack if f.get("type") == "agent.sentence"],
            [],
            f"a barged turn spoke after the ack: {[f.get('type') for f in after_ack]}",
        )

    async def test_a_barge_marked_id_never_speaks_even_with_no_task(self):
        """The other half: ``barged`` catches the turn whose task has not run."""
        providers = ProviderSet(
            stt=StubSTT(),
            llm=FastLLM(["Should never be spoken."]),
            tts=SlowTTS(delay_s=0.01),
        )
        ws = fake_ws()
        turn_tasks: dict = {}
        barged = {"t-race-2"}
        await handle_turn_task(
            ws,
            "t-race-2",
            "research this",
            SpeakQueue(),
            turn_tasks,
            active_persona=TEST_PERSONA,
            providers=providers,
            barged=barged,
        )
        types = [f.get("type") for f in sent_frames(ws)]
        self.assertNotIn("agent.sentence", types, types)
        self.assertIn("agent.done", types, types)
        self.assertEqual(
            [f.get("path") for f in sent_frames(ws) if f.get("type") == "agent.done"],
            ["interrupted"],
        )
        self.assertNotIn("t-race-2", barged, "barged id was not cleared after the turn")


class TestBoundedPerTurnState(unittest.IsolatedAsyncioTestCase):
    """Nothing keyed on client input may grow without a ceiling."""

    async def test_two_hundred_turns_bound_the_stall_cache_and_personas(self):
        from server import stall as stall_module
        from server import ws as ws_module

        cap = stall_module.stall_cache_max()
        session = ws_module.Session(ws=fake_ws())
        providers = ProviderSet(
            stt=StubSTT(),
            llm=FastLLM(["One short answer."], path="stall"),
            tts=SlowTTS(delay_s=0.0),
        )
        old_snapshot = ws_module.runtime.snapshot_under_lock

        async def _snapshot():
            return providers

        ws_module.runtime.snapshot_under_lock = _snapshot
        self.addCleanup(
            setattr, ws_module.runtime, "snapshot_under_lock", old_snapshot
        )

        for i in range(200):
            turn_id = f"t-mem-{i}"
            await ws_module._on_user_text(
                session,
                {
                    "type": "user.text",
                    "turn_id": turn_id,
                    "text": "research the weather",
                    # A different float every turn: the old cache key space.
                    "custom_speed": 0.5 + (i * 0.0037),
                    "custom_voice": f"voice-{i}",
                },
            )
            task = session.turn_tasks.get(turn_id)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)

        self.assertLessEqual(
            stall_module.stall_cache_size(),
            cap,
            f"stall cache grew past its cap: {stall_module.stall_cache_size()} > {cap}",
        )
        self.assertLessEqual(
            len(session.turn_persona),
            1,
            f"turn_persona leaked {len(session.turn_persona)} entries across 200 turns",
        )
        self.assertLessEqual(len(session.turn_tasks), 1)
        self.assertLessEqual(len(session.barged), ws_module.MAX_BARGED_IDS)
        print(
            f"    200 turns: stall_cache={stall_module.stall_cache_size()} (cap {cap}), "
            f"turn_persona={len(session.turn_persona)}"
        )

    def test_client_speed_is_quantised_and_clamped(self):
        from server.persona_runtime import SPEED_MAX, SPEED_MIN, clamp_speed

        self.assertEqual(clamp_speed(0.01), SPEED_MIN)
        self.assertEqual(clamp_speed(99.0), SPEED_MAX)
        self.assertEqual(clamp_speed(1.234), 1.25)
        distinct = {clamp_speed(0.5 + i * 0.0037) for i in range(200)}
        self.assertLessEqual(
            len(distinct), 15, f"speed key space is not finite: {sorted(distinct)}"
        )


class TestSpeakQueueMechanics(unittest.IsolatedAsyncioTestCase):
    async def test_flush_counts_queued_and_inflight(self):
        q = SpeakQueue()
        await q.push("one")
        await q.push("two")
        await q.push("three")
        self.assertEqual(await q.size(), 3)
        self.assertEqual(await q.get(), "one")  # now in flight
        self.assertEqual(await q.flush(), 3, "flush must count the in-flight sentence")
        self.assertEqual(await q.size(), 0)

    async def test_size_replaces_async_dunder_len(self):
        q = SpeakQueue()
        self.assertFalse(
            hasattr(type(q), "__len__"),
            "SpeakQueue must not define __len__ (an async one is always a bug)",
        )
        await q.push("only")
        self.assertEqual(await q.size(), 1)

    async def test_resume_from_uses_word_times(self):
        q = SpeakQueue()
        times = [
            {"word": "alpha", "start_ms": 0, "end_ms": 300, "estimated": False},
            {"word": "beta", "start_ms": 300, "end_ms": 600, "estimated": False},
            {"word": "gamma", "start_ms": 600, "end_ms": 900, "estimated": False},
        ]
        point = await q.resume_from(1, "alpha beta gamma", word_times=times)
        self.assertEqual(point.text, "beta gamma")
        self.assertEqual(point.start_ms, 300)
        self.assertFalse(point.estimated)

        estimated = await q.resume_from(1, "alpha beta gamma")
        self.assertEqual(estimated.text, "beta gamma")
        self.assertTrue(estimated.estimated, "estimated timings must be flagged")

        with self.assertRaises(ProviderError) as ctx:
            await q.resume_from(99, "alpha beta gamma")
        self.assertEqual(ctx.exception.reason, "queue_bad_word_idx")


# ----------------------------------------------------------------- persona ---


class TestPersonaIdentity(unittest.TestCase):
    def test_unknown_persona_fails_closed(self):
        with self.assertRaises(ProviderError) as ctx:
            resolve_persona("nobody-by-that-name")
        self.assertEqual(ctx.exception.reason, "unknown_persona")

    def test_known_persona_resolves(self):
        self.assertEqual(resolve_persona("donna").name, "donna")
        self.assertEqual(resolve_persona("jarvis").name, "jarvis")

    def test_prompt_carries_only_the_active_persona(self):
        jarvis = resolve_persona("jarvis")
        prompt = build_system_prompt(jarvis, "Voice loop: test.")
        self.assertIn("jarvis", prompt.lower())
        self.assertNotIn("donna", prompt.lower(), "another persona leaked into the prompt")

    def test_neutral_prompt_for_persona_without_instruction_spec(self):
        p = Persona(name="researcher", tone="Precise and evidence-backed.")
        prompt = build_system_prompt(p, "")
        self.assertIn("You are researcher.", prompt)
        self.assertIn("Precise and evidence-backed.", prompt)
        self.assertNotIn("Donna", prompt)


# -------------------------------------------------------------- HTTP + WS ---


class TestSettingsRedaction(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app import app
        from server.auth import STUDIO_TOKEN_HEADER, studio_token

        self.client = TestClient(app, headers={STUDIO_TOKEN_HEADER: studio_token()})

    def tearDown(self):
        self.client.post("/settings", json={"deepgram_api_key": ""})

    def test_get_settings_redacts_every_credential(self):
        self.client.post("/settings", json={"deepgram_api_key": "dg-not-a-real-key"})
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for name, value in data["settings"].items():
            if any(h in name.lower() for h in ("key", "token", "secret", "password")):
                self.assertIn(
                    value, ("***", ""), f"{name} leaked a credential: {value!r}"
                )
        self.assertTrue(data["secrets_set"]["deepgram_api_key"])
        self.assertEqual(data["settings"]["deepgram_api_key"], "***")
        # The whole body must not contain the value we just set.
        self.assertNotIn("dg-not-a-real-key", r.text)

    def test_mask_roundtrip_does_not_wipe_a_key(self):
        self.client.post("/settings", json={"deepgram_api_key": "dg-keep-me"})
        self.client.post("/settings", json={"deepgram_api_key": "***"})
        r = self.client.get("/settings")
        self.assertTrue(r.json()["secrets_set"]["deepgram_api_key"])

    def test_health_reports_providers_and_degraded(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for key in ("ok", "service", "version", "providers", "degraded"):
            self.assertIn(key, data)
        for tyre in ("stt", "llm", "tts"):
            self.assertIn(tyre, data["providers"])
        self.assertIsInstance(data["degraded"], list)


class TestSocketSurvivesBadFrames(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app import app
        from server.auth import STUDIO_TOKEN_HEADER, studio_token

        self.client = TestClient(app, headers={STUDIO_TOKEN_HEADER: studio_token()})

    def test_bad_frame_does_not_drop_the_socket(self):
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json().get("type"), "state.idle")

            # Unparseable sample_rate on an otherwise valid stop frame.
            ws.send_json(
                {
                    "type": "user.stop",
                    "turn_id": "t-bad-rate",
                    "pcm_b64": "AAAA",
                    "sample_rate": "sixteen thousand",
                }
            )
            err = ws.receive_json()
            self.assertEqual(err.get("type"), "agent.error")
            self.assertEqual(err.get("reason"), "bad_frame")

            # Not a known frame type.
            ws.send_json({"type": "agent.teleport", "turn_id": "t-bad-type"})
            err = ws.receive_json()
            self.assertEqual(err.get("reason"), "unknown_frame")

            # Socket still serves the next real frame. Drained to agent.done so
            # the turn is not in flight when the client context exits.
            ws.send_json({"type": "user.text", "turn_id": "t-after-bad", "text": "hello"})
            following = ws.receive_json()
            self.assertEqual(following.get("type"), "transcript.user")
            self.assertEqual(following.get("turn_id"), "t-after-bad")
            self.assertTrue(_drain_to_done(ws), "turn never reached agent.done")

    def test_unknown_persona_errors_over_the_socket(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.receive_json()  # state.idle
            ws.send_json(
                {
                    "type": "user.text",
                    "turn_id": "t-ghost",
                    "text": "hello",
                    "persona": "not-a-persona",
                }
            )
            err = ws.receive_json()
            self.assertEqual(err.get("type"), "agent.error")
            self.assertEqual(err.get("reason"), "unknown_persona")

    def test_attach_without_eyes_module_is_named(self):
        import importlib.util

        if importlib.util.find_spec("server.eyes") is not None:
            self.skipTest("SKIP: server.eyes has landed — eyes_disabled no longer applies")
        with self.client.websocket_connect("/ws") as ws:
            ws.receive_json()  # state.idle
            ws.send_json(
                {
                    "type": "user.attach",
                    "turn_id": "t-eyes",
                    "ref": "att-1",
                    "kind": "screenshot",
                    "mime": "image/png",
                    "b64": "AAAA",
                    "task": "describe",
                }
            )
            err = ws.receive_json()
            self.assertEqual(err.get("type"), "agent.error")
            self.assertEqual(err.get("reason"), "eyes_disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
