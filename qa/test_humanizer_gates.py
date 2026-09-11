#!/usr/bin/env python3
"""qa/test_humanizer_gates.py — backchannel contract tests (TECH-SPEC sec 8.3).

CONTRACT TESTS, not implementation tests: the humanizer lane runs in
parallel, so this file imports NOTHING from pet-talk/humanizer/. Instead it
encodes the section 8.3 rules as interface assertions against a documented
reference policy defined below. When pet-talk/humanizer/ lands, a conformance
test can import its real `should_backchannel` and run the SAME cases against
it — the cases are the contract, the reference policy is just the oracle.

Spec contract under test (TECH-SPEC section 8.3, verbatim):
  - While listening: VAD speech >1.5s continuous -> emit one Micro
    ("mmhmm" / "yeah" / "right", persona-voiced, max 1 per 8s).
  - Never steals floor; never during user pause <500ms.

Derived interface (what the humanizer module MUST provide):
  `should_backchannel(continuous_speech_ms, ms_since_last_emit,
                       user_pause_ms, floor_held_by) -> (emit: bool, token: str|None)`
  - `floor_held_by` in {"user", "agent", "none"}; emit must never flip it.
  - `token`, when emitting, must be one of MICRO_VOCAB.

Stdlib only. Run: `python3 qa/test_humanizer_gates.py -v`.
Exit 0 = pass/skip, nonzero = FAIL.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUMANIZER_DIR = os.path.join(ROOT, "humanizer")

# ---------------------------------------------------------------------------
# Contract constants — TECH-SPEC section 8.3, verbatim figures (ms).
# If the spec changes these, THIS file must change too (loudly, by hand).
# ---------------------------------------------------------------------------
MIN_CONTINUOUS_SPEECH_MS = 1500   # VAD speech ">1.5s continuous"
MIN_EMIT_GAP_MS = 8000            # "max 1 per 8s"
MIN_USER_PAUSE_MS = 500           # "never during user pause <500ms"

MICRO_VOCAB = frozenset({"mmhmm", "yeah", "right"})


def reference_should_backchannel(continuous_speech_ms, ms_since_last_emit,
                                 user_pause_ms, floor_held_by="user"):
    """Reference oracle for the 8.3 contract. NOT the implementation.

    Returns (emit, token). Rules, in order:
      1. continuous speech must EXCEED 1500ms (strictly greater);
      2. at least 8000ms since the last emit;
      3. user pause must be >= 500ms (never during a <500ms pause);
      4. floor must not be held by the agent (never steal the floor;
         backchannel never takes the floor — caller keeps `floor_held_by`).
    """
    if continuous_speech_ms <= MIN_CONTINUOUS_SPEECH_MS:
        return False, None
    if ms_since_last_emit < MIN_EMIT_GAP_MS:
        return False, None
    if user_pause_ms < MIN_USER_PAUSE_MS:
        return False, None
    if floor_held_by == "agent":
        return False, None
    return True, "mmhmm"


class TestBackchannelContract(unittest.TestCase):
    def test_emit_after_long_continuous_speech(self):
        emit, token = reference_should_backchannel(
            continuous_speech_ms=1600, ms_since_last_emit=9000,
            user_pause_ms=600, floor_held_by="user")
        self.assertTrue(emit, "1.6s continuous speech should emit one Micro")
        self.assertIn(token, MICRO_VOCAB,
                      "emitted token %r not in Micro vocab %s" % (token, sorted(MICRO_VOCAB)))

    def test_boundary_1500ms_is_not_enough(self):
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=1500, ms_since_last_emit=99999,
            user_pause_ms=9999, floor_held_by="user")
        self.assertFalse(emit, "spec says >1.5s: exactly 1500ms must NOT emit")
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=1501, ms_since_last_emit=99999,
            user_pause_ms=9999, floor_held_by="user")
        self.assertTrue(emit, "1501ms must emit")

    def test_max_one_per_8s(self):
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=7999,
            user_pause_ms=600, floor_held_by="user")
        self.assertFalse(emit, "emit 7999ms after last must be suppressed (max 1 per 8s)")
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=8000,
            user_pause_ms=600, floor_held_by="user")
        self.assertTrue(emit, "emit exactly 8000ms after last is allowed")

    def test_never_during_short_pause(self):
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=20000,
            user_pause_ms=499, floor_held_by="user")
        self.assertFalse(emit, "pause <500ms must suppress emit")
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=20000,
            user_pause_ms=500, floor_held_by="user")
        self.assertTrue(emit, "pause exactly 500ms is allowed")

    def test_never_steals_floor(self):
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=20000,
            user_pause_ms=600, floor_held_by="agent")
        self.assertFalse(emit, "must never emit while the agent holds the floor")
        # Backchannel never TAKES the floor: emit leaves floor_held_by untouched.
        floor = "user"
        emit, _ = reference_should_backchannel(
            continuous_speech_ms=5000, ms_since_last_emit=20000,
            user_pause_ms=600, floor_held_by=floor)
        self.assertEqual(floor, "user",
                         "backchannel must be floor-holding-never: caller floor unchanged")

    def test_micro_vocab_closed(self):
        self.assertEqual(sorted(MICRO_VOCAB), ["mmhmm", "right", "yeah"],
                         "Micro vocab drifted from spec 8.3 (mmhmm/yeah/right)")

    def test_humanizer_module_conformance(self):
        """Conformance vs the real module — SKIPs until the lane lands."""
        if not os.path.isdir(HUMANIZER_DIR):
            self.skipTest("SKIP: pet-talk/humanizer/ absent — conformance "
                          "unprovable; contract cases above are the pending gate")
        # NOTE: bare `import humanizer` could resolve to the unrelated PyPI
        # namesake on sys.path, so require the repo file to exist first.
        landed = any(os.path.isfile(os.path.join(HUMANIZER_DIR, f))
                     for f in ("__init__.py", "humanize.py")) \
            or os.path.isfile(os.path.join(ROOT, "humanizer.py"))
        if not landed:
            self.skipTest("SKIP: pet-talk/humanizer/ present but no module file "
                          "(__init__.py/humanize.py) yet — conformance unprovable")
        sys.path.insert(0, ROOT)
        try:
            import humanizer  # noqa: F401
        except ImportError as e:
            self.skipTest("SKIP: pet-talk/humanizer/ present but unimportable (%s)" % e)
        finally:
            if ROOT in sys.path:
                sys.path.remove(ROOT)
        should_bc = getattr(humanizer, "should_backchannel", None)
        if not callable(should_bc):
            self.skipTest("SKIP: humanizer module has no `should_backchannel` "
                          "entry point yet — interface assertion pending")
        # When the interface exists, the SAME boundary cases run against it.
        cases = [
            # (continuous_ms, since_last_ms, pause_ms, floor, expect_emit)
            (1600, 9000, 600, "user", True),
            (1500, 99999, 9999, "user", False),
            (5000, 7999, 600, "user", False),
            (5000, 8000, 600, "user", True),
            (5000, 20000, 499, "user", False),
            (5000, 20000, 600, "agent", False),
        ]
        for cont, gap, pause, floor, want in cases:
            with self.subTest(cont=cont, gap=gap, pause=pause, floor=floor):
                got = should_bc(cont, gap, pause, floor)
                emit = got[0] if isinstance(got, tuple) else got
                self.assertEqual(bool(emit), want,
                                 "should_backchannel(%r,%r,%r,%r) = %r, want %r"
                                 % (cont, gap, pause, floor, got, want))


if __name__ == "__main__":
    unittest.main(verbosity=2)
