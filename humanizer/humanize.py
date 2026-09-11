"""Humanizer preprocessor for pet-talk (§8.2). Stdlib only.

Pure function: (text, spec) -> performed_text. Six ordered passes:
1. Contractions (gonna/wanna/dunno per tone)
2. Fillers at clause boundaries (p=filler_rate, never twice in a row)
3. Pause punctuation (, -> 150ms, ... -> beat, line break -> breath)
4. Slang injection (capped by slang_level)
5. Energy variance (+-5% pace tags per sentence)
6. Deliberate 150ms pre-answer beat marker

Deterministic under `seed` via `random.Random(seed)`; no global random.
"""

import random
import re

DEFAULTS = {
    "voice_affect": "neutral",
    "tone": "casual",
    "pacing": "medium",
    "emotion": "neutral",
    "pronunciation": [],
    "pauses": [],
    "emphasis": [],
    "delivery": "conversational",
    "filler_rate": 0.1,
    "slang_level": 1,
    "backchannel": "off",
}

# Pass 1: (pattern, replacement), applied in order, case-preserving for lead cap.
_CONTRACTIONS_FULL = [
    (r"\bgoing to\b", "gonna"),
    (r"\bwant to\b", "wanna"),
    (r"\bdo not know\b", "dunno"),
    (r"\bgot to\b", "gotta"),
    (r"\bkind of\b", "kinda"),
    (r"\bsort of\b", "sorta"),
    (r"\byou are\b", "you're"),
    (r"\bthey are\b", "they're"),
    (r"\bwe are\b", "we're"),
    (r"\bit is\b", "it's"),
    (r"\bthat is\b", "that's"),
    (r"\bthere is\b", "there's"),
    (r"\bcannot\b", "can't"),
    (r"\bdo not\b", "don't"),
    (r"\bdoes not\b", "doesn't"),
    (r"\bwill not\b", "won't"),
    (r"\bI am\b", "I'm"),
    (r"\byou have\b", "you've"),
]
# Formal tone: only the neutral contractions, no gonna/wanna/dunno/gotta/kinda/sorta.
_CONTRACTIONS_FORMAL = [p for p in _CONTRACTIONS_FULL if p[1] not in (
    "gonna", "wanna", "dunno", "gotta", "kinda", "sorta")]

_FILLERS = ["um", "uh", "you know", "like", "well"]

# Pass 4: (plain, slangy, min_level). Level 0 injects nothing.
_SLANG = [
    ("really", "totally", 1),
    ("very", "super", 1),
    ("good", "awesome", 1),
    ("great", "amazing", 1),
    ("tired", "wiped", 2),
    ("exciting", "fire", 2),
    ("cool", "iconic", 2),
    ("funny", "hilarious", 2),
    ("yes", "yass", 2),
    ("no way", "nah, no way", 2),
]

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT = re.compile(r"(\n+|,\s*|;\s*|\.\.\.|—|--)")


def _with_spec(spec):
    merged = dict(DEFAULTS)
    if spec:
        for k, v in spec.items():
            if k in merged:
                merged[k] = v
    # clamp
    try:
        merged["filler_rate"] = max(0.0, min(0.3, float(merged["filler_rate"])))
    except (TypeError, ValueError):
        merged["filler_rate"] = DEFAULTS["filler_rate"]
    try:
        merged["slang_level"] = max(0, min(2, int(merged["slang_level"])))
    except (TypeError, ValueError):
        merged["slang_level"] = DEFAULTS["slang_level"]
    return merged


def _match_case(replacement, original):
    if original and original[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def _pass1_contract(text, spec):
    table = _CONTRACTIONS_FORMAL if str(spec.get("tone", "")).lower() == "formal" \
        else _CONTRACTIONS_FULL
    for pat, repl in table:
        text = re.sub(pat, lambda m: _match_case(repl, m.group(0)), text,
                      flags=re.IGNORECASE)
    return text


def _pass2_fillers(text, spec, rng):
    rate = spec["filler_rate"]
    if rate <= 0:
        return text
    parts = _CLAUSE_SPLIT.split(text)
    out = []
    prev_was_filler = False
    filler_set = set(_FILLERS)
    for part in parts:
        out.append(part)
        if _CLAUSE_SPLIT.fullmatch(part or "") and part.strip():
            # never twice in a row: skip if previous emitted token was a filler
            tail = "".join(out[:-1])
            last_word = re.findall(r"[A-Za-z']+", tail[-24:])
            if last_word and last_word[-1].lower() in filler_set:
                prev_was_filler = True
            if prev_was_filler:
                prev_was_filler = False
                continue
            if rng.random() < rate:
                filler = rng.choice(_FILLERS)
                out.append(" " + filler + ",")
                prev_was_filler = True
            else:
                prev_was_filler = False
    return "".join(out).replace("  ", " ")


def _pass3_pause_punctuation(text):
    # ... -> beat marker (before comma rule so ", ..." orders sanely)
    text = text.replace("...", " [beat:300ms]")
    # line break -> breath marker (keep the break for TTS queue readability)
    text = re.sub(r"\n+", " [breath] ", text)
    # comma -> 150ms pause tag (skip commas already followed by a tag/filler insertion)
    text = re.sub(r",(?!\s*\[)", ", [pause:150ms]", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _pass4_slang(text, spec, rng):
    level = spec["slang_level"]
    if level <= 0:
        return text
    for plain, slang, min_level in _SLANG:
        if min_level > level:
            continue
        # level 1: 35% chance per occurrence; level 2: 70%
        p = 0.35 if level == 1 else 0.70
        text = re.sub(r"\b" + re.escape(plain) + r"\b",
                      lambda m: _match_case(slang, m.group(0)) if rng.random() < p
                      else m.group(0),
                      text, flags=re.IGNORECASE)
    return text


def _pass5_energy(text, rng):
    sentences = _SENT_SPLIT.split(text)
    out = []
    for s in sentences:
        if not s.strip():
            continue
        pct = rng.randint(-5, 5)
        sign = "+" if pct >= 0 else ""
        out.append(f"[pace:{sign}{pct}%] " + s.strip())
    return " ".join(out)


def _pass6_pre_answer_beat(text):
    return "[beat:150ms] " + text.strip()


def perform(text, spec=None, seed=0):
    """Run the six ordered humanizer passes. Pure; deterministic under seed."""
    if spec is None:
        spec = {}
    cfg = _with_spec(spec)
    rng = random.Random(seed)
    text = _pass1_contract(text, cfg)
    text = _pass2_fillers(text, cfg, rng)
    text = _pass3_pause_punctuation(text)
    text = _pass4_slang(text, cfg, rng)
    text = _pass5_energy(text, rng)
    text = _pass6_pre_answer_beat(text)
    return text
