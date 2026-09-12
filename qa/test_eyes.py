#!/usr/bin/env python3
"""qa/test_eyes.py — eyes lane gates (WAVE3 §1, TECH-DESIGN Phase 4).

Hermetic: stdlib unittest + asyncio only. No WebSocket, no zrv subprocess,
no network. Frames are collected through the ``send=`` hook that
``eyes.handle_attach`` exposes for exactly this purpose.

Run: ``python3 qa/test_eyes.py -v``
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.eyes import (  # noqa: E402
    FLEET_VISION_MODEL_ENV,
    EyesConfig,
    EyesError,
    EyesProvider,
    StubEngine,
    ZrvEngine,
    handle_attach,
    make_engine,
    resolve_fleet_vision_alias,
    task_for,
)
from server import eyes as eyes_module  # noqa: E402

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


class Collector:
    """Fake ``send`` — records every frame the lane emits."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, payload: dict) -> bool:
        self.frames.append(payload)
        return True

    @property
    def types(self) -> list[str]:
        return [f["type"] for f in self.frames]

    def of(self, ftype: str) -> dict:
        for f in self.frames:
            if f["type"] == ftype:
                return f
        raise AssertionError(f"no {ftype} frame in {self.types}")


def cfg(**kw) -> EyesConfig:
    base = {
        "scratch_dir": os.path.join(tempfile.gettempdir(), "pet-talk-eyes-test"),
        "engine": "stub",
    }
    base.update(kw)
    return EyesConfig(**base)


def attach_frame(**kw) -> dict:
    f = {
        "type": "user.attach",
        "turn_id": "t1-abcdef",
        "ref": "att-1",
        "kind": "screenshot",
        "mime": "image/png",
        "b64": PNG_B64,
        "task": "transcribe",
    }
    f.update(kw)
    return f


def run(frame_in: dict, engine=None, conf: EyesConfig | None = None) -> Collector:
    conf = conf or cfg()
    prov = EyesProvider(conf, engine=engine if engine is not None else StubEngine())
    sink = Collector()
    asyncio.run(handle_attach(None, frame_in, None, send=sink, provider=prov))
    return sink


class TestValidation(unittest.TestCase):
    def test_too_large_fails_before_any_ack(self):
        big = base64.b64encode(b"x" * 4096).decode()
        sink = run(attach_frame(b64=big), conf=cfg(max_bytes=1024))
        self.assertEqual(sink.types, ["agent.error"])
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_too_large")
        self.assertEqual(sink.of("agent.error")["ref"], "att-1")

    def test_bad_kind(self):
        sink = run(attach_frame(kind="video"))
        self.assertEqual(sink.types, ["agent.error"])
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_bad_kind")
        self.assertEqual(sink.of("agent.error")["detail"], "video")

    def test_missing_b64_is_bad_kind(self):
        f = attach_frame()
        del f["b64"]
        sink = run(f)
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_bad_kind")

    def test_wave3_bytes_b64_alias_accepted(self):
        f = attach_frame()
        f["bytes_b64"] = f.pop("b64")
        sink = run(f)
        self.assertEqual(sink.types, ["eyes.received", "eyes.text"])

    def test_every_frame_carries_turn_id(self):
        sink = run(attach_frame())
        self.assertTrue(all(f.get("turn_id") == "t1-abcdef" for f in sink.frames))


class TestHappyPath(unittest.TestCase):
    def test_received_then_text(self):
        engine = StubEngine(text="INVOICE 42\nTOTAL 9.00")
        sink = run(attach_frame(), engine=engine)
        self.assertEqual(sink.types, ["eyes.received", "eyes.text"])
        rec = sink.of("eyes.received")
        self.assertEqual(rec["ref"], "att-1")
        self.assertEqual(rec["task"], "transcribe")
        txt = sink.of("eyes.text")
        self.assertEqual(txt["text"], "INVOICE 42\nTOTAL 9.00")
        self.assertFalse(txt["truncated"])
        self.assertEqual(txt["source"], "eyes:att-1:screenshot")
        self.assertEqual(txt["engine"], "stub")

    def test_no_text_when_engine_returns_blank(self):
        sink = run(attach_frame(), engine=StubEngine(text="   \n  "))
        self.assertEqual(sink.types, ["eyes.received", "agent.error"])
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_no_text")

    def test_scratch_file_is_deleted(self):
        conf = cfg()
        prov = EyesProvider(conf, engine=StubEngine())
        att = prov.stage(attach_frame(), None)
        self.assertTrue(os.path.isfile(att.path))
        task, text = asyncio.run(prov.resolve(att.ref))
        self.assertEqual(task, "transcribe")
        self.assertEqual(text, "STUB OCR TEXT")
        self.assertFalse(os.path.exists(att.path), "temp file must be purged after resolve")

    def test_context_tag_shape(self):
        prov = EyesProvider(cfg(), engine=StubEngine())
        att = prov.stage(attach_frame(kind="screenshot", task="describe"), None)
        self.assertEqual(
            prov.context_tag(att, "dialog, toggle off"),
            "[eyes:att-1:screenshot|describe] dialog, toggle off",
        )
        prov.discard(att.ref)


class TestTruncation(unittest.TestCase):
    def test_truncated_flag_and_cap(self):
        sink = run(attach_frame(), engine=StubEngine(text="a" * 50), conf=cfg(char_cap=20))
        txt = sink.of("eyes.text")
        self.assertTrue(txt["truncated"])
        self.assertEqual(len(txt["text"]), 20)

    def test_exact_cap_not_truncated(self):
        sink = run(attach_frame(), engine=StubEngine(text="b" * 20), conf=cfg(char_cap=20))
        self.assertFalse(sink.of("eyes.text")["truncated"])


class TestTimeout(unittest.TestCase):
    def test_slow_engine_times_out_with_named_reason(self):
        sink = run(attach_frame(), engine=StubEngine(delay_s=0.3), conf=cfg(timeout_s=0.05))
        self.assertEqual(sink.types, ["eyes.received", "agent.error"])
        err = sink.of("agent.error")
        self.assertEqual(err["reason"], "eyes_ocr_failed")
        self.assertEqual(err["detail"], "timeout")


class TestEngineAvailability(unittest.TestCase):
    def test_missing_binary_is_eyes_disabled(self):
        """Preflight runs BEFORE the ack: no eyes.received for a dead engine."""
        conf = cfg(zrv_bin="zrv-does-not-exist-pet-talk")
        sink = run(attach_frame(), engine=ZrvEngine(conf), conf=conf)
        self.assertEqual(sink.types, ["agent.error"])
        err = sink.of("agent.error")
        self.assertEqual(err["reason"], "eyes_disabled")
        self.assertIn("zrv_binary_missing", err["detail"])

    def test_unavailable_stub_is_eyes_disabled(self):
        sink = run(attach_frame(), engine=StubEngine(available=False))
        self.assertEqual(sink.types, ["agent.error"])
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_disabled")

    def test_unknown_engine_name_fails_closed(self):
        with self.assertRaises(EyesError) as ctx:
            make_engine("gpt4-eyes", cfg())
        self.assertEqual(ctx.exception.reason, "eyes_disabled")

    def test_make_engine_known_names(self):
        self.assertIsInstance(make_engine("stub", cfg()), StubEngine)
        self.assertIsInstance(make_engine("zrv", cfg()), ZrvEngine)


class TestZrvArgv(unittest.TestCase):
    """Argv shape only — no subprocess runs here (verified against zrv --help)."""

    def test_transcribe_argv_has_no_pin_by_default(self):
        conf = cfg(engine="zrv", zrv_bin="zrv")
        argv = ZrvEngine(conf).argv("/tmp/x.png", "transcribe", conf)
        self.assertEqual(argv[1:], ["ocr", "/tmp/x.png", "--task", "transcribe", "--json"])

    def test_describe_pin_unset_by_default(self):
        conf = cfg(engine="zrv")
        argv = ZrvEngine(conf).argv("/tmp/x.png", "describe", conf)
        self.assertNotIn("--engine", argv)
        self.assertIn("describe", argv)

    def test_describe_pin_is_honoured_when_set(self):
        conf = cfg(engine="zrv", describe_engine_pin="apple-fm")
        argv = ZrvEngine(conf).argv("/tmp/x.png", "describe", conf)
        self.assertIn("--engine", argv)
        self.assertEqual(argv[argv.index("--engine") + 1], "apple-fm")

    def test_transcribe_pin_is_honoured(self):
        conf = cfg(engine="zrv", engine_pin="tesseract")
        argv = ZrvEngine(conf).argv("/tmp/x.png", "transcribe", conf)
        self.assertEqual(argv[argv.index("--engine") + 1], "tesseract")


class Persona:
    def __init__(self, name="donna", **kw):
        self.name = name
        for k, v in kw.items():
            setattr(self, k, v)


class TestPersonaTaskMapping(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PET_TALK_DICTATION", None)

    def test_frame_task_wins(self):
        self.assertEqual(task_for(Persona(eyes_task="transcribe"), "image", "describe"), "describe")

    def test_pdf_is_always_transcribe(self):
        self.assertEqual(task_for(Persona(eyes_task="describe"), "pdf", None), "transcribe")

    def test_persona_frontmatter_field(self):
        self.assertEqual(task_for(Persona(eyes_task="transcribe"), "screenshot", None), "transcribe")
        self.assertEqual(task_for(Persona(eyes_default="transcribe"), "screenshot", None), "transcribe")

    def test_dictation_context_transcribes(self):
        os.environ["PET_TALK_DICTATION"] = "1"
        self.assertEqual(task_for(Persona(), "screenshot", None), "transcribe")

    def test_default_task_is_transcribe(self):
        """WAVE3 §1.2 default, and the only task inside the latency budget."""
        self.assertEqual(task_for(Persona(), "screenshot", None), "transcribe")
        self.assertEqual(task_for(None, "image", None), "transcribe")

    def test_config_can_default_to_describe(self):
        self.assertEqual(task_for(Persona(), "image", None, cfg(default_task="describe")), "describe")

    def test_invalid_requested_task_ignored(self):
        self.assertEqual(task_for(Persona(), "image", "summarise"), "transcribe")


class TestDescribeEngineGate(unittest.TestCase):
    """EYES_DESCRIBE_ENGINE gate (docs/ARCHITECTURE.md, item 3, 2026-09-12).

    Unset (default): describe fails fast with `eyes_disabled` naming the env
    var -- never a silent fallback to zrv's own default engine (apple-vision,
    which refuses --task describe with an unrelated error) or a 100s+ hang
    on apple-fm. Set: describe routes through to whatever engine is staged,
    same as transcribe -- the pin itself is another lane's job in
    zero-vision, this lane only has to route to it.
    """

    def test_describe_with_no_pin_fails_closed_naming_the_env_var(self):
        sink = run(attach_frame(task="describe"), conf=cfg(describe_engine_pin=None))
        err = sink.of("agent.error")
        self.assertEqual(err["reason"], "eyes_disabled")
        self.assertIn("EYES_DESCRIBE_ENGINE", err["detail"])
        # Fails before any OCR ran -- eyes.received still fires (payload was
        # accepted), but there is no eyes.text.
        self.assertIn("eyes.received", sink.types)
        self.assertNotIn("eyes.text", sink.types)

    def test_describe_with_pin_set_routes_through(self):
        sink = run(attach_frame(task="describe"), conf=cfg(describe_engine_pin="cloud-vlm"))
        text_frame = sink.of("eyes.text")
        self.assertEqual(text_frame["task"], "describe")
        self.assertNotIn("agent.error", sink.types)

    def test_transcribe_is_unaffected_by_the_gate(self):
        """The gate is describe-only -- transcribe never needs a pin."""
        sink = run(attach_frame(task="transcribe"), conf=cfg(describe_engine_pin=None))
        self.assertNotIn("agent.error", sink.types)
        self.assertEqual(sink.of("eyes.text")["task"], "transcribe")

    def test_pdf_with_no_explicit_task_is_forced_transcribe_so_the_gate_never_applies(self):
        """`task_for`: an explicit frame task wins even for a PDF; only the
        *implicit* PDF-is-always-transcribe default (no task on the frame)
        makes the describe gate moot here."""
        sink = run(attach_frame(kind="pdf", task=None), conf=cfg(describe_engine_pin=None))
        self.assertNotIn("agent.error", sink.types)
        self.assertEqual(sink.of("eyes.text")["task"], "transcribe")

    def test_pdf_with_explicit_describe_still_hits_the_gate(self):
        """An explicit `task: describe` on a PDF frame wins over the PDF
        default (task_for precedence: frame > kind) and so still needs
        EYES_DESCRIBE_ENGINE, same as any other kind."""
        sink = run(attach_frame(kind="pdf", task="describe"), conf=cfg(describe_engine_pin=None))
        err = sink.of("agent.error")
        self.assertEqual(err["reason"], "eyes_disabled")
        self.assertIn("EYES_DESCRIBE_ENGINE", err["detail"])


class TestFleetVisionAlias(unittest.TestCase):
    """The fleet/vision describe-route stub (docs/SPEC.md §8, lane 9)."""

    def tearDown(self):
        os.environ.pop(FLEET_VISION_MODEL_ENV, None)

    def test_non_alias_pin_passes_through_unchanged(self):
        self.assertEqual(resolve_fleet_vision_alias("apple-fm"), "apple-fm")
        self.assertIsNone(resolve_fleet_vision_alias(None))

    def test_unset_env_fails_closed_naming_the_var(self):
        os.environ.pop(FLEET_VISION_MODEL_ENV, None)
        with self.assertRaises(EyesError) as ctx:
            resolve_fleet_vision_alias("fleet/vision")
        self.assertEqual(ctx.exception.reason, "eyes_disabled")
        self.assertIn(FLEET_VISION_MODEL_ENV, ctx.exception.detail)

    def test_set_env_resolves_to_its_value(self):
        os.environ[FLEET_VISION_MODEL_ENV] = "cloud-vlm"
        self.assertEqual(resolve_fleet_vision_alias("fleet/vision"), "cloud-vlm")

    def test_describe_pinned_to_fleet_vision_routes_through_once_env_set(self):
        os.environ[FLEET_VISION_MODEL_ENV] = "cloud-vlm"
        sink = run(attach_frame(task="describe"), conf=cfg(describe_engine_pin="fleet/vision"))
        self.assertNotIn("agent.error", sink.types)
        self.assertEqual(sink.of("eyes.text")["task"], "describe")

    def test_describe_pinned_to_fleet_vision_fails_closed_when_env_unset(self):
        sink = run(attach_frame(task="describe"), conf=cfg(describe_engine_pin="fleet/vision"))
        err = sink.of("agent.error")
        self.assertEqual(err["reason"], "eyes_disabled")
        self.assertIn(FLEET_VISION_MODEL_ENV, err["detail"])


class TestContextQueue(unittest.TestCase):
    """The tagged context line queued for server/turn.py (lane 9)."""

    def test_resolved_attachment_queues_a_tagged_line(self):
        prov = EyesProvider(cfg(), engine=StubEngine(text="INVOICE 42"))
        sink = Collector()
        asyncio.run(handle_attach(None, attach_frame(), None, send=sink, provider=prov))
        pending = prov.take_context()
        self.assertEqual(len(pending), 1)
        tag, truncated = pending[0]
        self.assertEqual(tag, "[eyes:att-1:screenshot|transcribe] INVOICE 42")
        self.assertFalse(truncated)

    def test_take_context_drains_exactly_once(self):
        prov = EyesProvider(cfg(), engine=StubEngine(text="X"))
        sink = Collector()
        asyncio.run(handle_attach(None, attach_frame(), None, send=sink, provider=prov))
        first = prov.take_context()
        second = prov.take_context()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_context_line_capped_independently_of_the_frame_cap(self):
        conf = cfg(char_cap=4000, context_max_chars=10)
        prov = EyesProvider(conf, engine=StubEngine(text="a" * 50))
        sink = Collector()
        asyncio.run(handle_attach(None, attach_frame(), None, send=sink, provider=prov))
        # The eyes.text FRAME used the bigger char_cap, so it is not truncated.
        self.assertFalse(sink.of("eyes.text")["truncated"])
        # But the QUEUED prompt line used the small context_max_chars.
        tag, truncated = prov.take_context()[0]
        self.assertTrue(truncated)
        self.assertLessEqual(len(tag), 10)

    def test_failed_attach_queues_nothing(self):
        prov = EyesProvider(cfg(), engine=StubEngine(available=False))
        sink = Collector()
        asyncio.run(handle_attach(None, attach_frame(), None, send=sink, provider=prov))
        self.assertEqual(sink.of("agent.error")["reason"], "eyes_disabled")
        self.assertEqual(prov.take_context(), [])


PDF_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "eyes_sample.pdf")
with open(PDF_FIXTURE, "rb") as _f:
    PDF_B64 = base64.b64encode(_f.read()).decode()
PDF_FIXTURE_TEXT = "Pet Talk PDF fixture: the quick brown fox jumps over the lazy dog."

REAL_ENGINE = os.environ.get("PET_TALK_REAL_ENGINE") == "1"


class TestPdfOcr(unittest.TestCase):
    """PDF OCR (docs/SPEC.md Known gaps: 'PDF OCR is untested').

    A hermetic path (StubEngine, always runs) plus one real-engine path
    (`PET_TALK_REAL_ENGINE=1`, gated) that shells the actual `zrv` CLI on
    the fixture PDF above -- verified manually 2026-09-12:
    `zrv ocr qa/fixtures/eyes_sample.pdf --task transcribe --json` returns
    `{"ok": true, "engine": "apple-vision", "text": "Pet Talk PDF
    fixture: ..."}` in ~370ms. `zrv ocr file.pdf --json` reads the PDF's
    real text layer (not a rasterize-then-OCR path) -- apple-vision's OCR
    ran on the page text directly and returned an exact match, no OCR
    noise at all.
    """

    def test_hermetic_pdf_task_forced_transcribe_and_text_flows(self):
        sink = run(
            attach_frame(kind="pdf", mime="application/pdf", b64=PDF_B64, task=None),
            engine=StubEngine(text=PDF_FIXTURE_TEXT),
        )
        received = sink.of("eyes.received")
        self.assertEqual(received["kind"], "pdf")
        self.assertEqual(received["task"], "transcribe")  # no explicit task -> PDF's own default
        text_frame = sink.of("eyes.text")
        self.assertEqual(text_frame["task"], "transcribe")
        self.assertEqual(text_frame["text"], PDF_FIXTURE_TEXT)
        self.assertFalse(text_frame["truncated"])

    def test_hermetic_pdf_suffix_written_to_scratch(self):
        prov = EyesProvider(cfg(), engine=StubEngine())
        att = prov.stage(attach_frame(kind="pdf", mime="application/pdf", b64=PDF_B64), None)
        try:
            self.assertTrue(att.path.endswith(".pdf"))
            self.assertTrue(os.path.isfile(att.path))
            with open(att.path, "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF"))
        finally:
            prov.discard(att.ref)

    @unittest.skipUnless(REAL_ENGINE, "SKIP: PET_TALK_REAL_ENGINE!=1 — this shells the real zrv CLI")
    def test_real_zrv_engine_transcribes_the_fixture_pdf(self):
        conf = cfg(engine="zrv")
        sink = run(
            attach_frame(kind="pdf", mime="application/pdf", b64=PDF_B64, task="transcribe"),
            engine=ZrvEngine(conf),
            conf=conf,
        )
        self.assertNotIn("agent.error", sink.types)
        text_frame = sink.of("eyes.text")
        self.assertEqual(text_frame["engine"], "zrv")
        self.assertIn("Pet Talk PDF fixture", text_frame["text"])


class TestEyesContextReachesTheTurnPrompt(unittest.IsolatedAsyncioTestCase):
    """WAVE3 §1.2 / lane 9: server/turn.py drains server/eyes.py's queue.

    Hermetic — stub OCR engine, stub LLM, no socket, no zrv, no network. A
    real-engine variant below (gated on ``PET_TALK_REAL_ENGINE=1``) does the
    same thing with the actual ``zrv`` CLI against the PDF fixture.
    """

    def setUp(self):
        eyes_module.reset_provider()

    def tearDown(self):
        eyes_module.reset_provider()

    @staticmethod
    def _fake_ws():
        from unittest.mock import AsyncMock, MagicMock

        from starlette.websockets import WebSocketState

        ws = MagicMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.send_json = AsyncMock()
        return ws

    @staticmethod
    def _capturing_llm_and_providers():
        from server.provider_factory import ProviderSet
        from server.providers import LLMProvider, StubSTT, StubTTS

        class CapturingLLM(LLMProvider):
            def __init__(self) -> None:
                self.seen: list[list[dict]] = []

            def route(self, text: str) -> str:
                return "direct"

            async def stream(self, messages):
                self.seen.append(messages)
                yield "Noted."

        llm = CapturingLLM()
        return llm, ProviderSet(stt=StubSTT(), llm=llm, tts=StubTTS())

    async def test_turn_after_attach_sees_ocr_text_second_turn_does_not(self):
        from server.persona import Persona
        from server.speak_queue import SpeakQueue
        from server.turn import handle_turn

        eyes_module._provider = EyesProvider(
            cfg(),
            engine=StubEngine(text="INVOICE 42 TOTAL 9.00"),
        )
        sink = Collector()
        await handle_attach(None, attach_frame(), None, send=sink)
        self.assertEqual(sink.types, ["eyes.received", "eyes.text"])

        llm, providers = self._capturing_llm_and_providers()
        persona = Persona(
            name="default", voice="af_heart", speed=1.0, stalls=["One moment."], tone="Plain."
        )
        ws = self._fake_ws()

        await handle_turn(
            ws, "t-eyes-1", "what does it say", SpeakQueue(),
            active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 1)
        first_prompt = llm.seen[0][0]["content"]
        # The OCR text reaches the prompt, but FENCED and escaped: the `[` of
        # our own tag is escaped too, because inside the fence nothing can be
        # told apart from what the picture said (server/persona_runtime.py).
        from server.persona_runtime import (
            OCR_FENCE_CLOSE,
            OCR_FENCE_OPEN,
            OCR_PREAMBLE,
        )

        self.assertIn(OCR_PREAMBLE, first_prompt)
        body = first_prompt[
            first_prompt.index(OCR_FENCE_OPEN) : first_prompt.index(OCR_FENCE_CLOSE)
        ]
        self.assertIn("eyes:att-1:screenshot|transcribe", body)
        self.assertIn("INVOICE 42 TOTAL 9.00", body)
        self.assertEqual(first_prompt.count("INVOICE 42 TOTAL 9.00"), 1)

        await handle_turn(
            ws, "t-eyes-2", "anything else", SpeakQueue(),
            active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 2)
        second_prompt = llm.seen[1][0]["content"]
        self.assertNotIn("EYES CONTEXT", second_prompt)
        self.assertNotIn("INVOICE 42", second_prompt)

    async def test_a_control_turn_consumes_the_context_it_does_not_use(self):
        """One attachment, exactly one turn — even when that turn is a control.

        The drain used to sit AFTER the control and empty-transcript returns,
        so "status" or a blank transcript left the OCR text in the queue and a
        LATER, unrelated turn picked it up. The picture the user attached would
        have reached a question asked minutes afterwards.
        """
        from server.persona import Persona
        from server.speak_queue import SpeakQueue
        from server.turn import handle_turn

        eyes_module._provider = EyesProvider(
            cfg(), engine=StubEngine(text="INVOICE 42 TOTAL 9.00")
        )
        await handle_attach(None, attach_frame(), None, send=Collector())

        llm, providers = self._capturing_llm_and_providers()
        persona = Persona(
            name="default", voice="af_heart", speed=1.0, stalls=["One moment."],
            tone="Plain.",
        )
        ws = self._fake_ws()

        # 1. A control turn. It builds no prompt at all, so the context cannot
        #    reach the model here — and must not survive to the next turn.
        await handle_turn(
            ws, "t-ctl", "status", SpeakQueue(),
            active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 0, "a control turn called the LLM")

        # 2. An empty transcript. Same contract.
        await handle_turn(
            ws, "t-empty", "   ", SpeakQueue(),
            active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 0, "an empty transcript called the LLM")

        # 3. A real turn, later. The OCR text is gone.
        await handle_turn(
            ws, "t-real", "anything else", SpeakQueue(),
            active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 1)
        prompt = llm.seen[0][0]["content"]
        self.assertNotIn("INVOICE 42", prompt,
                         "the OCR text leaked into a later, unrelated turn")
        from server.persona_runtime import OCR_FENCE_OPEN

        self.assertNotIn(OCR_FENCE_OPEN, prompt)

    async def test_a_barge_before_the_turn_starts_clears_the_queue(self):
        """A discarded turn discards its context, with a named reason.

        ``handle_turn_task`` returns before the pipeline when a barge already
        marked the id. Nothing drained the queue on that path, so the next turn
        inherited an attachment the user had already abandoned.
        """
        from server.speak_queue import SpeakQueue
        from server.turn import handle_turn_task

        eyes_module._provider = EyesProvider(
            cfg(), engine=StubEngine(text="INVOICE 42 TOTAL 9.00")
        )
        await handle_attach(None, attach_frame(), None, send=Collector())
        self.assertTrue(eyes_module.get_provider()._pending_context,
                        "fixture failed: nothing was queued to lose")

        ws = self._fake_ws()
        await handle_turn_task(
            ws, "t-barged", "what does it say", SpeakQueue(), {},
            barged={"t-barged"},
        )
        self.assertEqual(
            eyes_module.get_provider().take_context(), [],
            "a barged turn left its OCR context for the next turn to inherit",
        )

    @unittest.skipUnless(REAL_ENGINE, "SKIP: PET_TALK_REAL_ENGINE!=1 — this shells the real zrv CLI")
    async def test_real_zrv_attach_then_turn_reaches_the_stub_llm_prompt(self):
        """Real zrv OCRs the PDF fixture; the LLM stays stub (turn budget)."""
        from server.persona import Persona
        from server.speak_queue import SpeakQueue
        from server.telemetry import TurnLog
        from server.turn import handle_turn

        conf = cfg(engine="zrv")
        eyes_module._provider = EyesProvider(conf, engine=ZrvEngine(conf))
        sink = Collector()
        await handle_attach(
            None,
            attach_frame(kind="pdf", mime="application/pdf", b64=PDF_B64, task="transcribe"),
            None,
            send=sink,
        )
        self.assertNotIn("agent.error", sink.types)
        self.assertIn(PDF_FIXTURE_TEXT, sink.of("eyes.text")["text"])

        llm, providers = self._capturing_llm_and_providers()
        persona = Persona(
            name="default", voice="af_heart", speed=1.0, stalls=["One moment."], tone="Plain."
        )
        log_ = TurnLog(path=os.path.join(tempfile.gettempdir(), "pet-talk-eyes-test-turns.jsonl"))
        log_.start("t-eyes-real", {})
        await handle_turn(
            self._fake_ws(), "t-eyes-real", "what does the pdf say", SpeakQueue(),
            log_=log_, active_persona=persona, providers=providers,
        )
        self.assertEqual(len(llm.seen), 1)
        self.assertIn(PDF_FIXTURE_TEXT, llm.seen[0][0]["content"])
        eyes_ocr_ms = log_._stages.get("eyes_ocr_ms")
        self.assertIsNotNone(eyes_ocr_ms, "TurnLog must record eyes_ocr_ms for an attach turn")
        print(f"eyes_ocr_ms={eyes_ocr_ms}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
