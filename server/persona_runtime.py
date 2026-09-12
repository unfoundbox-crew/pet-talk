"""Persona resolution and system-prompt construction.

Identity comes from the ACTIVE persona and nowhere else. There is no baked-in
character: a persona with an ``instruction_spec`` supplies its own prompt, and
one without gets a neutral prompt built from its own ``persona.md`` fields. An
unknown persona name is an error, never a silent substitution.
"""
from __future__ import annotations

import os
from typing import Optional

from .persona import PERSONAS_DIR, Persona
from .persona import load_persona
from .providers import ProviderError
from . import runtime

#: Personas that need no ``personas/<name>.md`` on disk.
VIRTUAL_PERSONAS = frozenset({"default"})

#: Client-supplied speed is quantised and clamped before it reaches a
#: persona. It is part of the stall cache key, so an unclamped float from a
#: frame meant a client could mint unlimited cache entries (and ask a backend
#: for absurd rates). 0.05 steps inside [0.7, 1.4] is a finite set of 15.
SPEED_MIN = 0.7
SPEED_MAX = 1.4
SPEED_STEP = 0.05


def clamp_speed(value: float) -> float:
    """Quantise to ``SPEED_STEP`` and clamp into [SPEED_MIN, SPEED_MAX]."""
    stepped = round(float(value) / SPEED_STEP) * SPEED_STEP
    return round(min(SPEED_MAX, max(SPEED_MIN, stepped)), 2)

def persona_exists(name: str) -> bool:
    if name in VIRTUAL_PERSONAS:
        return True
    return os.path.isfile(os.path.join(PERSONAS_DIR, f"{name}.md"))


def resolve_persona(name: Optional[str]) -> Persona:
    """Load a persona by name. Fails closed — never a blank default.

    An absent name means "the server default"; a name we have no persona file
    for raises ``unknown_persona`` so the client hears about it instead of
    silently talking to somebody else.
    """
    if name is None or not str(name).strip():
        return runtime.default_persona()
    clean = str(name).strip().lower()
    if not persona_exists(clean):
        raise ProviderError("unknown_persona", clean)
    try:
        return load_persona(clean)
    except Exception as e:
        raise ProviderError("persona_unloadable", f"{clean}: {e}") from e


def apply_persona_overrides(p: Persona, msg: dict) -> Persona:
    """Apply per-turn voice/speed/tone/stall overrides from a client frame."""
    voice = msg.get("custom_voice") or msg.get("voice")
    if voice:
        p.voice = str(voice)
    speed = msg.get("custom_speed") or msg.get("speed")
    if speed not in (None, ""):
        try:
            p.speed = clamp_speed(speed)
        except (TypeError, ValueError) as e:
            raise ProviderError("bad_frame", f"speed={speed!r} is not a number") from e
    tone = msg.get("custom_tone") or msg.get("system_prompt")
    if tone:
        p.tone = str(tone)
    stalls = msg.get("custom_stalls")
    if stalls is not None:
        if not isinstance(stalls, list):
            raise ProviderError("bad_frame", "custom_stalls must be a list")
        cleaned = [str(s) for s in stalls if str(s).strip()]
        if cleaned:
            p.stalls = cleaned
    return p


# --------------------------------------------------------------- prompt ---

VOICE_RULES = (
    "LIVE VOICE RULES:\n"
    "1. Reply in 1 or 2 concise spoken sentences, 20 words at the outside.\n"
    "2. Lead with the answer; no preamble.\n"
    "3. No markdown, bullets, asterisks, backticks, code, or parentheticals.\n"
    "4. Never read file names, git hashes, or raw service ports aloud.\n"
    "5. Stay in the character described above — it is the only character you have."
)


def build_system_prompt(p: Persona, grounding: str) -> str:
    """Build the system prompt from the ACTIVE persona only.

    A persona carrying an ``instruction_spec`` supplies its own prompt
    verbatim. One without it gets a neutral prompt derived from its own
    ``persona.md`` fields (name, tone body) — no other persona's identity
    ever leaks in.
    """
    spec = str(getattr(p, "instruction_spec", "") or "").strip()
    grounding_block = f"[ACTIVE SYSTEM GROUNDING]\n{grounding}" if grounding else ""
    if spec:
        return "\n\n".join(part for part in (spec, grounding_block) if part)

    name = (getattr(p, "name", "") or "the assistant").strip()
    tone = str(getattr(p, "tone", "") or "").strip()
    identity = f"You are {name}." if name else ""
    parts = [part for part in (identity, tone, grounding_block, VOICE_RULES) if part]
    return "\n\n".join(parts)

