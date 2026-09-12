"""Deterministic local commands — answered in <20ms with no LLM round trip."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .persona import Persona


@dataclass(frozen=True)
class Control:
    """Result of the deterministic (zero-LLM) command check."""

    matched: bool = False
    reply: Optional[str] = None
    cancel: bool = False


CANCEL_COMMANDS = frozenset(
    {"stop", "cancel", "pause", "be quiet", "shut up", "hush", "silence", "halt"}
)
STATUS_COMMANDS = frozenset(
    {"status", "ping", "are you there", "system status", "health", "test"}
)
IDENTITY_COMMANDS = frozenset(
    {"who are you", "who is this", "what is your name", "introduce yourself"}
)


def check_deterministic_control(text: str, p: Optional[Persona] = None) -> Control:
    """Local commands answered in <20ms with no LLM round trip.

    ``stop``/``cancel`` and friends do not merely influence the next turn —
    the caller cancels the in-flight one down the same path as ``barge``.
    """
    t = text.strip().lower().rstrip(".?!,")
    if t in CANCEL_COMMANDS:
        return Control(matched=True, reply=None, cancel=True)
    if t in STATUS_COMMANDS:
        return Control(matched=True, reply="All systems online and ready.")
    if t in IDENTITY_COMMANDS:
        name = (getattr(p, "name", "") or "").strip() if p is not None else ""
        who = name.capitalize() if name else "your assistant"
        return Control(matched=True, reply=f"I'm {who}. What do you need?")
    return Control()

