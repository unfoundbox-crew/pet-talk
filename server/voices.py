"""``personas/voices.yaml`` reader.

No hardcoded voice anywhere (TECH-SPEC section 5): a persona names a voice id
and the server resolves it here. PyYAML is used when installed; otherwise a
minimal nested-map parser covers the file's shape. Either way, fail closed.
"""
from __future__ import annotations

import os
from typing import Any

from .providers import ProviderError
from .settings import VOICES_PATH


def parse_voices_minimal(text: str) -> dict[str, Any]:
    """Minimal YAML-subset parser for voices.yaml: nested maps only.

    Handles ``key:``, ``key: value`` (2/4-space indent), comments, and
    single/double-quoted scalars. Anything else raises ProviderError.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent]:
            raise ProviderError("voices_bad_yaml", f"line {lineno}: tabs not allowed")
        if ":" not in stripped:
            raise ProviderError(
                "voices_bad_yaml", f"line {lineno}: expected 'key:' or 'key: value'"
            )
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            raise ProviderError("voices_bad_yaml", f"line {lineno}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            node: dict[str, Any] = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = value
    return root


def load_voices(path: str = "") -> list[dict[str, str]]:
    """Read voices.yaml -> ``[{id, display_name, lang}]``. Fail-closed."""
    voices_path = path or VOICES_PATH
    if not os.path.exists(voices_path):
        raise ProviderError("voices_not_found", os.path.basename(voices_path))
    with open(voices_path, encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml  # type: ignore  # optional: stdlib-only fallback below

        data = yaml.safe_load(text)
    except ImportError:
        data = parse_voices_minimal(text)
    if not isinstance(data, dict) or not isinstance(data.get("voices"), dict):
        raise ProviderError("voices_bad_yaml", "expected top-level 'voices:' map")
    out: list[dict[str, str]] = []
    for vid, meta in data["voices"].items():
        if not isinstance(meta, dict):
            raise ProviderError("voices_bad_yaml", f"voice '{vid}': expected map")
        try:
            out.append(
                {"id": vid, "display_name": meta["display_name"], "lang": meta["lang"]}
            )
        except KeyError as e:
            raise ProviderError("voices_bad_voice", f"voice '{vid}': missing {e}") from e
    return out
