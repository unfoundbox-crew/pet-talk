"""Persona loader — no hardcoding.

Loads ``personas/<name>.md`` frontmatter (``voice``, ``speed``, ``stalls[]``).
If the personas dir is absent (v0.2 default), falls back to a built-in
default persona. Tone rules ride along as body text.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

PERSONAS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "personas")

DEFAULT_PERSONA = {
    "name": "default",
    "voice": "af_heart",
    "speed": 1.0,
    "stalls": [
        "On it — one sec.",
        "Let me look that up for you.",
        "Good question — checking now.",
    ],
    "tone": "Warm, brief, plain-spoken. One idea per sentence.",
}


@dataclass
class Persona:
    name: str = DEFAULT_PERSONA["name"]
    voice: str = DEFAULT_PERSONA["voice"]
    speed: float = DEFAULT_PERSONA["speed"]
    stalls: list = field(default_factory=lambda: list(DEFAULT_PERSONA["stalls"]))
    tone: str = DEFAULT_PERSONA["tone"]

    def stall_for(self, index: int = 0) -> str:
        if not self.stalls:
            raise ValueError("persona_no_stalls: persona defines no stall phrases")
        return self.stalls[index % len(self.stalls)]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-ish frontmatter parser (no extra deps)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        raise ValueError("persona_bad_frontmatter: missing closing ---")
    raw = text[3:end].strip()
    body = text[end + 3 :].strip()
    meta: dict = {}
    current_key: str = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and current_key:
            item = line.strip()[2:].strip().strip("'\"")
            if not isinstance(meta.get(current_key), list):
                meta[current_key] = []
            meta[current_key].append(item)
        elif ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip().strip("'\"")
            current_key = key
            if value == "":
                meta[key] = []
            elif value.startswith("[") and value.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            else:
                try:
                    meta[key] = float(value) if "." in value else int(value)
                except ValueError:
                    meta[key] = value
    return meta, body


def load_persona(name: str = "default", personas_dir: str = PERSONAS_DIR) -> Persona:
    """Load personas/<name>.md; fall back to built-in default if dir/file absent."""
    path = os.path.join(personas_dir, f"{name}.md")
    if not os.path.isdir(personas_dir) or not os.path.isfile(path):
        d = DEFAULT_PERSONA
        return Persona(
            name=name if os.path.isfile(path) is False and name != "default" else d["name"],
            voice=d["voice"],
            speed=d["speed"],
            stalls=list(d["stalls"]),
            tone=d["tone"],
        )
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        raise ValueError(f"persona_unreadable: {e}")
    meta, body = _parse_frontmatter(text)
    voice = meta.get("voice", DEFAULT_PERSONA["voice"])
    speed = meta.get("speed", DEFAULT_PERSONA["speed"])
    stalls = meta.get("stalls", list(DEFAULT_PERSONA["stalls"]))
    tone = body or DEFAULT_PERSONA["tone"]
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise ValueError(f"persona_bad_speed: {speed!r}")
    if not isinstance(stalls, list) or not stalls:
        raise ValueError("persona_no_stalls: frontmatter needs a non-empty stalls list")
    return Persona(name=name, voice=str(voice), speed=speed, stalls=[str(s) for s in stalls], tone=tone)
