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
    EyesConfig,
    EyesError,
    EyesProvider,
    StubEngine,
    ZrvEngine,
    handle_attach,
    make_engine,
    task_for,
)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
