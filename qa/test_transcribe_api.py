#!/usr/bin/env python3
"""qa/test_transcribe_api.py — unit tests for POST /transcribe REST API and user.text WS frame.

TDD contract:
  - POST /transcribe:
    - Accepts base64 encoded PCM audio payload ({"pcm_b64": str, "sample_rate": int}).
    - Decodes audio and returns {"ok": True, "text": transcribed_text}.
    - Rejects empty audio payloads with HTTP 400 and {"ok": False, "error": ...}.
    - Rejects invalid base64 encoding with HTTP 400 and {"ok": False, "error": ...}.
    - Catches ProviderError gracefully and returns {"ok": False, "error": ...}.
  - WebSocket /ws:
    - Accepts {"type": "user.text", "turn_id": str, "text": str} for typed prompt submission.
    - Emits {"type": "transcript.user", "turn_id": str, "text": str} mirroring the prompt.
    - Drives spoken reply through agent.stall/sentence -> agent.done.
    - Rejects empty text with {"type": "agent.error", "reason": "empty_text"}.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient
from server import app as server_module
from server.app import app
from server.providers import ProviderError, StubSTT


class TestTranscribeApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.old_stt = server_module.stt
        server_module.stt = StubSTT()
        # 320ms PCM16 mono @16kHz fixture
        self.pcm_bytes = bytes(320 * 2)
        self.pcm_b64 = base64.b64encode(self.pcm_bytes).decode("ascii")

    def tearDown(self):
        server_module.stt = self.old_stt

    def test_transcribe_valid_audio(self):
        r = self.client.post("/transcribe", json={"pcm_b64": self.pcm_b64, "sample_rate": 16000})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("text", data)
        self.assertEqual(data["text"], "Hello agent, what is the weather.")
        self.assertEqual(data.get("raw_text"), "hello agent, what is the weather")

    def test_transcribe_clean_prose_disabled(self):
        r = self.client.post("/transcribe", json={"pcm_b64": self.pcm_b64, "clean_prose": False})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["text"], "hello agent, what is the weather")

    def test_transcribe_audio_alias_key(self):
        # Supports alternative keys like audio_b64 or audio
        r = self.client.post("/transcribe", json={"audio_b64": self.pcm_b64})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(len(data.get("text", "")) > 0)

    def test_transcribe_empty_audio_payload(self):
        r = self.client.post("/transcribe", json={"pcm_b64": ""})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("error", data)

    def test_transcribe_missing_audio(self):
        r = self.client.post("/transcribe", json={})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok"))

    def test_transcribe_invalid_base64(self):
        r = self.client.post("/transcribe", json={"pcm_b64": "!!!not_valid_base64@@@"})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("invalid_base64", data.get("error", ""))

    def test_transcribe_provider_error(self):
        with patch("server.app.stt.transcribe", side_effect=ProviderError("stt_mock_failure", "simulated device failure")):
            r = self.client.post("/transcribe", json={"pcm_b64": self.pcm_b64})
            self.assertEqual(r.status_code, 502)
            data = r.json()
            self.assertFalse(data.get("ok"))
            self.assertIn("stt_mock_failure", data.get("error", ""))
            self.assertEqual(data.get("reason"), "stt_mock_failure")

    def test_ws_user_text_frame(self):
        with self.client.websocket_connect("/ws") as ws:
            idle_frame = ws.receive_json()
            self.assertEqual(idle_frame.get("type"), "state.idle")

            # Send typed prompt via user.text
            turn_id = "test-text-turn-01"
            prompt_text = "What is the status of the project?"
            ws.send_json({
                "type": "user.text",
                "turn_id": turn_id,
                "text": prompt_text,
                "persona": "donna",
            })

            # Check for user transcript reflection
            m = ws.receive_json()
            self.assertEqual(m.get("type"), "transcript.user")
            self.assertEqual(m.get("turn_id"), turn_id)
            self.assertEqual(m.get("text"), prompt_text)

            # Receive subsequent turn frames until agent.done
            received = []
            for _ in range(10):
                f = ws.receive_json()
                received.append(f.get("type"))
                if f.get("type") == "agent.done":
                    break

            self.assertIn("agent.done", received)

    def test_ws_user_text_empty_rejected(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.receive_json()  # idle
            ws.send_json({
                "type": "user.text",
                "turn_id": "test-empty-01",
                "text": "   ",
            })
            err_frame = ws.receive_json()
            self.assertEqual(err_frame.get("type"), "agent.error")
            self.assertEqual(err_frame.get("reason"), "empty_text")


if __name__ == "__main__":
    unittest.main()
