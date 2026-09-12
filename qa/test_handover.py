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


class TestHandoverIsConsumedByTheNextTurn(unittest.TestCase):
    """`Session.pending_handover` was write-only: set by the chord, read by
    nobody, so a handed-over turn was indistinguishable from a normal one."""

    def setUp(self):
        self.client = TestClient(server_module.app)
        self.token = auth.studio_token()

    def test_the_next_user_text_carries_handover_and_clears_the_flag(self):
        from server import ws as ws_module

        with self.client.websocket_connect(f"/ws?token={self.token}") as ws:
            ws.receive_json()  # state.idle
            ws.send_json({"type": "user.handover", "turn_id": "t-ho-3", "source": "hotkey"})
            ws.receive_json()  # handover.received
            ws.receive_json()  # state.listening

            ws.send_json({"type": "user.text", "turn_id": "t-ho-3b", "text": "ship the fix"})
            transcript = ws.receive_json()
            self.assertEqual(transcript["type"], "transcript.user")
            self.assertTrue(
                transcript.get("handover"),
                f"transcript.user does not carry handover: {transcript}",
            )
            for _ in range(24):
                if ws.receive_json().get("type") == "agent.done":
                    break

            # A second turn is ordinary work again — the flag is consumed once.
            ws.send_json({"type": "user.text", "turn_id": "t-ho-3c", "text": "and again"})
            second = ws.receive_json()
            self.assertEqual(second["type"], "transcript.user")
            self.assertFalse(
                second.get("handover"),
                f"handover leaked into the next turn: {second}",
            )
            for _ in range(24):
                if ws.receive_json().get("type") == "agent.done":
                    break

    def test_a_handover_turn_gets_the_delegated_work_line_in_its_prompt(self):
        from server.persona import Persona
        from server.persona_runtime import HANDOVER_LINE, build_system_prompt

        p = Persona(
            name="default", voice="af_heart", speed=1.0,
            stalls=["One moment."], tone="Plain and brief.",
        )
        plain = build_system_prompt(p, "")
        handed = build_system_prompt(p, "", handover=True)
        self.assertNotIn(HANDOVER_LINE, plain)
        self.assertIn(HANDOVER_LINE, handed)
        self.assertIn("state what you will do", HANDOVER_LINE)
        self.assertIn("confirm the target file or repo", HANDOVER_LINE)

    def test_the_line_also_reaches_a_persona_with_its_own_instruction_spec(self):
        from server.persona import Persona
        from server.persona_runtime import HANDOVER_LINE, build_system_prompt

        p = Persona(
            name="default", voice="af_heart", speed=1.0,
            stalls=["One moment."], tone="Plain.",
        )
        p.instruction_spec = "You are a very specific character."
        self.assertIn(HANDOVER_LINE, build_system_prompt(p, "", handover=True))


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
