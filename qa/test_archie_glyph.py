#!/usr/bin/env python3
"""qa/test_archie_glyph.py — Archie's small form, in and out of the browser.

Contract under test (PLAN-2026-09-12 §C, Lane 5a):

  - pet-talk does not get its own mascot: the notch glyph is AgentWorth's
    Archie, small form only. `web/src/components/ArchieGlyph.tsx` renders
    one snapshot per state (idle, listening, speaking, error), driven by the
    same `data-lamp` convention AgentWorth already uses for sleeping/error
    poses. `web/src/components/ArchieGlyph.test.tsx` (vitest) is the source
    of truth for those snapshots; this file drives that suite from the
    Python gate and adds the two checks vitest can't: an accessibility label
    distinct per state, and a repo-wide placement rule enforced by grep, not
    convention — no user surface renders the full (non-small) hound more
    than once, and never on the Archie-receipts chip.

Hermetic: no daemon, no network, no audio. The only subprocess is `npm run
test` inside web/ (vitest), which SKIPs with a clear reason if Node/npm or
web/node_modules isn't present on this machine — never FAILs the gate for a
missing toolchain another lane's environment might not have installed.

Stdlib only for the Python side. Run: `python3 qa/test_archie_glyph.py -v`.
Exit 0 = pass/skip, nonzero = FAIL.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WEB_DIR = os.path.join(ROOT, "web")
GLYPH_TS = os.path.join(WEB_DIR, "src", "components", "ArchieGlyph.tsx")
GLYPH_TEST = os.path.join(WEB_DIR, "src", "components", "ArchieGlyph.test.tsx")
FULL_HOUND_TSX = os.path.join(WEB_DIR, "src", "brand", "Archie.tsx")
RECEIPT_CHIP = os.path.join(WEB_DIR, "src", "components", "ReceiptChip.tsx")

STATES = ["idle", "listening", "speaking", "error"]

NPM = shutil.which("npm")
HAVE_TOOLCHAIN = bool(NPM) and os.path.isdir(os.path.join(WEB_DIR, "node_modules"))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//.*$", re.MULTILINE)


def _code_only(src: str) -> str:
    """Strips /** ... */ and // ... comments before a substring/regex check,
    so a doc comment that *mentions* `<img>` or "Archie" in prose (as this
    file's own components do, at length) never trips a check meant to catch
    actual markup or an actual import."""
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", src))


class TestArchieGlyphFileShape(unittest.TestCase):
    """Static checks on the source, no toolchain required."""

    def test_glyph_file_exists(self):
        self.assertTrue(os.path.isfile(GLYPH_TS), f"missing {GLYPH_TS}")

    def test_glyph_defines_all_four_states(self):
        src = _read(GLYPH_TS)
        for state in STATES:
            self.assertIn(
                f'"{state}"', src, f"ArchieGlyphState is missing {state!r}"
            )

    def test_glyph_forces_small_form(self):
        src = _read(GLYPH_TS)
        self.assertIn(
            'data-size="small"',
            src,
            "ArchieGlyph must always force the small form (never data-size=\"full\")",
        )
        self.assertNotIn(
            'data-size="full"',
            src,
            "ArchieGlyph must never render the full-drawing form",
        )

    def test_glyph_never_sets_an_accessory(self):
        src = _read(GLYPH_TS)
        self.assertNotIn('data-accessory="lamp"', src)
        self.assertNotIn('data-accessory="goggles"', src)

    def test_glyph_uses_data_lamp_convention_for_error(self):
        src = _read(GLYPH_TS)
        self.assertIn('"off"', src, "error state must map to data-lamp=\"off\"")

    def test_glyph_never_imports_an_img_tag_helper(self):
        src = _code_only(_read(GLYPH_TS))
        self.assertNotIn("<img", src, "Archie must be inline SVG, never <img>")

    def test_glyph_colours_come_only_from_vendored_css(self):
        """The glyph's own CSS (archie-glyph.css) may add motion, never a
        new hard-coded colour — every fill must come from the vendored
        archie.css colourway classes."""
        glyph_css = os.path.join(WEB_DIR, "src", "brand", "archie-glyph.css")
        self.assertTrue(os.path.isfile(glyph_css), f"missing {glyph_css}")
        css = _read(glyph_css)
        hex_colours = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
        self.assertEqual(
            hex_colours, [], f"archie-glyph.css must carry no hard-coded colour, found {hex_colours}"
        )


class TestArchieGlyphVitest(unittest.TestCase):
    """Runs web/src/components/ArchieGlyph.test.tsx via vitest — the
    per-state snapshots and the distinct-accessibility-label assertion live
    there, in TypeScript, next to the component they test."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_TOOLCHAIN:
            raise unittest.SkipTest(
                "SKIP: npm and/or web/node_modules not present on this machine — "
                "install web deps (`npm --prefix web install`) to run the vitest suite"
            )
        if not os.path.isfile(GLYPH_TEST):
            raise unittest.SkipTest(f"SKIP: {GLYPH_TEST} not created yet")

    def test_vitest_suite_passes(self):
        result = subprocess.run(
            [NPM, "run", "test", "--", "src/components/ArchieGlyph.test.tsx"],
            cwd=WEB_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            self.fail(
                "vitest ArchieGlyph.test.tsx FAILED:\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )


class TestArchiePlacementRules(unittest.TestCase):
    """The placement rule enforced by grep, not convention: the full
    (non-small) hound renders on at most one surface per screen, never on
    the Archie-receipts chip, never twice."""

    def test_full_hound_component_exists_and_is_distinct_from_glyph(self):
        self.assertTrue(os.path.isfile(FULL_HOUND_TSX), f"missing {FULL_HOUND_TSX}")
        self.assertTrue(os.path.isfile(GLYPH_TS), f"missing {GLYPH_TS}")

    def test_receipt_chip_never_imports_the_full_hound_or_the_glyph(self):
        """Archie never appears on a receipt — 'he is a state, not a
        texture' — checked here the same way ReceiptChip.tsx's own docstring
        promises it is (qa/test_receipts.py covers the backend half; this is
        the brand half)."""
        if not os.path.isfile(RECEIPT_CHIP):
            self.skipTest(f"SKIP: {RECEIPT_CHIP} not created yet (another lane owns it)")
        src = _code_only(_read(RECEIPT_CHIP))
        self.assertNotIn("Archie", src, "ReceiptChip.tsx must never import or render Archie")
        self.assertNotIn("ArchieGlyph", src, "ReceiptChip.tsx must never import or render ArchieGlyph")

    def test_full_hound_rendered_at_most_once_per_source_file(self):
        """A grep-based census: for every .tsx/.ts file under web/src, the
        full-hound component (<Archie ...>/Archie.tsx import) is used at
        most once. Two Archies on one screen are two states, and one of
        them is lying (agentworth/docs/DESIGN.md, 'Archie')."""
        web_src = os.path.join(WEB_DIR, "src")
        offenders = []
        for dirpath, _dirs, files in os.walk(web_src):
            for name in files:
                if not (name.endswith(".tsx") or name.endswith(".ts")):
                    continue
                path = os.path.join(dirpath, name)
                if path in (FULL_HOUND_TSX,):
                    continue  # the component's own definition file, not a usage site
                src = _code_only(_read(path))
                # Count JSX usages of the full-hound component (<Archie ...),
                # not the unrelated ArchieGlyph component and not plain text
                # mentions (comments, strings) which aren't a render.
                uses = re.findall(r"<Archie\b(?!Glyph)", src)
                if len(uses) > 1:
                    offenders.append((os.path.relpath(path, ROOT), len(uses)))
        self.assertEqual(
            offenders,
            [],
            f"full hound (<Archie ...>) rendered more than once in: {offenders}",
        )

    def test_no_surface_renders_full_hound_and_glyph_together_as_a_double(self):
        """Belt-and-suspenders on 'never twice on one screen': a file that
        renders both the full hound and the glyph is two Archies on one
        screen too, unless it's intentionally the empty-state/app-icon
        surface — flagged here for a human to confirm rather than silently
        allowed."""
        web_src = os.path.join(WEB_DIR, "src")
        both = []
        for dirpath, _dirs, files in os.walk(web_src):
            for name in files:
                if not (name.endswith(".tsx") or name.endswith(".ts")):
                    continue
                path = os.path.join(dirpath, name)
                if path in (FULL_HOUND_TSX, GLYPH_TS, GLYPH_TEST):
                    continue
                src = _code_only(_read(path))
                has_full = bool(re.search(r"<Archie\b(?!Glyph)", src))
                has_glyph = "<ArchieGlyph" in src
                if has_full and has_glyph:
                    both.append(os.path.relpath(path, ROOT))
        self.assertEqual(both, [], f"both the full hound and the glyph render in: {both}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
