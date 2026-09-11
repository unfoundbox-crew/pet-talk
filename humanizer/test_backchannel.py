"""Tests for backchannel.should_backchannel (TECH-SPEC section 8.3).

Stdlib unittest only. Covers: continuous-speech boundary 1499/1500/1501ms
(strict >1500 per the spec's ">1.5s continuous" — exactly 1500 holds),
emit-gap boundary 7999/8000ms + never-emitted (None), pause boundary
499/500ms, floor agent/user (+ 'none' holds), deterministic rotation across
the 3 micros, determinism, no wall-clock/random imports, QA positional
calling-convention parity, and the `humanizer.should_backchannel` package
entry point (which is what un-skips QA's conformance test).
"""

import ast
import os
import sys
import unittest

try:
    from backchannel import (
        should_backchannel,
        choose_micro,
        MICRO_VOCAB,
        MIN_CONTINUOUS_SPEECH_MS,
        MIN_EMIT_GAP_MS,
        MIN_USER_PAUSE_MS,
    )
except ImportError:  # run from pet-talk/ root instead of humanizer/
    from humanizer.backchannel import (
        should_backchannel,
        choose_micro,
        MICRO_VOCAB,
        MIN_CONTINUOUS_SPEECH_MS,
        MIN_EMIT_GAP_MS,
        MIN_USER_PAUSE_MS,
    )

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)


def S(**over):
    """Full dict state with sane emit-yes defaults; override per case."""
    state = {
        "continuous_speech_ms": 5000,
        "last_emit_ms_ago": 20000,
        "current_pause_ms": 600,
        "floor_holder": "user",
        "now_ms": 8000,
    }
    state.update(over)
    return state


class TestBackchannelDictForm(unittest.TestCase):
    def test_emit_happy_path(self):
        emit, reason = should_backchannel(S(continuous_speech_ms=1600,
                                            last_emit_ms_ago=None,
                                            current_pause_ms=600,
                                            now_ms=8000))
        self.assertTrue(emit)
        self.assertTrue(reason.startswith("emit:"), reason)
        micro = reason.split(":")[1]
        self.assertIn(micro, list(MICRO_VOCAB), reason)

    def test_continuous_boundary_strict_gt_1500(self):
        # Spec says ">1.5s continuous": exactly 1500 holds, 1501 emits.
        for cont, want in ((1499, False), (1500, False), (1501, True)):
            with self.subTest(cont=cont):
                emit, reason = should_backchannel(S(continuous_speech_ms=cont))
                self.assertEqual(emit, want, reason)
                self.assertTrue(reason.startswith("emit:" if want else "hold:"),
                                reason)

    def test_gap_boundary_7999_8000_and_never(self):
        emit, reason = should_backchannel(S(last_emit_ms_ago=7999))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:emitted-7999ms-ago")
        emit, reason = should_backchannel(S(last_emit_ms_ago=8000))
        self.assertTrue(emit, reason)
        emit, reason = should_backchannel(S(last_emit_ms_ago=None))
        self.assertTrue(emit, reason)  # never emitted -> gap rule passes

    def test_brief_gap_reason_example(self):
        emit, reason = should_backchannel(S(last_emit_ms_ago=3000))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:emitted-3000ms-ago")

    def test_pause_boundary_499_500(self):
        emit, reason = should_backchannel(S(current_pause_ms=499))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:pause-499ms")
        emit, reason = should_backchannel(S(current_pause_ms=500))
        self.assertTrue(emit, reason)

    def test_brief_pause_reason_example(self):
        emit, reason = should_backchannel(S(current_pause_ms=200))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:pause-200ms")

    def test_floor(self):
        emit, reason = should_backchannel(S(floor_holder="agent"))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:floor-agent")
        emit, reason = should_backchannel(S(floor_holder="user"))
        self.assertTrue(emit, reason)
        emit, reason = should_backchannel(S(floor_holder="none"))
        self.assertFalse(emit)  # anything != 'user' holds: never steal floor
        self.assertEqual(reason, "hold:floor-none")

    def test_rule_priority_first_failure_wins(self):
        # All rules violated -> continuous-speech reason (rule 1 checked first).
        emit, reason = should_backchannel(S(continuous_speech_ms=100,
                                            last_emit_ms_ago=0,
                                            current_pause_ms=0,
                                            floor_holder="agent"))
        self.assertFalse(emit)
        self.assertEqual(reason, "hold:continuous-100ms")

    def test_determinism_same_input_same_output(self):
        state = S(continuous_speech_ms=1600, now_ms=12345)
        first = should_backchannel(state)
        for _ in range(5):
            self.assertEqual(should_backchannel(dict(state)), first)

    def test_rotation_across_three_micros(self):
        micros = []
        for now in (0, 8000, 16000):
            emit, reason = should_backchannel(S(continuous_speech_ms=5000,
                                                now_ms=now))
            self.assertTrue(emit, reason)
            micros.append(reason.split(":")[1])
        self.assertEqual(micros, ["mmhmm", "yeah", "right"])
        # Wraps around deterministically; same now_ms -> same micro.
        emit, reason = should_backchannel(S(continuous_speech_ms=5000,
                                            now_ms=24000))
        self.assertTrue(emit)
        self.assertEqual(reason.split(":")[1], "mmhmm")
        emit, again = should_backchannel(S(continuous_speech_ms=5000,
                                           now_ms=0))
        self.assertEqual(again.split(":")[1], "mmhmm")

    def test_rotation_seedable(self):
        _, r0 = should_backchannel(S(continuous_speech_ms=5000, now_ms=0))
        _, r1 = should_backchannel(S(continuous_speech_ms=5000, now_ms=0,
                                     seed=1))
        self.assertEqual(r0.split(":")[1], "mmhmm")
        self.assertEqual(r1.split(":")[1], "yeah")
        self.assertEqual(choose_micro(0), "mmhmm")
        self.assertEqual(choose_micro(3), "mmhmm")

    def test_no_wallclock_no_random_imports(self):
        with open(os.path.join(THIS_DIR, "backchannel.py")) as f:
            tree = ast.parse(f.read())
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])

    def test_micro_vocab_closed(self):
        self.assertEqual(sorted(MICRO_VOCAB), ["mmhmm", "right", "yeah"])
        self.assertEqual(MIN_CONTINUOUS_SPEECH_MS, 1500)
        self.assertEqual(MIN_EMIT_GAP_MS, 8000)
        self.assertEqual(MIN_USER_PAUSE_MS, 500)


class TestBackchannelQAPositionalParity(unittest.TestCase):
    """Same boundary cases QA's conformance test runs, via the positional
    (continuous_ms, ms_since_last_emit, user_pause_ms, floor_held_by) form."""

    def test_qa_cases(self):
        cases = [
            (1600, 9000, 600, "user", True),
            (1500, 99999, 9999, "user", False),
            (1501, 99999, 9999, "user", True),
            (5000, 7999, 600, "user", False),
            (5000, 8000, 600, "user", True),
            (5000, 20000, 499, "user", False),
            (5000, 20000, 500, "user", True),
            (5000, 20000, 600, "agent", False),
            (5000, 20000, 600, "user", True),
        ]
        for cont, gap, pause, floor, want in cases:
            with self.subTest(cont=cont, gap=gap, pause=pause, floor=floor):
                got = should_backchannel(cont, gap, pause, floor)
                self.assertEqual(bool(got[0]), want, "got %r" % (got,))
                if want:
                    self.assertIn(got[1], list(MICRO_VOCAB), "got %r" % (got,))
                else:
                    self.assertIsNone(got[1], "got %r" % (got,))

    def test_package_entry_point_importable(self):
        """Mirrors QA: `import humanizer` off pet-talk/ root exposes it."""
        sys.path.insert(0, ROOT)
        try:
            import humanizer
            self.assertTrue(callable(getattr(humanizer, "should_backchannel",
                                             None)))
            # Same entry point (modulo import path), same behavior.
            probe = S(continuous_speech_ms=1600, last_emit_ms_ago=None)
            self.assertEqual(humanizer.should_backchannel(dict(probe)),
                             should_backchannel(dict(probe)))
            emit, _ = humanizer.should_backchannel(5000, 20000, 600, "user")
            self.assertTrue(emit)
            # Pre-existing preprocessor still exported through the package.
            self.assertTrue(callable(getattr(humanizer, "perform", None)))
        finally:
            if ROOT in sys.path:
                sys.path.remove(ROOT)


if __name__ == "__main__":
    unittest.main()
