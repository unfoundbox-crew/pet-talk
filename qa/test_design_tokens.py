#!/usr/bin/env python3
"""qa/test_design_tokens.py — QA suite for the one design-token source.

pet-talk keeps no palette of its own. `design/tokens.css` is AgentWorth's
`packages/ui/tokens.css`, vendored verbatim; `design/tokens.pet-talk.json`
is pet-talk's own layer (geometry, motion, listening-line, receipt colours).
`design/build.py` generates the consumers; `design/sync-agentworth.sh`
vendors and drift-checks the base. See DESIGN.md.

Honest by construction: every assertion here reads the files on disk and
regenerates in memory, never a cached/hardcoded expectation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DESIGN_DIR = os.path.join(ROOT, "design")
TOKENS_PET_TALK = os.path.join(DESIGN_DIR, "tokens.pet-talk.json")
TOKENS_CSS = os.path.join(DESIGN_DIR, "tokens.css")
BUILD_PY = os.path.join(DESIGN_DIR, "build.py")
SYNC_SH = os.path.join(DESIGN_DIR, "sync-agentworth.sh")

GENERATED_FILES = [
    os.path.join(ROOT, "web", "src", "styles", "tokens.css"),
    os.path.join(ROOT, "web", "src", "styles", "tokens.pet-talk.css"),
    os.path.join(ROOT, "cli", "hotkey", "DesignTokens.swift"),
]

sys.path.insert(0, DESIGN_DIR)
import build as design_build  # noqa: E402  (design/build.py, not a package)

HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b")

# Files outside this suite's ownership that are known, as of 2026-09-12, to
# still carry hand-written hex colour literals. cli/hotkey/*.swift is a
# concurrent lane's in-flux surface (see DESIGN.md / AGENTS.md §"Where
# things live"); web/src/**/*.tsx is the existing cockpit UI, which this
# lane's card explicitly forbids editing beyond the one-line CSS import in
# main.tsx ("UI rebuild on Receipts Over Prose", item 5, is the lane that
# reworks these components onto the new tokens). Wiring either set to
# DesignTokens/tokens.pet-talk.css is that later work's job, not this one's.
# Listed by exact path so a NEW offender (one not on this list) still fails
# the suite instead of hiding behind it.
# Emptied 2026-09-12 by the notch reskin: hud_window.swift and main.swift now
# take every colour from HUDTheme, which reads the generated DesignTokens. The
# set stays as an empty set rather than being deleted, so re-listing a file is a
# visible, deliberate act. qa/test_hud.py adds the stricter check the old list
# was hiding: `0xNN / 255` components, which HEX_RE below never matched.
KNOWN_UNMIGRATED_HEX_SWIFT: set = set()
# Emptied 2026-09-12 by the UI rebuild (item 5): every web/src/**/*.tsx file
# listed here now takes its colour from tokens.css / tokens.pet-talk.css through
# web/src/styles/app.css. The set stays as an empty set rather than being
# deleted, so re-listing a file is a visible, deliberate act.
KNOWN_UNMIGRATED_HEX_TSX: set = set()


def _load_tokens():
    with open(TOKENS_PET_TALK, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_tsx_files():
    for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, "web", "src")):
        if "node_modules" in dirpath:
            continue
        for fn in filenames:
            if fn.endswith(".tsx") or fn.endswith(".ts"):
                yield os.path.join(dirpath, fn)


def _iter_swift_files():
    hotkey_dir = os.path.join(ROOT, "cli", "hotkey")
    if not os.path.isdir(hotkey_dir):
        return
    for fn in sorted(os.listdir(hotkey_dir)):
        if fn.endswith(".swift"):
            yield os.path.join(hotkey_dir, fn)


class TestTokensPetTalkJsonShape(unittest.TestCase):
    """tokens.pet-talk.json parses and every colour has light + dark + role."""

    def test_parses_and_colors_have_light_dark_role(self):
        tokens = _load_tokens()
        self.assertIn("listening_line", tokens)
        listening = tokens["listening_line"]
        self.assertTrue(listening, "listening_line must not be empty")
        for name, entry in listening.items():
            self.assertIn("light", entry, f"listening_line.{name} missing 'light'")
            self.assertIn("dark", entry, f"listening_line.{name} missing 'dark'")
            self.assertIn("role", entry, f"listening_line.{name} missing 'role'")
            self.assertTrue(entry["role"].strip(), f"listening_line.{name}.role is empty")

    def test_geometry_motion_type_sections_present(self):
        tokens = _load_tokens()
        for section in ("type", "geometry", "motion"):
            self.assertIn(section, tokens)
            self.assertTrue(tokens[section], f"{section} must not be empty")


class TestGeneratorCheckCatchesStaleness(unittest.TestCase):
    """generate.py --check fails when a generated file is hand-edited, or
    when the vendored tokens.css has drifted from AgentWorth's source."""

    def test_check_passes_on_clean_tree(self):
        res = subprocess.run(
            [sys.executable, BUILD_PY, "--check"], cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_check_fails_when_generated_file_hand_edited(self):
        target = GENERATED_FILES[0]
        with open(target, "r", encoding="utf-8") as f:
            original = f.read()
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(original + "\n/* hand edit */\n")
            res = subprocess.run(
                [sys.executable, BUILD_PY, "--check"], cwd=ROOT,
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(res.returncode, 0, "expected --check to fail on a hand edit")
        finally:
            with open(target, "w", encoding="utf-8") as f:
                f.write(original)

    def test_generator_is_deterministic(self):
        outputs_a = design_build.build_outputs()
        outputs_b = design_build.build_outputs()
        self.assertEqual(outputs_a, outputs_b)

    def test_vendor_drift_check(self):
        agentworth_src = os.environ.get(
            "AGENTWORTH_TOKENS_CSS",
            "/Users/saurabh/code/unfoundbox/agentworth/packages/ui/tokens.css",
        )
        if not os.path.isfile(agentworth_src):
            raise unittest.SkipTest(
                f"SKIP: AgentWorth source not present at {agentworth_src} on this host"
            )
        if not os.path.isfile(SYNC_SH):
            self.fail("design/sync-agentworth.sh is missing")

        res = subprocess.run(["bash", SYNC_SH, "--check"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        with open(TOKENS_CSS, "r", encoding="utf-8") as f:
            original = f.read()
        try:
            with open(TOKENS_CSS, "w", encoding="utf-8") as f:
                f.write(original + "\n/* drift */\n")
            res = subprocess.run(["bash", SYNC_SH, "--check"], cwd=ROOT,
                                  capture_output=True, text=True, timeout=30)
            self.assertNotEqual(res.returncode, 0, "expected --check to fail on drift")
        finally:
            with open(TOKENS_CSS, "w", encoding="utf-8") as f:
                f.write(original)


class TestNoHexLiteralsOutsideGenerated(unittest.TestCase):
    """No hex colour literal in web/src/**/*.tsx or cli/hotkey/*.swift
    outside the two generated files (tokens.pet-talk.css / DesignTokens.swift
    carry hex by design; nothing else should hand-roll a colour)."""

    def test_no_hex_in_web_src(self):
        offenders = []
        for path in _iter_tsx_files():
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            if HEX_RE.search(text):
                offenders.append(path)

        new_offenders = [p for p in offenders if p not in KNOWN_UNMIGRATED_HEX_TSX]
        self.assertFalse(
            new_offenders,
            "NEW hand-rolled hex colour literal(s) found outside generated CSS: "
            f"{[os.path.relpath(p, ROOT) for p in new_offenders]}",
        )
        if offenders:
            raise unittest.SkipTest(
                "SKIP: pre-existing hex literals remain in "
                f"{sorted(os.path.relpath(p, ROOT) for p in offenders)} — this lane's "
                "card forbids editing web/src components beyond the CSS import in "
                "main.tsx; migrating them to tokens.pet-talk.css is item 5's job "
                "(UI rebuild on Receipts Over Prose)."
            )

    def test_no_new_hex_in_swift_outside_generated(self):
        generated_swift = {os.path.join(ROOT, "cli", "hotkey", "DesignTokens.swift")}
        offenders = []
        for path in _iter_swift_files():
            if path in generated_swift:
                continue
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            if HEX_RE.search(text):
                offenders.append(path)

        new_offenders = [p for p in offenders if p not in KNOWN_UNMIGRATED_HEX_SWIFT]
        self.assertFalse(
            new_offenders,
            f"NEW hand-rolled hex colour literal(s) in cli/hotkey/*.swift: "
            f"{[os.path.relpath(p, ROOT) for p in new_offenders]}",
        )
        if offenders:
            raise unittest.SkipTest(
                "SKIP: pre-existing hex literals remain in "
                f"{sorted(os.path.relpath(p, ROOT) for p in offenders)} — "
                "owned by the concurrent HUD lane (lane 3); wiring them to "
                "DesignTokens is that lane's integration step, not this suite's."
            )


class TestNoIdentityOverrideInPetTalkJson(unittest.TestCase):
    """tokens.pet-talk.json never redefines --mv-accent or a neutral — it
    adds, it does not override AgentWorth's identity."""

    def test_no_accent_or_neutral_keys(self):
        tokens = _load_tokens()
        banned_substrings = ("accent", "ground", "surface", "border", "ink", "text", "muted", "faint")
        for section in ("type", "geometry", "motion"):
            for key in tokens.get(section, {}).keys():
                lowered = key.lower()
                for banned in banned_substrings:
                    self.assertNotIn(
                        banned, lowered,
                        f"tokens.pet-talk.json.{section}.{key} looks like an identity token "
                        "(accent/neutral) — those belong only in tokens.css",
                    )

    def test_no_hex_duplicates_agentworth_tokens_css(self):
        with open(TOKENS_CSS, "r", encoding="utf-8") as f:
            agentworth_hexes = {h.lower() for h in HEX_RE.findall(f.read())}

        tokens = _load_tokens()
        listening = tokens.get("listening_line", {})
        pet_talk_hexes = set()
        for entry in listening.values():
            for side in ("light", "dark"):
                val = entry.get(side, "")
                if HEX_RE.match(val):
                    pet_talk_hexes.add(val.lower())

        dupes = pet_talk_hexes & agentworth_hexes
        self.assertFalse(
            dupes, f"tokens.pet-talk.json hex value(s) duplicate tokens.css: {dupes}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
