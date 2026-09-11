"""Provider ABCs + stub implementations + real backend adapters.

Contract: TECH-SPEC.md section 3. Every model swappable via config only.
Heavy ML deps are NEVER imported here — lazy imports inside methods only.
Fail-closed: every failure raises ProviderError with a named reason.

This is a package (`server/providers/`) split by concern:
  `_shared.py` — ProviderError, secret-safe repr, WAV/word-time helpers,
                 sentence-splitting shared by every LLM stream() impl.
  `stt.py`     — STTProvider + all speech-to-text backends + make_stt().
  `llm.py`     — LLMProvider + OpenAICompatibleLLM + make_llm().
  `tts.py`     — TTSProvider + all text-to-speech backends + make_tts().
  `vad.py`     — VAD + EnergyGateVAD.

Every public name below is re-exported at the package root, so both
`from server import providers; providers.X` and `from server.providers
import X` keep working exactly as they did when this was a single module.
"""
from __future__ import annotations

from ._shared import (
    ProviderError,
    encode_multipart_formdata,
    estimate_word_times,
    finalize_think,
    json_dumps,
    logger,
    pcm16_to_wav_bytes,
    pcm_duration_ms,
    redacted_repr,
    split_sentences,
    strip_closed_think_tags,
)
from .llm import (
    RESEARCH_TRIGGERS,
    LLMProvider,
    OpenAICompatibleLLM,
    StubLLM,
    make_llm,
    route_text,
)
from .stt import (
    DeepgramSTT,
    FasterWhisperSTT,
    GroqSTT,
    MLXWhisperSTT,
    OpenAIWhisperSTT,
    SenseVoiceSTT,
    STTProvider,
    StubSTT,
    WhisperKitSTT,
    WhisperLocalSTT,
    make_stt,
)
from .tts import (
    DeepgramTTS,
    ElevenLabsTTS,
    KokoroSpacePilotTTS,
    SmallestAITTS,
    StubTTS,
    TTSProvider,
    make_tts,
)
from .vad import VAD, EnergyGateVAD

__all__ = [
    "ProviderError",
    "encode_multipart_formdata",
    "estimate_word_times",
    "finalize_think",
    "json_dumps",
    "logger",
    "pcm16_to_wav_bytes",
    "pcm_duration_ms",
    "redacted_repr",
    "split_sentences",
    "strip_closed_think_tags",
    "RESEARCH_TRIGGERS",
    "LLMProvider",
    "OpenAICompatibleLLM",
    "StubLLM",
    "make_llm",
    "route_text",
    "DeepgramSTT",
    "FasterWhisperSTT",
    "GroqSTT",
    "MLXWhisperSTT",
    "OpenAIWhisperSTT",
    "SenseVoiceSTT",
    "STTProvider",
    "StubSTT",
    "WhisperKitSTT",
    "WhisperLocalSTT",
    "make_stt",
    "DeepgramTTS",
    "ElevenLabsTTS",
    "KokoroSpacePilotTTS",
    "SmallestAITTS",
    "StubTTS",
    "TTSProvider",
    "make_tts",
    "VAD",
    "EnergyGateVAD",
]
