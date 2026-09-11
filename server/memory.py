"""Hippocampus: durable episodic memory ledger for pet-talk.

Contract:
- Memory survives server reboots (persisted to ledger.jsonl).
- Only fully completed turns are committed (barge-in interrupts are never
  committed as agent knowledge).
- Recent conversation turns are injected into LLM context on each turn.
- Pure stdlib, zero external dependencies.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


class Hippocampus:
    def __init__(self, ledger_path: Optional[str] = None) -> None:
        if ledger_path is None:
            server_dir = os.path.dirname(os.path.abspath(__file__))
            ledger_path = os.path.join(server_dir, "ledger.jsonl")
        self.ledger_path = ledger_path

    def record_turn(
        self,
        turn_id: str,
        persona: str,
        user_text: str,
        agent_sentences: list[str],
    ) -> dict:
        """Commit a verified, completed turn to the ledger."""
        if not user_text.strip() or not agent_sentences:
            return {}
        entry = {
            "turn_id": turn_id,
            "timestamp": time.time(),
            "persona": persona,
            "user": user_text.strip(),
            "agent": " ".join(s.strip() for s in agent_sentences if s.strip()),
        }
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def get_history_messages(self, persona: str, limit: int = 6) -> list[dict]:
        """Read recent conversation turns for context injection."""
        if not os.path.exists(self.ledger_path):
            return []
        turns: list[dict] = []
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("persona") == persona or not data.get("persona"):
                            turns.append(data)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []

        recent = turns[-limit:]
        messages = []
        for t in recent:
            if t.get("user"):
                messages.append({"role": "user", "content": t["user"]})
            if t.get("agent"):
                messages.append({"role": "assistant", "content": t["agent"]})
        return messages

    def recent_turns(self, limit: int = 50) -> list[dict]:
        """Read raw recent conversation turns from the ledger."""
        if not os.path.exists(self.ledger_path):
            return []
        turns: list[dict] = []
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        turns.append(data)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return turns[-limit:]

