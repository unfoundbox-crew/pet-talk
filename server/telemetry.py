"""Turn telemetry: JSONL per-turn stage timings for before/after swaps."""
import json
import time
from typing import Any, Optional


class TurnLog:
    """Append-only JSONL. One line per turn (..jsonl), no PII beyond text hashes."""

    def __init__(self, path: str = "turns.jsonl"):
        self.path = path
        self._t0: Optional[float] = None
        self._stages: dict[str, float] = {}
        self._turn: dict[str, Any] = {}

    def start(self, turn_id: str, providers: dict[str, str], **meta: Any) -> None:
        self._t0 = time.time()
        self._stages = {}
        self._turn = {"turn_id": turn_id, "providers": providers, **meta}

    def mark(self, stage: str) -> float:
        """Record ms since start for a stage. Returns the value."""
        ms = round((time.time() - (self._t0 or time.time())) * 1000, 1)
        self._stages[stage] = ms
        return ms

    def end(self, **extra: Any) -> dict[str, Any]:
        total = round((time.time() - (self._t0 or time.time())) * 1000, 1)
        row = {**self._turn, "stages_ms": self._stages, "total_ms": total, **extra}
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")
        return row
