#!/usr/bin/env python3
"""qa/test_handover.py — the hand-over chord frame (user.handover) is acknowledged
and opens the mic. Hermetic: stub providers, TestClient WebSocket, studio token."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k in ("STT_PROVIDER", "TTS_PROVIDER", "LLM_PROVIDER"):
    os.environ.setdefault(k, "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")

from fastapi.testclient import TestClient  # noqa: E402
from server import app as server_module  # noqa: E402
from server import auth  # noqa: E402


class TestHandoverFrame(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server_module.app)
        self.token = auth.studio_token()

    def test_handover_is_acked_then_listening(self):
        with self.client.websocket_connect(f"/ws?token={self.token}") as ws:
            first = ws.receive_json()
            self.assertEqual(first["type"], "state.idle")
            ws.send_json({"type": "user.handover", "turn_id": "t-ho-1", "source": "hotkey"})
            ack = ws.receive_json()
            self.assertEqual(ack["type"], "handover.received")
            self.assertEqual(ack["turn_id"], "t-ho-1")
            self.assertEqual(ack["source"], "hotkey")
            nxt = ws.receive_json()
            self.assertEqual(nxt["type"], "state.listening")
            self.assertEqual(nxt["turn_id"], "t-ho-1")

    def test_handover_without_turn_id_gets_one(self):
        with self.client.websocket_connect(f"/ws?token={self.token}") as ws:
            ws.receive_json()
            ws.send_json({"type": "user.handover"})
            ack = ws.receive_json()
            self.assertEqual(ack["type"], "handover.received")
            self.assertTrue(ack["turn_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
