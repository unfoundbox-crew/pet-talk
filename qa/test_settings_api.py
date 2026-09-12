#!/usr/bin/env python3
"""qa/test_settings_api.py — unit tests for runtime tire switching via /settings API.

TDD contract:
  - GET /settings returns current runtime configuration and active provider types.
  - POST /settings updates provider settings and hot-swaps active stt, llm, and tts instances.
  - Re-querying GET /settings reflects the hot-swapped providers without restarting process.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestSettingsApi(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app import app

        self.client = TestClient(app)

    def test_get_settings(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("settings", data)
        self.assertIn("active", data)
        self.assertIn("stt", data["active"])
        self.assertIn("llm", data["active"])
        self.assertIn("tts", data["active"])

    def test_hot_swap_providers(self):
        # Update settings to litellm and stub tts
        payload = {
            "llm_provider": "litellm",
            "llm_base_url": "http://127.0.0.1:4000/v1",
            "llm_model": "claude-3-7-sonnet",
            # Fail-closed: without a key the swap lands as UnavailableLLM with a
            # named degraded reason, so the test supplies one explicitly.
            "llm_api_key": "test-key-not-real",
            "tts_provider": "stub",
            "stt_provider": "stub",
        }
        r = self.client.post("/settings", json=payload)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["active"]["llm"], "OpenAICompatibleLLM")
        self.assertEqual(data["active"]["tts"], "StubTTS")

        # Verify GET /settings reflects the update
        r2 = self.client.get("/settings")
        self.assertEqual(r2.status_code, 200)
        data2 = r2.json()
        self.assertEqual(data2["settings"]["llm_provider"], "litellm")
        self.assertEqual(data2["active"]["llm"], "OpenAICompatibleLLM")

        # Reset back to stub
        reset_payload = {
            "llm_provider": "stub",
            "tts_provider": "stub",
            "stt_provider": "stub",
        }
        self.client.post("/settings", json=reset_payload)


if __name__ == "__main__":
    unittest.main()
