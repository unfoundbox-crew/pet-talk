#!/usr/bin/env python3
"""design/build.py — generate pet-talk's design-token consumers.

Reads the two source files:
  design/tokens.css            AgentWorth's tokens.css, vendored verbatim
                                (see design/sync-agentworth.sh)
  design/tokens.pet-talk.json  pet-talk's own layer: geometry, motion,
                                listening-line + receipt colours only

Writes three generated consumers, each carrying a "generated, do not edit"
header naming both source files:
  web/src/styles/tokens.css          AgentWorth's tokens.css, passed through
  web/src/styles/tokens.pet-talk.css CSS custom properties for the pet-talk
                                      layer (geometry, motion, listening-line)
  cli/hotkey/DesignTokens.swift      enum DesignTokens { static let ... }

Deterministic: same two inputs always produce byte-identical outputs (JSON
keys are read in file order — Python dicts preserve insertion order, and
tokens.pet-talk.json is not re-sorted — so re-running --write with an
unchanged tokens.pet-talk.json never touches the outputs).

Usage:
  python3 design/build.py --check   # exit 1 if any generated file is stale
  python3 design/build.py --write   # (re)writes all three generated files
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS_CSS = os.path.join(ROOT, "design", "tokens.css")
TOKENS_PET_TALK = os.path.join(ROOT, "design", "tokens.pet-talk.json")

OUT_WEB_CSS = os.path.join(ROOT, "web", "src", "styles", "tokens.css")
OUT_WEB_PET_TALK_CSS = os.path.join(ROOT, "web", "src", "styles", "tokens.pet-talk.css")
OUT_SWIFT = os.path.join(ROOT, "cli", "hotkey", "DesignTokens.swift")

HEADER_SOURCES = "design/tokens.css + design/tokens.pet-talk.json"


def load_pet_talk_tokens() -> dict:
    with open(TOKENS_PET_TALK, "r", encoding="utf-8") as f:
        return json.load(f)


def load_agentworth_css() -> str:
    with open(TOKENS_CSS, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# AgentWorth palette, PARSED from design/tokens.css — never redefined in
# tokens.pet-talk.json (qa/test_design_tokens.py forbids it). The notch HUD is
# AppKit, not CSS, so it cannot read a custom property: these values are lifted
# out of the vendored stylesheet at generate time so the Swift side has exactly
# one source and drift is impossible. Hex only — the rgba() soft/border ramps
# have no AppKit consumer.
# ---------------------------------------------------------------------------
PALETTE_ROLES = (
    "ground",
    "surface",
    "surface-2",
    "surface-3",
    "border",
    "border-soft",
    "ink",
    "text",
    "muted",
    "faint",
    "accent",
    "success",
    "warn",
    "danger",
)

_HEX_DECL_RE = re.compile(r"--mv-([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")


def _block_after(css: str, selector: str) -> str:
    """The text of the first `{...}` block whose opening line contains `selector`."""
    i = css.find(selector)
    if i < 0:
        raise ValueError(f"tokens.css has no {selector!r} block")
    start = css.find("{", i)
    if start < 0:
        raise ValueError(f"tokens.css {selector!r} has no opening brace")
    depth = 0
    for j in range(start, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : j]
    raise ValueError(f"tokens.css {selector!r} block is unterminated")


def parse_agentworth_palette(css: str) -> dict:
    """{role: {"light": "#rrggbb", "dark": "#rrggbb"}} for PALETTE_ROLES.

    Light comes from the bare `:root` block (the default theme since
    2026-08-31), dark from the explicit `[data-theme="dark"]` block — the same
    two blocks a browser would resolve. A role missing from either block is
    omitted entirely rather than guessed.
    """
    light_block = _block_after(css, ":root {")
    dark_block = _block_after(css, ':root[data-theme="dark"]')
    light = dict(_HEX_DECL_RE.findall(light_block))
    dark = dict(_HEX_DECL_RE.findall(dark_block))
    out = {}
    for role in PALETTE_ROLES:
        if role in light and role in dark:
            out[role] = {"light": light[role], "dark": dark[role]}
    return out


# ---------------------------------------------------------------------------
# web/src/styles/tokens.css — AgentWorth's tokens.css, passed through with a
# generated-file header. The header is a comment, so this file is still
# valid, loadable CSS on its own.
# ---------------------------------------------------------------------------
def render_web_css(agentworth_css: str) -> str:
    header = (
        "/* GENERATED FILE — do not edit by hand.\n"
        f"   Source: {HEADER_SOURCES}\n"
        "   Regenerate with: python3 design/build.py --write\n"
        "   This is AgentWorth's packages/ui/tokens.css, vendored at\n"
        "   design/tokens.css and passed through unchanged — pet-talk's\n"
        "   identity (colour, neutrals, font family) is locked to it. */\n\n"
    )
    return header + agentworth_css


# ---------------------------------------------------------------------------
# web/src/styles/tokens.pet-talk.css — the pet-talk-only layer as CSS custom
# properties: geometry and motion are theme-independent so they land on bare
# :root; listening-line/receipt colours get the three-state theming contract
# (bare :root light default, guarded dark media query, explicit data-theme
# override) required for every pet-talk surface.
# ---------------------------------------------------------------------------
def _css_var_name(prefix: str, dotted_key: str) -> str:
    return "--pt-" + prefix + "-" + dotted_key.replace(".", "-")


def render_web_pet_talk_css(tokens: dict) -> str:
    type_scale = tokens.get("type", {})
    geometry = tokens.get("geometry", {})
    motion = tokens.get("motion", {})
    listening = tokens.get("listening_line", {})

    lines = [
        "/* GENERATED FILE — do not edit by hand.",
        f"   Source: {HEADER_SOURCES}",
        "   Regenerate with: python3 design/build.py --write",
        "   pet-talk's own layer only — colour/neutral identity comes from",
        "   tokens.css (AgentWorth) and is never redefined here. */",
        "",
        ":root {",
    ]
    for k, v in type_scale.items():
        _, is_numeric = _swift_numeric_literal(v)
        css_v = v if is_numeric else f'"{v}"'
        lines.append(f"  {_css_var_name('type', k)}: {css_v};")
    for k, v in geometry.items():
        lines.append(f"  {_css_var_name('geo', k)}: {v};")
    for k, v in motion.items():
        lines.append(f"  {_css_var_name('motion', k)}: {v};")
    for name, entry in listening.items():
        light = entry.get("light")
        lines.append(f"  {_css_var_name('listening', name)}: {light};")
    lines.append("}")
    lines.append("")

    dark_names = [n for n in listening.keys()]
    if dark_names:
        lines.append("@media (prefers-color-scheme: dark) {")
        lines.append('  :root:not([data-theme="light"]) {')
        for name in dark_names:
            dark = listening[name].get("dark")
            lines.append(f"    {_css_var_name('listening', name)}: {dark};")
        lines.append("  }")
        lines.append("}")
        lines.append(':root[data-theme="dark"] {')
        for name in dark_names:
            dark = listening[name].get("dark")
            lines.append(f"  {_css_var_name('listening', name)}: {dark};")
        lines.append("}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# cli/hotkey/DesignTokens.swift — enum DesignTokens, one static let per
# geometry/motion/listening-line value. Must compile standalone
# (`swiftc -typecheck cli/hotkey/DesignTokens.swift`) with no dependency on
# any other file in cli/hotkey/ — lane 3 wires the reference in from
# hud_window.swift's own HUDMotionTokens/HUDCapsuleView once this lands.
# ---------------------------------------------------------------------------
def _swift_ident(dotted_key: str) -> str:
    parts = dotted_key.replace("-", "_").split(".")
    out = parts[0]
    for p in parts[1:]:
        out += p[0].upper() + p[1:] if p else ""
    return out


def _swift_numeric_literal(value: str):
    """Return (literal, isNumeric) — pt/ms/px suffixed values become Double,
    plain numbers become Double, everything else stays a String literal."""
    s = value.strip()
    for suffix in ("pt", "ms", "px"):
        if s.endswith(suffix):
            core = s[: -len(suffix)]
            try:
                float(core)
                return core, True
            except ValueError:
                return None, False
    try:
        float(s)
        return s, True
    except ValueError:
        return None, False


def render_swift(tokens: dict, palette: dict) -> str:
    type_scale = tokens.get("type", {})
    geometry = tokens.get("geometry", {})
    motion = tokens.get("motion", {})
    listening = tokens.get("listening_line", {})

    lines = [
        "// GENERATED FILE — do not edit by hand.",
        f"// Source: {HEADER_SOURCES}",
        "// Regenerate with: python3 design/build.py --write",
        "//",
        "// pet-talk's own token layer (type scale, geometry, motion,",
        "// listening-line) PLUS the AgentWorth palette lifted out of",
        "// design/tokens.css — AppKit cannot read a CSS custom property, so the",
        "// notch HUD reads the same values from here. Colour identity is still",
        "// AgentWorth's and is never redefined in tokens.pet-talk.json.",
        "// Must compile standalone:",
        "//   swiftc -typecheck cli/hotkey/DesignTokens.swift",
        "",
        "import Foundation",
        "",
        "public enum DesignTokens {",
        "",
        "    // MARK: - Type scale",
    ]
    for k, v in type_scale.items():
        literal, is_numeric = _swift_numeric_literal(v)
        name = _swift_ident(k)
        if is_numeric:
            lines.append(f"    public static let {name}: Double = {literal}")
        else:
            lines.append(f'    public static let {name}: String = "{v}"')

    lines.append("")
    lines.append("    // MARK: - Geometry")
    for k, v in geometry.items():
        literal, is_numeric = _swift_numeric_literal(v)
        name = _swift_ident(k)
        if is_numeric:
            lines.append(f"    public static let {name}: Double = {literal}")
        else:
            lines.append(f'    public static let {name}: String = "{v}"')

    lines.append("")
    lines.append("    // MARK: - Motion")
    for k, v in motion.items():
        literal, is_numeric = _swift_numeric_literal(v)
        name = _swift_ident(k)
        if is_numeric:
            lines.append(f"    public static let {name}: Double = {literal}")
        else:
            lines.append(f'    public static let {name}: String = "{v}"')

    lines.append("")
    lines.append("    // MARK: - Listening line / receipt colours")
    for name, entry in listening.items():
        base = _swift_ident(name)
        light = entry.get("light", "")
        dark = entry.get("dark", "")
        role = entry.get("role", "")
        lines.append(f"    /// {role}")
        lines.append(f'    public static let {base}Light: String = "{light}"')
        lines.append(f'    public static let {base}Dark: String = "{dark}"')

    lines.append("")
    lines.append("    // MARK: - AgentWorth palette (parsed from design/tokens.css)")
    lines.append("    //")
    lines.append("    // Role names and values are AgentWorth's. `--mv-accent` is the ONE")
    lines.append("    // accent in the system: selection, focus, links, and the receipt total")
    lines.append("    // — nothing else. success/warn/danger are the reserved state colours.")
    for role, entry in palette.items():
        name = _swift_ident("palette." + role.replace("-", "."))
        lines.append(f'    public static let {name}Light: String = "{entry["light"]}"')
        lines.append(f'    public static let {name}Dark: String = "{entry["dark"]}"')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def build_outputs():
    tokens = load_pet_talk_tokens()
    agentworth_css = load_agentworth_css()
    return {
        OUT_WEB_CSS: render_web_css(agentworth_css),
        OUT_WEB_PET_TALK_CSS: render_web_pet_talk_css(tokens),
        OUT_SWIFT: render_swift(tokens, parse_agentworth_palette(agentworth_css)),
    }


def cmd_write() -> int:
    outputs = build_outputs()
    for path, content in outputs.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"wrote {os.path.relpath(path, ROOT)}")
    return 0


def cmd_check() -> int:
    outputs = build_outputs()
    stale = []
    for path, expected in outputs.items():
        rel = os.path.relpath(path, ROOT)
        if not os.path.exists(path):
            stale.append((rel, "missing"))
            continue
        with open(path, "r", encoding="utf-8") as f:
            actual = f.read()
        if actual != expected:
            stale.append((rel, "content differs from generator output"))
    if stale:
        print("design/build.py --check: FAIL", file=sys.stderr)
        for rel, reason in stale:
            print(f"  {rel}: {reason}", file=sys.stderr)
        return 1
    print("design/build.py --check: OK — all generated files match their sources")
    return 0


def main(argv):
    if "--write" in argv:
        return cmd_write()
    if "--check" in argv:
        return cmd_check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
