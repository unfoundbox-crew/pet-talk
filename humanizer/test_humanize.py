"""Tests for humanize.perform. Stdlib unittest only."""

import re
import unittest

from humanize import perform, _SLANG, _FILLERS

MILLENNIAL = {"tone": "millennial", "filler_rate": 0.15, "slang_level": 2}


class TestHumanize(unittest.TestCase):
    def test_determinism(self):
        text = "I am going to tell you, it is really good. It is very exciting, and cool!"
        a = perform(text, MILLENNIAL, seed=7)
        b = perform(text, MILLENNIAL, seed=7)
        self.assertEqual(a, b)

    def test_different_seeds_can_differ(self):
        text = ("I am going to tell you, it is really good, and it is very "
                "exciting, and cool, and great, yes indeed, so funny.")
        outs = {perform(text, MILLENNIAL, seed=s) for s in range(10)}
        self.assertGreater(len(outs), 1)

    def test_filler_caps_never_twice_in_row(self):
        text = "Well, I think, you know, it is fine, honestly, it is fine, really."
        for seed in range(20):
            out = perform(text, {"filler_rate": 0.3, "slang_level": 0}, seed=seed)
            words = re.findall(r"[A-Za-z']+", out.lower())
            fillers = {"um", "uh", "well"}
            # multi-word "you know" checked separately
            for i in range(len(words) - 1):
                if words[i] in fillers:
                    self.assertNotIn(words[i + 1], fillers,
                                     f"consecutive fillers at seed {seed}: {out}")
            self.assertNotRegex(out.lower(), r"you know,?\s+(um|uh|well|you know|like)\b")
            self.assertNotRegex(out.lower(), r"\b(um|uh|well|like),?\s+you know\b")

    def test_slang_level_zero(self):
        text = "It is really very good, great and cool, so funny, yes, exciting!"
        out = perform(text, {"slang_level": 0, "filler_rate": 0}, seed=3)
        low = out.lower()
        for _, slang, _ in _SLANG:
            self.assertNotIn(slang.lower(), low, f"slang {slang!r} leaked: {out}")

    def test_pause_punctuation_present(self):
        out = perform("Hello, world... take a breath\nnext line here.", {}, seed=0)
        self.assertIn("[pause:150ms]", out)
        self.assertIn("[beat:300ms]", out)
        self.assertIn("[breath]", out)

    def test_energy_tags_in_range(self):
        out = perform("First sentence here. Second sentence here! Third one?", {},
                      seed=1)
        tags = re.findall(r"\[pace:([+-]?\d+)%\]", out)
        self.assertTrue(tags, f"no pace tags: {out}")
        for t in tags:
            self.assertGreaterEqual(int(t), -5)
            self.assertLessEqual(int(t), 5)

    def test_pre_answer_beat_marker(self):
        out = perform("Hello there.", {}, seed=0)
        self.assertTrue(out.startswith("[beat:150ms]"))

    def test_contractions(self):
        out = perform("I am going to tell you, I do not know.", {}, seed=0)
        low = out.lower()
        self.assertIn("gonna", low)
        self.assertIn("dunno", low)

    def test_defaults_when_spec_absent(self):
        a = perform("Hello, world. It is good.", None, seed=0)
        b = perform("Hello, world. It is good.", {}, seed=0)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("[beat:150ms]"))


if __name__ == "__main__":
    unittest.main()
