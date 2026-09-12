"""Voice activity detection."""
from __future__ import annotations

import abc
import math
import struct

from ._shared import ProviderError


class VAD(abc.ABC):
    @abc.abstractmethod
    def voice_onset(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> bool:
        raise NotImplementedError


class EnergyGateVAD(VAD):
    """v0.2 energy gate (TECH-SPEC). Neural backend later, same interface."""

    def __init__(self, threshold_rms: float = 500.0) -> None:
        self.threshold_rms = threshold_rms

    def voice_onset(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> bool:
        if len(pcm16_bytes) < 2:
            return False
        n = len(pcm16_bytes) // 2
        fmt = f"<{n}h"
        try:
            samples = struct.unpack(fmt, pcm16_bytes[: n * 2])
        except struct.error:
            raise ProviderError("vad_bad_pcm", "odd-length pcm16 buffer")
        mean_sq = sum(s * s for s in samples) / n
        return math.sqrt(mean_sq) > self.threshold_rms
