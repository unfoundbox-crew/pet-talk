"""Shared, dependency-free helpers used by every provider submodule.

Nothing in this module imports from its siblings (`stt.py`, `llm.py`, `tts.py`,
`vad.py`) to avoid import cycles with the package `__init__.py`. Stdlib only.
"""
from __future__ import annotations

import io
import logging
import re
import uuid
import wave
from typing import Optional

logger = logging.getLogger("pet_talk.providers")


class ProviderError(RuntimeError):
    """Fail-closed error with a machine-readable, snake_case reason.

    Every provider failure — missing config, unreachable host, bad
    credentials, malformed upstream response — raises this instead of
    silently degrading. `reason` is what callers branch on; `detail` is
    free-form context for logs (never a secret value).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def redacted_repr(obj: object, *, secret_attrs: tuple[str, ...]) -> str:
    """Build a `repr()` for a provider that never prints a secret value.

    Any attribute named in `secret_attrs` is rendered as `***` (or
    `<empty>` if falsy) instead of its real value. Use this from a
    provider's `__repr__` whenever it holds an API key or token.
    """
    parts = []
    for key, value in vars(obj).items():
        if key in secret_attrs:
            parts.append(f"{key}=***" if value else f"{key}=<empty>")
        else:
            parts.append(f"{key}={value!r}")
    return f"{type(obj).__name__}({', '.join(parts)})"


def pcm16_to_wav_bytes(pcm16_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Pack PCM16 mono bytes into a compliant in-memory RIFF WAV container."""
    if pcm16_bytes.startswith(b"RIFF"):
        return pcm16_bytes
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16_bytes)
    return buf.getvalue()


def pcm_duration_ms(wav_bytes: bytes) -> int:
    """Read a RIFF/WAVE header and return the clip's duration in milliseconds.

    Used only for word-time *estimation* — never for playback — so a
    malformed or empty input returns 0 rather than raising.
    """
    if not wav_bytes:
        return 0
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or 1
            return int(frames * 1000 / rate)
    except (wave.Error, EOFError, OSError):
        return 0


def estimate_word_times(text: str, duration_ms: int) -> list[dict]:
    """Estimate per-word timing by splitting `duration_ms` across words
    proportionally to word length. Every entry carries `estimated: True` —
    callers must never claim these are the backend's real timestamps.
    """
    words = text.split()
    if not words or duration_ms <= 0:
        return []
    total_chars = sum(len(w) for w in words) or 1
    times: list[dict] = []
    cursor = 0.0
    for word in words:
        share = len(word) / total_chars
        duration = duration_ms * share
        start = cursor
        end = cursor + duration
        times.append(
            {"word": word, "start_ms": int(start), "end_ms": int(end), "estimated": True}
        )
        cursor = end
    return times


def encode_multipart_formdata(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    """Pure stdlib multipart/form-data encoder (RFC 7578) with zero extra deps."""
    boundary = f"----PetTalkBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, val in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(val).encode("utf-8"))
        body.extend(b"\r\n")
    for name, (fname, content, ctype) in files.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def json_dumps(payload: dict) -> str:
    import json as _json

    return _json.dumps(payload)


# --------------------------------------------------- sentence splitting ---

_SENTENCE_ENDERS = (".", "!", "?", "\n")
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")


def strip_closed_think_tags(buf: str) -> Optional[str]:
    """Remove any fully-closed `<think>...</think>` reasoning spans from `buf`.

    Returns `None` when an opening `<think>` tag is present without its
    closing tag yet — the caller should hold off splitting and wait for
    more streamed text before looking at this buffer again.
    """
    if "<think>" in buf:
        if "</think>" in buf:
            return _THINK_RE.sub("", buf).lstrip()
        return None
    return buf


def finalize_think(buf: str) -> str:
    """Final-flush variant of `strip_closed_think_tags`: an unterminated
    `<think>` block at end-of-stream is discarded (reasoning preamble that
    never resolved into spoken text), never spoken.
    """
    if "<think>" in buf:
        if "</think>" in buf:
            return _THINK_RE.sub("", buf).strip()
        return ""
    return buf.strip()


def split_sentences(buf: str) -> tuple[list[str], str]:
    """Extract every complete sentence-sized chunk currently in `buf`.

    The single splitting code path shared by every `LLMProvider.stream()`
    implementation (previously duplicated ~70 lines apiece between the
    httpx and no-httpx branches of `OpenAICompatibleLLM`). Splits on
    `.`/`!`/`?`/newline followed by whitespace-or-EOF, and — to keep
    spoken chunks short for low-latency TTS — also on `;`/`—` once at
    least 5 words have accumulated, or `,` once at least 8 words have.

    Returns `(sentences, remainder)`: the remainder is unconsumed text to
    prepend to the next incoming delta.
    """
    sentences: list[str] = []
    while True:
        split_idx = -1
        for i, ch in enumerate(buf):
            if ch in _SENTENCE_ENDERS:
                if i + 1 == len(buf) or buf[i + 1].isspace():
                    split_idx = i + 1
                    break
            elif ch in (";", "—") and len(buf[:i].split()) >= 5:
                split_idx = i + 1
                break
            elif ch == "," and len(buf[:i].split()) >= 8:
                split_idx = i + 1
                break
        if split_idx == -1:
            break
        sentence = buf[:split_idx].strip()
        buf = buf[split_idx:].lstrip()
        if sentence:
            sentences.append(sentence)
    return sentences, buf
