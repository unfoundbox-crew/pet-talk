"""SpeakQueue — a real producer/consumer FIFO between the LLM and TTS.

The LLM producer pushes sentences as fast as it streams them; a separate
consumer task synthesizes and sends. That decoupling is the point: when the
LLM outruns TTS the queue buffers ahead (TECH-SPEC section 9, gate 3 — three
sentences behind the playing audio), and a barge can drop the whole buffer in
one call with an honest count.

``flush()`` returns the true number of sentences that will never be spoken:
everything still queued plus the one the consumer has in flight.
"""
from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass, field
from typing import Optional

from .providers import ProviderError

#: Fallback speaking rate when a TTS backend returns no word times.
ESTIMATED_MS_PER_WORD = 350


@dataclass(frozen=True)
class ResumePoint:
    """Where speech should pick up after a barge."""

    text: str
    word_idx: int
    start_ms: int
    estimated: bool


@dataclass
class SpokenSentence:
    """One synthesized sentence and its word timings."""

    seq: int
    text: str
    word_times: list[dict] = field(default_factory=list)
    estimated: bool = True

    @classmethod
    def from_synth(
        cls, seq: int, text: str, word_times: Optional[list[dict]]
    ) -> "SpokenSentence":
        times = list(word_times or [])
        if times:
            estimated = any(bool(w.get("estimated")) for w in times if isinstance(w, dict))
            return cls(seq=seq, text=text, word_times=times, estimated=estimated)
        return cls(seq=seq, text=text, word_times=estimate_word_times(text), estimated=True)


def estimate_word_times(text: str) -> list[dict]:
    """Uniform word timings for a backend that reports none. Always estimated."""
    words = text.split()
    out: list[dict] = []
    for i, word in enumerate(words):
        out.append(
            {
                "word": word,
                "start_ms": i * ESTIMATED_MS_PER_WORD,
                "end_ms": (i + 1) * ESTIMATED_MS_PER_WORD,
                "estimated": True,
            }
        )
    return out


class SpeakQueue:
    """Async FIFO of sentences with flush (barge) and resume support.

    One instance per socket, reused across turns: ``reopen()`` before a turn's
    producer starts, ``close()`` when it finishes, ``flush()`` on barge.
    """

    def __init__(self) -> None:
        self._q: collections.deque[str] = collections.deque()
        self._cond = asyncio.Condition()
        self._closed = False
        self._inflight: Optional[str] = None
        self._high_water = 0
        self._pushed = 0
        self._last_spoken: Optional[SpokenSentence] = None

    # -- producer -------------------------------------------------------

    async def push(self, sentence: str) -> int:
        """Enqueue one sentence. Returns the queue depth after the push."""
        async with self._cond:
            if self._closed:
                raise ProviderError("queue_closed", "push after close")
            self._q.append(sentence)
            self._pushed += 1
            depth = len(self._q)
            self._high_water = max(self._high_water, depth)
            self._cond.notify_all()
            return depth

    async def close(self) -> None:
        """Signal the producer is done; waiting consumers drain then stop."""
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    async def reopen(self) -> None:
        """Reset for a new turn. Drops any residue from the previous one."""
        async with self._cond:
            self._q.clear()
            self._closed = False
            self._inflight = None
            self._high_water = 0
            self._pushed = 0
            self._cond.notify_all()

    # -- consumer -------------------------------------------------------

    async def get(self) -> Optional[str]:
        """Wait for the next sentence. None once the turn is closed and drained.

        The returned sentence counts as *in flight* until
        :meth:`done_with_inflight` — so a barge landing during its synthesis
        still counts it as dropped.
        """
        async with self._cond:
            while not self._q and not self._closed:
                await self._cond.wait()
            if self._q:
                self._inflight = self._q.popleft()
                return self._inflight
            self._inflight = None
            return None

    async def done_with_inflight(self, spoken: Optional[SpokenSentence] = None) -> None:
        """Mark the in-flight sentence delivered (or abandoned)."""
        async with self._cond:
            self._inflight = None
            if spoken is not None:
                self._last_spoken = spoken

    async def pop(self) -> Optional[str]:
        """Non-blocking dequeue. Kept for callers that poll rather than wait."""
        async with self._cond:
            if not self._q:
                return None
            self._inflight = self._q.popleft()
            return self._inflight

    # -- barge ----------------------------------------------------------

    async def flush(self) -> int:
        """Drop everything unspoken. Returns the true count dropped.

        Counts the queued sentences plus the one mid-synthesis, and closes the
        queue so a consumer blocked in :meth:`get` wakes up and exits.
        """
        async with self._cond:
            dropped = len(self._q) + (1 if self._inflight is not None else 0)
            self._q.clear()
            self._inflight = None
            self._closed = True
            self._cond.notify_all()
            return dropped

    # -- introspection --------------------------------------------------

    async def size(self) -> int:
        """Sentences waiting to be synthesized."""
        async with self._cond:
            return len(self._q)

    async def stats(self) -> dict[str, int]:
        async with self._cond:
            return {
                "depth": len(self._q),
                "high_water": self._high_water,
                "pushed": self._pushed,
                "inflight": 1 if self._inflight is not None else 0,
            }

    @property
    def high_water(self) -> int:
        """Deepest the buffer ever got this turn — the buffer-ahead receipt."""
        return self._high_water

    @property
    def closed(self) -> bool:
        return self._closed

    # -- resume ---------------------------------------------------------

    async def resume_from(
        self,
        word_idx: int,
        sentence: Optional[str] = None,
        word_times: Optional[list[dict]] = None,
    ) -> ResumePoint:
        """Where to restart speech, given how many words already played.

        Uses the TTS word times for the sentence (the last one spoken when the
        caller does not name one). Timings may be estimated — the
        ``estimated`` flag rides along so the client can treat the offset as
        approximate rather than a seek point.

        Fails closed on an out-of-range index (law 1).
        """
        async with self._cond:
            last = self._last_spoken
        if sentence is None:
            if last is None:
                raise ProviderError("queue_no_spoken_sentence", "nothing spoken yet")
            sentence = last.text
            if word_times is None:
                word_times = last.word_times
                estimated = last.estimated
            else:
                estimated = any(
                    bool(w.get("estimated")) for w in word_times if isinstance(w, dict)
                )
        else:
            if word_times is None:
                if last is not None and last.text == sentence:
                    word_times = last.word_times
                    estimated = last.estimated
                else:
                    word_times = estimate_word_times(sentence)
                    estimated = True
            else:
                estimated = any(
                    bool(w.get("estimated")) for w in word_times if isinstance(w, dict)
                )

        words = sentence.split()
        if word_idx < 0 or word_idx > len(words):
            raise ProviderError(
                "queue_bad_word_idx", f"{word_idx} of {len(words)} words"
            )

        start_ms = 0
        if word_times and word_idx < len(word_times):
            entry = word_times[word_idx]
            if isinstance(entry, dict):
                try:
                    start_ms = int(entry.get("start_ms", 0))
                except (TypeError, ValueError):
                    start_ms = word_idx * ESTIMATED_MS_PER_WORD
                    estimated = True
        elif word_times:
            entry = word_times[-1]
            if isinstance(entry, dict):
                try:
                    start_ms = int(entry.get("end_ms", 0))
                except (TypeError, ValueError):
                    start_ms = len(words) * ESTIMATED_MS_PER_WORD
                    estimated = True
        else:
            start_ms = word_idx * ESTIMATED_MS_PER_WORD
            estimated = True

        return ResumePoint(
            text=" ".join(words[word_idx:]),
            word_idx=word_idx,
            start_ms=start_ms,
            estimated=estimated,
        )
