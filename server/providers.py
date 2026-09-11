"""Provider ABCs + stub implementations + real Kokoro-over-SpacePilot TTS.

Contract: TECH-SPEC.md section 3. Every model swappable via config only.
Heavy ML deps are NEVER imported here — lazy imports inside methods only.
Fail-closed: every failure raises ProviderError with a named reason.
"""
from __future__ import annotations

import abc
import asyncio
import io
import math
import os
import struct
import time
import urllib.parse
import urllib.request
import wave
from typing import AsyncIterator, Optional


class ProviderError(RuntimeError):
    """Fail-closed error with a machine-readable reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- STT ---


class STTProvider(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        """PCM16 mono bytes -> text. Raises ProviderError on failure."""
        raise NotImplementedError


class StubSTT(STTProvider):
    """No model. Returns fixed text so the loop runs with zero downloads."""

    def __init__(self, fixed_text: str = "hello agent, what is the weather") -> None:
        self.fixed_text = fixed_text

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        return self.fixed_text


class WhisperLocalSTT(STTProvider):
    """Real backend (lazy import). Not installed by default."""

    def __init__(self, model_name: str = "base") -> None:
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                import whisper  # type: ignore  # lazy: pip install openai-whisper
            except ImportError as e:
                raise ProviderError("stt_whisper_not_installed", str(e))
            self._model = whisper.load_model(self.model_name)
        return self._model

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import tempfile  # stdlib, safe

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        model = self._load()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            with wave.open(f.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(pcm16_bytes)
            result = model.transcribe(f.name)
        text = (result.get("text") or "").strip()
        if not text:
            raise ProviderError("stt_empty_result", "whisper returned no text")
        return text


# ---------------------------------------------------------------- LLM ---


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield sentence/token chunks. Raises ProviderError on failure."""
        raise NotImplementedError
        yield  # pragma: no cover — keeps this an async generator

    @abc.abstractmethod
    def route(self, text: str) -> str:
        """Return 'stall' (research/worker path) or 'answer' (direct path)."""
        raise NotImplementedError


RESEARCH_TRIGGERS = (
    "research",
    "look up",
    "search",
    "find out",
    "investigate",
    "deep dive",
    "what is the weather",
    "weather",
)


def route_text(text: str) -> str:
    lowered = text.lower()
    for trigger in RESEARCH_TRIGGERS:
        if trigger in lowered:
            return "stall"
    return "answer"


class StubLLM(LLMProvider):
    """Streams canned sentences with a small delay so queueing is exercised."""

    def __init__(self, sentences: Optional[list[str]] = None) -> None:
        self.sentences = sentences or [
            "Got it, working on that now.",
            "I found three relevant points for you.",
            "Here is the summary of what matters most.",
        ]

    def route(self, text: str) -> str:
        return route_text(text)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        if not messages:
            raise ProviderError("llm_empty_messages", "no messages to complete")
        for sentence in self.sentences:
            await asyncio.sleep(0.05)
            yield sentence


class OpenAICompatibleLLM(LLMProvider):
    """Real backend (lazy import). Any OpenAI-compatible endpoint via config."""

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key

    def route(self, text: str) -> str:
        return route_text(text)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        try:
            from openai import AsyncOpenAI  # type: ignore  # lazy: pip install openai
        except ImportError as e:
            raise ProviderError("llm_openai_not_installed", str(e))
        client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key or "x")
        try:
            resp = await client.chat.completions.create(
                model=self.model, messages=messages, stream=True
            )
            buf = ""
            async for chunk in resp:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if not delta:
                    continue
                buf += delta
                while True:
                    split_idx = -1
                    for i, ch in enumerate(buf):
                        if ch in (".", "!", "?", "\n"):
                            if i + 1 == len(buf) or buf[i + 1].isspace():
                                split_idx = i + 1
                                break
                    if split_idx != -1:
                        sentence = buf[:split_idx].strip()
                        buf = buf[split_idx:].lstrip()
                        if sentence:
                            yield sentence
                    else:
                        break
            if buf.strip():
                yield buf.strip()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("llm_stream_failed", str(e))


# ---------------------------------------------------------------- TTS ---


def _sine_wav_bytes(
    duration_s: float = 0.5, freq_hz: float = 440.0, sample_rate: int = 22050
) -> bytes:
    """Deterministic 0.5s sine WAV — no model, no download, no numpy needed."""
    n = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            w.writeframes(struct.pack("<h", sample))
    return buf.getvalue()


class TTSProvider(abc.ABC):
    @abc.abstractmethod
    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        """Text -> (wav_bytes, word_times). Raises ProviderError on failure."""
        raise NotImplementedError


class StubTTS(TTSProvider):
    """Writes 0.5s sine WAV so the loop runs with NO model downloads."""

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        return _sine_wav_bytes(), []


class KokoroSpacePilotTTS(TTSProvider):
    """Real backend: SpacePilot daemon over HTTP.

    Endpoint shape (per coordinator brief):
      POST {base}/api/generate/voice {text, voice, speed} -> {job_id}
      GET  {base}/api/jobs/{id} -> {status, file_path?...}
      download file_path (server-relative) -> wav bytes
    Auth: bearer token read from STUDIO_TOKEN_FILE env path (fail-closed).
    Uses only stdlib (urllib) so no extra deps.
    """

    POLL_INTERVAL_S = 0.25
    TIMEOUT_S = 120.0

    def __init__(self, base_url: str = "http://127.0.0.1:8088") -> None:
        self.base_url = base_url.rstrip("/")
        self._token: Optional[str] = None

    def _auth_token(self) -> str:
        if self._token is not None:
            return self._token
        token_file = os.environ.get("STUDIO_TOKEN_FILE", "")
        if not token_file:
            raise ProviderError("tts_no_token", "STUDIO_TOKEN_FILE env not set")
        try:
            with open(token_file) as f:
                self._token = f.read().strip()
        except OSError as e:
            raise ProviderError("tts_token_unreadable", str(e))
        if not self._token:
            raise ProviderError("tts_token_empty", f"{token_file} is empty")
        return self._token

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        import json as _json

        data = _json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-SpacePilot-Token": self._auth_token(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _json.loads(resp.read().decode())
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("tts_request_failed", f"{method} {path}: {e}")

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        job = self._request(
            "POST", "/api/generate/voice", {"text": text, "voice": voice, "speed": speed}
        )
        job_id = job.get("job_id", "")
        if not job_id:
            raise ProviderError("tts_no_job_id", f"daemon replied: {job}")
        deadline = time.time() + self.TIMEOUT_S
        file_path = ""
        while time.time() < deadline:
            status = self._request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}")
            state = status.get("status", "")
            if state in ("done", "completed", "succeeded"):
                # audio_url is the routable download; file_path is server-local.
                file_path = status.get("audio_url", "") or status.get("file_path", "")
                break
            if state in ("failed", "error"):
                raise ProviderError("tts_job_failed", str(status))
            time.sleep(self.POLL_INTERVAL_S)
        if not file_path:
            raise ProviderError("tts_job_timeout", f"job {job_id} not done in {self.TIMEOUT_S}s")
        url = (
            file_path
            if file_path.startswith("http")
            else self.base_url + urllib.parse.quote(file_path)
        )
        req = urllib.request.Request(
            url, headers={"X-SpacePilot-Token": self._auth_token()}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), []
        except Exception as e:
            raise ProviderError("tts_download_failed", str(e))


# ------------------------------------------------- ElevenLabs (swappable tyre) ---


class ElevenLabsTTS(TTSProvider):
    """Cloud tyre: ElevenLabs TTS. Same interface, env-selected, never default.

    Select with TTS_PROVIDER=elevenlabs. Key from ELEVENLABS_API_KEY env
    (Doppler). Model crc32-logged per call for credit accounting.
    Uses only stdlib (urllib).
    """

    API = "https://api.elevenlabs.io/v1/text-to-speech"

    def __init__(self, voice_id: str = "21m00Tcm4TlvDq8ikWAM", model_id: str = "eleven_flash_v2_5") -> None:
        # Default voice_id is Rachel (English female); pass-through otherwise.
        self.voice_id = voice_id
        self.model_id = model_id

    def _key(self) -> str:
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            raise ProviderError("tts_no_key", "ELEVENLABS_API_KEY env not set")
        return key

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        vid = voice if voice not in ("af_heart", "") else self.voice_id
        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed},
        }
        data = json_dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.API}/{urllib.parse.quote(vid)}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "xi-api-key": self._key()},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio = resp.read()
        except Exception as e:
            raise ProviderError("tts_request_failed", f"elevenlabs: {e}")
        if len(audio) < 1000:
            raise ProviderError("tts_empty_audio", f"elevenlabs returned {len(audio)} bytes")
        return audio, []


# -------------------------------------------------- Deepgram (swappable tyre) ---


class DeepgramSTT(STTProvider):
    """Cloud tyre: Deepgram listen API. Same interface, env-selected.

    Select with STT_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default model nova-3. Uses only stdlib (urllib).
    """

    LISTEN_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, model: str = "nova-3") -> None:
        self.model = model

    def _key(self) -> str:
        key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not key:
            raise ProviderError("stt_no_key", "DEEPGRAM_API_KEY env not set")
        return key

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm16_bytes)
        wav = buf.getvalue()
        url = (
            f"{self.LISTEN_URL}?model={urllib.parse.quote(self.model)}"
            f"&encoding=linear16&sample_rate={sample_rate}&channels=1&punctuate=true"
        )
        req = urllib.request.Request(
            url,
            data=wav,
            method="POST",
            headers={
                "Content-Type": "audio/wav",
                "Authorization": f"Token {self._key()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = _json.loads(resp.read().decode())
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("stt_request_failed", f"deepgram: {e}")
        try:
            text = (
                body["results"]["channels"][0]["alternatives"][0]["transcript"] or ""
            ).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise ProviderError("stt_bad_response", f"deepgram: {e}")
        if not text:
            raise ProviderError("stt_empty_result", "deepgram returned no text")
        return text


class DeepgramTTS(TTSProvider):
    """Cloud tyre: Deepgram speak API (Aura family). Same interface.

    Select with TTS_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default voice aura-2-thalia-en (Aura-2 family). Uses only stdlib.
    """

    SPEAK_URL = "https://api.deepgram.com/v1/speak"

    def __init__(self, model: str = "aura-2-thalia-en") -> None:
        self.model = model

    def _key(self) -> str:
        key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not key:
            raise ProviderError("tts_no_key", "DEEPGRAM_API_KEY env not set")
        return key

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        model = voice if voice not in ("af_heart", "") else self.model
        url = (
            f"{self.SPEAK_URL}?model={urllib.parse.quote(model)}"
            "&encoding=linear16&container=wav"
        )
        data = json_dumps({"text": text}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {self._key()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio = resp.read()
        except Exception as e:
            raise ProviderError("tts_request_failed", f"deepgram: {e}")
        if len(audio) < 1000:
            raise ProviderError("tts_empty_audio", f"deepgram returned {len(audio)} bytes")
        return audio, []


def json_dumps(payload: dict) -> str:
    import json as _json

    return _json.dumps(payload)


def make_tts(provider: Optional[str] = None, base_url: Optional[str] = None) -> TTSProvider:
    """Tyre switch: TTS_PROVIDER=stub|kokoro|elevenlabs|deepgram (default: stub)."""
    which = (provider or os.environ.get("TTS_PROVIDER", "stub")).lower()
    if which == "kokoro":
        return KokoroSpacePilotTTS(base_url=base_url or os.environ.get("KOKORO_BASE_URL", "http://127.0.0.1:8088"))
    if which == "elevenlabs":
        return ElevenLabsTTS()
    if which == "deepgram":
        return DeepgramTTS()
    return StubTTS()


def make_stt(provider: Optional[str] = None, api_key: Optional[str] = None) -> STTProvider:
    """Tyre switch: STT_PROVIDER=stub|deepgram (default: stub).

    whisper-local is not wired yet — fail-closed, never silent fallback.
    """
    which = (provider or os.environ.get("STT_PROVIDER", "stub")).lower()
    if which == "deepgram":
        return DeepgramSTT(api_key=api_key or os.environ.get("DEEPGRAM_API_KEY", ""))
    if which in ("whisper-local", "whisper_local", "whisper", "local"):
        raise ProviderError("stt_not_wired", "whisper-local not wired")
    return StubSTT()


def make_llm(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMProvider:
    """Tyre switch: LLM_PROVIDER=stub|openai|litellm|fleet (default: stub)."""
    which = (provider or os.environ.get("LLM_PROVIDER", "stub")).lower()
    if which in ("openai", "litellm", "fleet", "local"):
        b_url = base_url or os.environ.get("LLM_BASE_URL", "http://100.99.50.84:8000/v1")
        m = model or os.environ.get("LLM_MODEL", "claude-3-7-sonnet")
        key = api_key or os.environ.get("LLM_API_KEY", "x")
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)
    return StubLLM()


# ---------------------------------------------------------------- VAD ---


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
