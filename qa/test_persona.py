#!/usr/bin/env python3
"""qa/test_persona.py — persona / voice / i18n consistency (TECH-SPEC sec 5).

Spec contract under test:
  - every personas/*.md parses; frontmatter keys `voice` and `speed` present
  - every voice id referenced exists in voices.yaml (else flagged UNVERIFIED)
  - i18n en/hi key sets identical (web/src/i18n/*.json)

Stdlib only (unittest + json + re). Missing inputs SKIP honestly — a skip
means "unprovable yet", never a pass. Run: `python3 qa/test_persona.py -v`.
"""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONAS_DIR = os.path.join(ROOT, "personas")
VOICES_CANDIDATES = [os.path.join(ROOT, "voices.yaml"),
                     os.path.join(ROOT, "personas", "voices.yaml")]
I18N_EN = os.path.join(ROOT, "web", "src", "i18n", "en.json")
I18N_HI = os.path.join(ROOT, "web", "src", "i18n", "hi.json")

REQUIRED_FRONTMATTER = ("voice", "speed")

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_frontmatter(text):
    """Minimal `key: value` frontmatter parse. Returns (dict|None, error|None)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, "no --- frontmatter block"
    fm = {}
    for lineno, line in enumerate(m.group(1).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("- "):
            continue
        if ":" not in line:
            return None, "line %d not key: value (%r)" % (lineno, line)
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip("\"'")
    return fm, None


def load_voice_ids(path):
    """Tiny top-level-key reader for voices.yaml (stdlib has no YAML lib).

    Understands `id:` keys at indent 0, or one level under a `voices:` map.
    Returns (set_of_ids, error|None).
    """
    ids, under_voices, child_indent = set(), False, None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip(" "))
                m = re.match(r"([A-Za-z0-9_.-]+)\s*:", line.strip())
                if not m:
                    continue
                key = m.group(1)
                if indent == 0:
                    under_voices = (key == "voices")
                    child_indent = None
                    if key != "voices":
                        ids.add(key)
                elif under_voices:
                    if child_indent is None:
                        child_indent = indent  # first child level = voice ids
                    if indent == child_indent:
                        ids.add(key)  # deeper levels are attributes, ignored
    except OSError as e:
        return set(), str(e)
    return ids, None


def flat_keys(obj, prefix=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= flat_keys(v, prefix + str(k) + ".")
    else:
        out.add(prefix.rstrip("."))
    return out


def persona_files():
    if not os.path.isdir(PERSONAS_DIR):
        return None
    return sorted(f for f in os.listdir(PERSONAS_DIR) if f.endswith(".md"))


class TestPersonas(unittest.TestCase):
    def test_persona_frontmatter(self):
        files = persona_files()
        if files is None:
            self.skipTest("SKIP: personas/ not present — frontmatter gate unprovable")
        if not files:
            self.skipTest("SKIP: personas/ empty — nothing to check")
        for fname in files:
            with self.subTest(persona=fname):
                with open(os.path.join(PERSONAS_DIR, fname), encoding="utf-8") as f:
                    fm, err = parse_frontmatter(f.read())
                self.assertIsNotNone(fm, "%s: %s" % (fname, err))
                assert fm is not None
                for key in REQUIRED_FRONTMATTER:
                    self.assertIn(key, fm, "%s: frontmatter lacks '%s'" % (fname, key))
        print("    personas parsed: %d (%s)" % (len(files), ", ".join(files)))

    def test_voice_ids_resolve(self):
        files = persona_files()
        if files is None or not files:
            self.skipTest("SKIP: no personas — voice-reference gate unprovable")
        voices_file = next((p for p in VOICES_CANDIDATES if os.path.isfile(p)), None)
        if voices_file is None:
            self.skipTest("SKIP: voices.yaml absent (looked in %s) — every voice id "
                           "UNVERIFIED (checked %d persona(s), 0 resolvable)"
                           % (" and ".join(os.path.relpath(p, ROOT) for p in VOICES_CANDIDATES),
                              len(files)))
        voice_ids, err = load_voice_ids(voices_file)
        self.assertIsNone(err, "voices.yaml unreadable: %s" % err)
        unverified = []
        for fname in files:
            with open(os.path.join(PERSONAS_DIR, fname), encoding="utf-8") as f:
                fm, _ = parse_frontmatter(f.read())
            vid = (fm or {}).get("voice", "")
            if vid not in voice_ids:
                unverified.append("%s->%r" % (fname, vid))
        self.assertEqual(unverified, [],
                         "voice ids missing from voices.yaml: %s" % unverified)
        print("    voices resolved: %d ids in %s"
              % (len(voice_ids), os.path.relpath(voices_file, ROOT)))


class TestI18n(unittest.TestCase):
    def test_en_hi_key_sets_identical(self):
        missing = [p for p in (I18N_EN, I18N_HI) if not os.path.isfile(p)]
        if missing:
            self.skipTest("SKIP: i18n file(s) absent (%s) — key-parity gate unprovable"
                          % ", ".join(os.path.relpath(p, ROOT) for p in missing))
        with open(I18N_EN, encoding="utf-8") as f:
            en = json.load(f)
        with open(I18N_HI, encoding="utf-8") as f:
            hi = json.load(f)
        en_keys, hi_keys = flat_keys(en), flat_keys(hi)
        self.assertEqual(en_keys, hi_keys,
                         "en-only: %s | hi-only: %s"
                         % (sorted(en_keys - hi_keys), sorted(hi_keys - en_keys)))
        print("    i18n parity: %d keys in en + hi" % len(en_keys))


if __name__ == "__main__":
    unittest.main(verbosity=2)
