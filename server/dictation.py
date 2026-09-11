"""server/dictation.py — Clean Prose formatting engine for dictation STT.

Wispr Flow style prompt cleaning:
- Strips filler tokens: "uh", "um", "like", "you know", "er", "ah".
- Cleans repeated stutter words ("the the" -> "the").
- Preserves and formats technical syntax (app.py, git commit -m, CamelCase, snake_case).
- Auto-capitalizes and fixes sentence punctuation.
"""
from __future__ import annotations

import re

# File extensions commonly dictated in developer codebases
KNOWN_EXTENSIONS = (
    "py", "ts", "tsx", "js", "jsx", "json", "yaml", "yml", "md",
    "sh", "css", "html", "rs", "go", "c", "cpp", "h", "toml", "txt", "sql"
)

QUESTION_STARTERS = (
    "who", "what", "where", "when", "why", "how",
    "can", "could", "would", "should", "is", "are", "was", "were",
    "do", "does", "did", "will", "won't", "can't", "isn't", "aren't"
)


class CleanProseFormatter:
    """Deterministic, pure-python text normalizer for spoken dictation transcripts."""

    @classmethod
    def format_technical_syntax(cls, text: str) -> str:
        """Format spoken technical terms, file extensions, flags, and snake_case."""
        if not text:
            return ""

        # 1. File extensions: "app dot py" -> "app.py", "index dot html" -> "index.html"
        ext_pattern = r'\b([a-zA-Z0-9_\-]+)\s+dot\s+(' + '|'.join(KNOWN_EXTENSIONS) + r')\b'
        text = re.sub(ext_pattern, r'\1.\2', text, flags=re.IGNORECASE)

        # 2. Git command flags:
        # "git commit dash m" -> "git commit -m"
        # "git checkout dash b" -> "git checkout -b"
        text = re.sub(
            r'\bgit\s+([a-zA-Z0-9_\-]+)\s+(?:dash|minus)\s+([a-zA-Z0-9])\b',
            r'git \1 -\2',
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r'\bgit\s+([a-zA-Z0-9_\-]+)\s+(?:double\s+dash|dash\s+dash)\s+([a-zA-Z0-9_\-]+)\b',
            r'git \1 --\2',
            text,
            flags=re.IGNORECASE,
        )

        # 3. Spoken snake_case: "make underscore stt" -> "make_stt"
        # Loop to handle consecutive underscores e.g. "a underscore b underscore c"
        while re.search(r'\b([a-zA-Z0-9]+)\s+underscore\s+([a-zA-Z0-9]+)\b', text, flags=re.IGNORECASE):
            text = re.sub(
                r'\b([a-zA-Z0-9]+)\s+underscore\s+([a-zA-Z0-9]+)\b',
                r'\1_\2',
                text,
                flags=re.IGNORECASE,
            )

        return text

    @classmethod
    def clean_fillers(cls, text: str) -> str:
        """Strip filler tokens: "uh", "um", "like", "you know", "er", "ah"."""
        if not text:
            return ""

        # 1. Strip fixed multi-word phrases: "you know"
        text = re.sub(r'\byou\s+know\b', '', text, flags=re.IGNORECASE)

        # 2. Strip basic filler syllables: "uh", "um", "er", "ah" (and elongated variants: "uhh", "umm", etc.)
        text = re.sub(r'\b(uh+|um+|er+|ah+)\b', '', text, flags=re.IGNORECASE)

        # 3. Strip hesitative / filler "like"
        # Avoid stripping valid grammatical usages like "I like cats", "would like to", "looks like", etc.
        # Strip "like" when:
        # - at sentence/text beginning: "^like " or ", like,"
        # - hesitation before verbs: "was like thinking", "to like fix", "we should like deploy"
        # - hesitation around punctuation: ", like, "
        # - standalone / repeated filler lists: "uh um like you know"
        text = re.sub(r'^\s*like\b[,]?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'[,–—]\s*like\b[,–—]?', ',', text, flags=re.IGNORECASE)
        text = re.sub(
            r'\b(we|i|you|they|he|she|it|to|should|could|would|was|were|is|are|just|can|will)\s+like\s+([a-zA-Z]+(?:ing|ed|e|s|en)?)\b',
            lambda m: f"{m.group(1)} {m.group(2)}" if m.group(1).lower() not in ("would", "should", "i", "we", "they", "you") or m.group(2).lower() in ("thinking", "saying", "going", "doing", "fix", "deploy", "build", "run", "make", "create") else m.group(0),
            text,
            flags=re.IGNORECASE,
        )
        # If any remaining isolated "like" is surrounded only by whitespace or punctuation in filler-only text:
        tokens = [t.strip(",.?! ") for t in text.split()]
        if all(t.lower() in ("like", "", "uh", "um", "er", "ah", "you", "know") for t in tokens):
            return ""

        # Clean up stray commas or double spaces left after removing fillers
        text = re.sub(r',\s*,+', ',', text)
        text = re.sub(r'^\s*,\s*', '', text)
        text = re.sub(r'\s*,\s*$', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @classmethod
    def clean_stutters(cls, text: str) -> str:
        """Collapse immediate repeated stutter words ("the the" -> "the")."""
        if not text:
            return ""

        pattern = r'\b([a-zA-Z]+)(?:[,\s]+\1\b)+'
        # Preserve casing of the first word
        return re.sub(pattern, r'\1', text, flags=re.IGNORECASE)

    @classmethod
    def format_punctuation(cls, text: str) -> str:
        """Auto-capitalize first letter and fix sentence termination."""
        text = text.strip()
        if not text:
            return ""

        # Clean spaces before punctuation
        text = re.sub(r'\s+([,;:\.?!])', r'\1', text)
        # Collapse multi-commas or comma before period
        text = re.sub(r',\s*\.', '.', text)
        text = re.sub(r',\s*\?', '?', text)

        # Capitalize first character unless it starts with a technical symbol / filename
        first_token = text.split()[0]
        is_technical = (
            any(first_token.endswith("." + ext) for ext in KNOWN_EXTENSIONS)
            or "_" in first_token
            or (len(first_token) > 1 and first_token[0].islower() and any(c.isupper() for c in first_token[1:]))  # camelCase
        )
        if not is_technical:
            text = text[0].upper() + text[1:]

        # Terminal punctuation
        if not text.endswith(('.', '?', '!')):
            first_word = first_token.lower().strip(",.?!")
            if first_word in QUESTION_STARTERS:
                text += "?"
            else:
                text += "."

        return text

    @classmethod
    def format(cls, text: str) -> str:
        """Run the full Clean Prose pipeline on raw transcript text."""
        if not text or not text.strip():
            return ""

        # Step 1: Format spoken technical syntax (app dot py -> app.py, etc.)
        t = cls.format_technical_syntax(text.strip())

        # Step 2: Clean filler tokens (uh, um, like, you know)
        t = cls.clean_fillers(t)

        # Step 3: Collapse repeated stutter words (the the -> the)
        t = cls.clean_stutters(t)

        # Step 4: Fix sentence capitalization and punctuation
        t = cls.format_punctuation(t)

        return t
