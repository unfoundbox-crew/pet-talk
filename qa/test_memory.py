#!/usr/bin/env python3
"""Test suite for Hippocampus durable memory ledger."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.memory import Hippocampus


class TestHippocampus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self.tmp.close()
        self.ledger_path = self.tmp.name
        self.memory = Hippocampus(ledger_path=self.ledger_path)

    def tearDown(self):
        if os.path.exists(self.ledger_path):
            os.remove(self.ledger_path)

    def test_record_and_read_turn(self):
        entry = self.memory.record_turn(
            turn_id="turn-101",
            persona="donna",
            user_text="What are we building today?",
            agent_sentences=["We are shipping RealEngine.", "Then taking over pet-talk."],
        )
        self.assertEqual(entry["turn_id"], "turn-101")
        self.assertEqual(entry["persona"], "donna")
        self.assertIn("RealEngine", entry["agent"])

        # Retrieve history
        history = self.memory.get_history_messages("donna")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "What are we building today?")
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIn("RealEngine", history[1]["content"])

    def test_reboot_survival(self):
        # Simulate server reboot with new Hippocampus instance
        self.memory.record_turn("turn-1", "jarvis", "Status report", ["All systems green."])
        rebooted_memory = Hippocampus(ledger_path=self.ledger_path)
        history = rebooted_memory.get_history_messages("jarvis")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["content"], "All systems green.")

    def test_empty_turn_ignored(self):
        entry = self.memory.record_turn("turn-bad", "donna", "", [])
        self.assertEqual(entry, {})
        history = self.memory.get_history_messages("donna")
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
