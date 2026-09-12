"""Speech-to-text providers.

Contract: TECH-SPEC.md section 3 — `transcribe(pcm16_bytes, sample_rate=16000) -> str`,
sync, raises `ProviderError` on any failure. Heavy ML deps are lazy-imported
inside methods only, never at module import time.
"""
from __future__ import annotations

import abc
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import wave
from typing import Optional

from ._shared import ProviderError, encode_multipart_formdata, logger, pcm16_to_wav_bytes, redacted_repr


class STTProvider(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        """PCM16 mono bytes -> text. Raises ProviderError on failure."""
        raise NotImplementedError


class StubSTT(STTProvider):
    """No model. Returns fixed text so the loop runs with zero downloads.

    `delay_s` (default 0) sleeps before returning, so lifecycle tests can
    simulate a slow STT backend without a real model.
    """

    def __init__(self, fixed_text: str = "hello agent, what is the weather", delay_s: float = 0.0) -> None:
        self.fixed_text = fixed_text
        self.delay_s = delay_s

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        if self.delay_s:
            import time

            time.sleep(self.delay_s)
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


class DeepgramSTT(STTProvider):
    """Cloud flagship: Deepgram listen API. Same interface, env-selected.

    Select with STT_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default model nova-3. Uses only stdlib (urllib).
    """

    LISTEN_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, model: str = "nova-3", api_key: str = "") -> None:
        self.model = model
        self.api_key = api_key

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

    def _key(self) -> str:
        key = self.api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        if not key:
            raise ProviderError("stt_no_key", "DEEPGRAM_API_KEY env not set")
        return key

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
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


class GroqSTT(STTProvider):
    """Ultra-fast Groq LPU Whisper. Same interface, env or param selected.

    Select with STT_PROVIDER=groq. Key from GROQ_API_KEY env or param.
    Default model whisper-large-v3-turbo. Uses only stdlib (urllib).
    """

    DEFAULT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(
        self,
        model: str = "whisper-large-v3-turbo",
        api_key: str = "",
        base_url: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("GROQ_BASE_URL", self.DEFAULT_URL)).strip()

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

    def _key(self) -> str:
        key = self.api_key or os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise ProviderError("stt_no_key", "GROQ_API_KEY env not set")
        return key

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        body, ctype = encode_multipart_formdata(
            fields={"model": self.model, "response_format": "json"},
            files={"file": ("audio.wav", wav, "audio/wav")},
        )
        req = urllib.request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": ctype,
                "Authorization": f"Bearer {self._key()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode())
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("stt_request_failed", f"groq: {e}")
        text = (data.get("text") or "").strip()
        if not text:
            raise ProviderError("stt_empty_result", "groq returned no text")
        return text


class OpenAIWhisperSTT(STTProvider):
    """OpenAI Whisper API. Same interface.

    Select with STT_PROVIDER=openai. Key from OPENAI_API_KEY env or param.
    Default model whisper-1. Uses only stdlib (urllib).
    """

    DEFAULT_URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(
        self,
        model: str = "whisper-1",
        api_key: str = "",
        base_url: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", self.DEFAULT_URL)).strip()

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

    def _key(self) -> str:
        key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ProviderError("stt_no_key", "OPENAI_API_KEY env not set")
        return key

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        body, ctype = encode_multipart_formdata(
            fields={"model": self.model},
            files={"file": ("audio.wav", wav, "audio/wav")},
        )
        req = urllib.request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": ctype,
                "Authorization": f"Bearer {self._key()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = _json.loads(resp.read().decode())
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("stt_request_failed", f"openai: {e}")
        text = (data.get("text") or "").strip()
        if not text:
            raise ProviderError("stt_empty_result", "openai returned no text")
        return text


class WhisperKitSTT(STTProvider):
    """Apple Neural Engine CoreML via CLI process.

    Select with STT_PROVIDER=whisperkit.
    Default model openai/whisper-large-v3_turbo.
    """

    def __init__(
        self,
        model: str = "openai/whisper-large-v3_turbo",
        cli_path: str = "whisperkit-cli",
    ) -> None:
        self.model = model
        self.cli_path = cli_path

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")

        resolved_cli = shutil.which(self.cli_path) or (self.cli_path if os.path.exists(self.cli_path) else None)
        if not resolved_cli:
            raise ProviderError(
                "stt_whisperkit_not_found",
                f"whisperkit-cli executable not found at '{self.cli_path}'",
            )

        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            f.write(wav)
            f.flush()

            cmd = [
                resolved_cli,
                "transcribe",
                "--audio-path",
                f.name,
                "--model",
                self.model,
                "--report",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )
            except Exception as e:
                raise ProviderError("stt_whisperkit_failed", str(e))

            if proc.returncode != 0:
                raise ProviderError(
                    "stt_whisperkit_failed",
                    f"exit {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}",
                )

            out = proc.stdout.strip()
            text = ""
            if out.startswith("{") and out.endswith("}"):
                try:
                    data = _json.loads(out)
                    text = data.get("text", "")
                except Exception:
                    text = out
            else:
                text = out

            if not text:
                raise ProviderError("stt_empty_result", "whisperkit returned no text")
            return text


class MLXWhisperSTT(STTProvider):
    """Apple Silicon GPU via MLX. Lazy import of mlx_whisper.

    Select with STT_PROVIDER=mlx.
    Default model mlx-community/whisper-large-v3-turbo.
    """

    def __init__(
        self,
        model: str = "mlx-community/whisper-large-v3-turbo",
    ) -> None:
        self.model = model

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")

        try:
            import mlx_whisper  # type: ignore  # lazy
        except ImportError as e:
            raise ProviderError("stt_mlx_not_installed", str(e))

        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            f.write(wav)
            f.flush()
            try:
                result = mlx_whisper.transcribe(f.name, path_or_hf_repo=self.model)
            except Exception as e:
                raise ProviderError("stt_mlx_failed", str(e))

        text = (result.get("text") or "").strip()
        if not text:
            raise ProviderError("stt_empty_result", "mlx returned no text")
        return text


class SenseVoiceSTT(STTProvider):
    """Sovereign Fleet SenseVoice HTTP adapter (:8086).

    Select with STT_PROVIDER=sensevoice. Base URL from SENSEVOICE_BASE_URL.
    """

    DEFAULT_URL = "http://127.0.0.1:8086"

    def __init__(
        self,
        base_url: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SENSEVOICE_BASE_URL", self.DEFAULT_URL)).rstrip("/")

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        import json as _json

        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")

        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        url = f"{self.base_url}/api/transcribe"
        req = urllib.request.Request(
            url,
            data=wav,
            method="POST",
            headers={"Content-Type": "audio/wav"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode())
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("stt_request_failed", f"sensevoice: {e}")

        text = (data.get("text") or "").strip()
        if not text:
            raise ProviderError("stt_empty_result", "sensevoice returned no text")
        return text


class FasterWhisperSTT(STTProvider):
    """Local faster-whisper backend running on CPU/Metal. Zero cloud dependencies."""

    def __init__(
        self,
        model_name: str = "tiny.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise ProviderError("stt_faster_whisper_not_installed", str(e))
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        model = self._load()
        wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            f.write(wav)
            f.flush()
            segments, _ = model.transcribe(f.name)
            text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            raise ProviderError("stt_empty_result", "faster-whisper returned no text")
        return text


def make_stt(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> STTProvider:
    """Tyre switch for STT provider:
    deepgram | groq | sensevoice | whisperkit | faster-whisper | mlx | openai | stub.
    Fail-closed: raises ProviderError on unknown provider or missing credentials.
    Construction is cheap and does no network I/O.
    """
    default_provider = "deepgram" if os.environ.get("DEEPGRAM_API_KEY") else "faster-whisper"
    which = (provider or os.environ.get("STT_PROVIDER", default_provider)).lower().strip()
    logger.debug("make_stt: selecting provider=%s", which)
    if which in ("", "stub"):
        return StubSTT()
    if which == "deepgram":
        return DeepgramSTT(
            model=model or "nova-3",
            api_key=api_key or os.environ.get("DEEPGRAM_API_KEY", ""),
        )
    if which == "groq":
        return GroqSTT(
            model=model or "whisper-large-v3-turbo",
            api_key=api_key or os.environ.get("GROQ_API_KEY", ""),
            base_url=base_url or os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1/audio/transcriptions"),
        )
    if which in ("openai", "openai-whisper", "whisper-openai"):
        return OpenAIWhisperSTT(
            model=model or "whisper-1",
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/audio/transcriptions"),
        )
    if which in ("whisperkit", "whisper-kit", "coreml", "ane"):
        return WhisperKitSTT(
            model=model or "openai/whisper-large-v3_turbo",
            cli_path=base_url or os.environ.get("WHISPERKIT_CLI_PATH", "whisperkit-cli"),
        )
    if which in ("faster-whisper", "faster_whisper", "whisper", "local", "whisper-local"):
        return FasterWhisperSTT(
            model_name=model or os.environ.get("WHISPER_MODEL", "tiny.en"),
        )
    if which in ("mlx", "mlx-whisper"):
        return MLXWhisperSTT(
            model=model or "mlx-community/whisper-large-v3-turbo",
        )
    if which in ("sensevoice", "fleet", "sovereign"):
        return SenseVoiceSTT(
            base_url=base_url or os.environ.get("SENSEVOICE_BASE_URL", "http://127.0.0.1:8086"),
        )
    raise ProviderError("stt_unknown_provider", f"unknown STT provider: {which}")
