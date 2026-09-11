"""Provider construction — the tyre factory wrapper.

Law 1: fail closed with a named reason. A tyre that cannot be built is not
quietly replaced by something that works; it becomes an ``Unavailable*``
provider that re-raises the original :class:`ProviderError` the moment
anything uses it, and the reason is listed in ``/health``'s ``degraded``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from .logs import log, swallowed
from .providers import (
    LLMProvider,
    ProviderError,
    STTProvider,
    TTSProvider,
    make_llm,
    make_stt,
    make_tts,
    route_text,
)
from .settings import (
    LLM_REQUIRED_KEY,
    STT_REQUIRED_KEY,
    TTS_REQUIRED_KEY,
    RuntimeSettings,
)


class UnavailableSTT(STTProvider):
    """Placeholder that re-raises the build failure on first use."""

    def __init__(self, error: ProviderError) -> None:
        self.error = error

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        raise self.error


class UnavailableTTS(TTSProvider):
    def __init__(self, error: ProviderError) -> None:
        self.error = error

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        raise self.error


class UnavailableLLM(LLMProvider):
    def __init__(self, error: ProviderError) -> None:
        self.error = error

    def route(self, text: str) -> str:
        return route_text(text)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        raise self.error
        yield  # pragma: no cover — keeps this an async generator


@dataclass(frozen=True)
class ProviderSet:
    """The three tyres behind one turn, snapshotted so a mid-turn swap
    cannot split a turn across two provider generations."""

    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    degraded: tuple[str, ...] = ()

    def class_names(self) -> dict[str, str]:
        return {
            "stt": type(self.stt).__name__,
            "llm": type(self.llm).__name__,
            "tts": type(self.tts).__name__,
        }


def _require_key(kind: str, which: str, table: dict[str, str], key: str) -> None:
    env_var = table.get(which)
    if env_var and not str(key or "").strip():
        raise ProviderError("missing_api_key", env_var)


def build_providers(settings: RuntimeSettings) -> ProviderSet:
    """Build all three tyres. Never raises — failures become Unavailable*."""
    degraded: list[str] = []

    def _build(kind: str, fn) -> Any:
        try:
            return fn()
        except ProviderError as e:
            degraded.append(f"{kind}:{e.reason}" + (f":{e.detail}" if e.detail else ""))
            log.warning("provider_unavailable kind=%s reason=%s detail=%s", kind, e.reason, e.detail)
            return e
        except Exception as e:  # a factory that misbehaves must not kill boot
            reason = swallowed("provider_build_failed", e, kind=kind)
            degraded.append(f"{kind}:{reason}")
            return ProviderError(reason, str(e))

    def _stt() -> STTProvider:
        _require_key("stt", settings.stt_provider, STT_REQUIRED_KEY, settings.key_for_stt())
        return make_stt(
            provider=settings.stt_provider,
            api_key=settings.key_for_stt() or None,
            base_url=settings.sensevoice_base_url or None,
        )

    def _llm() -> LLMProvider:
        _require_key("llm", settings.llm_provider, LLM_REQUIRED_KEY, settings.key_for_llm())
        return make_llm(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url or None,
            model=settings.llm_model or None,
            api_key=settings.key_for_llm() or None,
        )

    def _tts() -> TTSProvider:
        _require_key("tts", settings.tts_provider, TTS_REQUIRED_KEY, settings.key_for_tts())
        return make_tts(
            provider=settings.tts_provider,
            base_url=settings.kokoro_base_url or None,
            api_key=settings.key_for_tts() or None,
        )

    stt = _build("stt", _stt)
    llm = _build("llm", _llm)
    tts = _build("tts", _tts)
    return ProviderSet(
        stt=UnavailableSTT(stt) if isinstance(stt, ProviderError) else stt,
        llm=UnavailableLLM(llm) if isinstance(llm, ProviderError) else llm,
        tts=UnavailableTTS(tts) if isinstance(tts, ProviderError) else tts,
        degraded=tuple(degraded),
    )


