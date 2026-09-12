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

from ._shared import (
    DEFAULT_USER_AGENT,
    ProviderError,
    encode_multipart_formdata,
    logger,
    pcm16_to_wav_bytes,
    redacted_repr,
)


class STTProvider(abc.ABC):
    #: Can this tyre be called repeatedly on a growing prefix of one
    #: utterance, cheaply enough to be worth it? `server/stt_stream.py` reads
    #: this flag and nothing else — it never infers capability from a class
    #: name. False is the honest default: a tyre that pays an HTTPS round trip
    #: per window would be slower AND billed per call, and a tyre that reloads
    #: its model per call (mlx today) would be slower still. Streaming with
    #: `PET_TALK_STT_STREAM=1` on a tyre that says False is refused by name
    #: (`stt_stream_unsupported_provider`), never quietly downgraded.
    supports_streaming: bool = False

    @abc.abstractmethod
    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        """PCM16 mono bytes -> text. Raises ProviderError on failure."""
        raise NotImplementedError


class StubSTT(STTProvider):
    """No model. Returns fixed text so the loop runs with zero downloads.

    `delay_s` (default 0) sleeps before returning, so lifecycle tests can
    simulate a slow STT backend without a real model.
    """

    #: No model, so a repeated decode costs `delay_s` and nothing else.
    supports_streaming = True

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

    #: In-process and model-loaded-once, so a repeated prefix decode is legal.
    #: It writes a temp WAV per call, which makes it the slowest of the local
    #: tyres to stream; faster-whisper is the measured one.
    supports_streaming = True

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

    #: One HTTPS round trip per window, billed per call. Deepgram DOES have a
    #: real streaming socket (`wss://api.deepgram.com/v1/listen`) that would
    #: belong here instead of repeated POSTs — deliberately out of scope for
    #: lane 2b (2026-09-12), which fixed the local default first.
    supports_streaming = False

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
                "User-Agent": DEFAULT_USER_AGENT,
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
                "User-Agent": DEFAULT_USER_AGENT,
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
                "User-Agent": DEFAULT_USER_AGENT,
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

    NOT MEASURED on this machine (2026-09-12, lane 2b): `whisperkit-cli` is
    not on PATH and no CoreML model is on disk, so nothing here has ever run.
    Its latency is unknown — do not repeat the guess that the Neural Engine
    makes it the fastest tyre until somebody has a number.
    """

    #: One process spawn plus one temp WAV per window, and the model is
    #: reloaded by that process every time. Streaming this would spend more
    #: than it saves; a persistent whisperkit daemon could change the answer.
    supports_streaming = False

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

    #: `mlx_whisper.transcribe` reloads the weights on every call (docs/SPEC.md
    #: 10), so a per-window decode would pay a model load per window. Flip
    #: this the day this tyre loads once, not before.
    supports_streaming = False

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
            headers={
                "User-Agent": DEFAULT_USER_AGENT,"Content-Type": "audio/wav"},
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
    """Local faster-whisper backend running on CPU. Zero cloud dependencies.

    The default STT tyre (2026-09-12) because it is the only one measured
    inside the 150ms ``stt_ms`` budget from this machine: 143ms p50 / 148ms
    p95 on the 1.37s `qa/fixtures/weather_turn.wav`, against Groq
    whisper-large-v3-turbo at 306ms and Deepgram nova-3 at 1432ms — both of
    which are paying this machine's real HTTPS round trip, not model time.

    Three things buy that number, all of them measured, none of them a
    guess (default 193ms -> beam/language 169ms -> this 143ms):

    * **No temp file.** The PCM goes to the model as a float32 ndarray, so a
      transcribe is not also a WAV encode plus two filesystem round trips.
    * **`beam_size=1`, `language="en"`.** Greedy decode, and no language
      detection pass on audio we already know is English.
    * **`vad_filter=False`, `without_timestamps=True`.** The turn's own VAD
      has already cut the clip; a second VAD pass and per-segment timestamps
      are work whose output nothing reads.

    The accuracy cost is real and named: greedy decode on `tiny.en` is the
    weakest setting in the family. It transcribes the fixture exactly, but a
    harder clip will do worse than `base.en` (332ms p50, over budget) or the
    cloud tyres. `WHISPER_MODEL` swaps the model without a code change.
    """

    #: 16kHz is whisper's native rate and the wire protocol's default, so the
    #: resample below is normally a no-op.
    TARGET_SAMPLE_RATE = 16000

    #: The streaming tyre. Model loads once, the PCM goes in as an ndarray
    #: with no temp file, and a 0.7s window decodes in a fraction of the
    #: whole-utterance pass — see `server/stt_stream.py` and docs/SPEC.md 9.2.
    supports_streaming = True

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

    def _as_float32(self, pcm16_bytes: bytes, sample_rate: int):
        """int16 PCM -> mono float32 at 16kHz, or None when numpy is absent."""
        try:
            import numpy as np  # type: ignore
        except ImportError:
            return None
        audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != self.TARGET_SAMPLE_RATE and len(audio) > 1:
            n = int(len(audio) * self.TARGET_SAMPLE_RATE / sample_rate)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio
            ).astype(np.float32)
        return audio

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            raise ProviderError("stt_empty_audio", "no bytes to transcribe")
        model = self._load()
        decode = dict(
            beam_size=1,
            language="en",
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        audio = self._as_float32(pcm16_bytes, sample_rate)
        if audio is not None:
            segments, _ = model.transcribe(audio, **decode)
            text = " ".join(s.text.strip() for s in segments).strip()
        else:
            # No numpy: fall back to the WAV/temp-file path. Correct, ~50ms
            # slower, and it keeps this tyre usable on a bare interpreter.
            wav = pcm16_to_wav_bytes(pcm16_bytes, sample_rate=sample_rate)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
                f.write(wav)
                f.flush()
                segments, _ = model.transcribe(f.name, **decode)
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
    # Local first, and on measurement rather than taste: faster-whisper is the
    # only tyre inside the 150ms `stt_ms` budget from this machine (143ms p50
    # vs Groq 306ms vs Deepgram 1432ms, 2026-09-12). Both cloud tyres stay one
    # env var away — STT_PROVIDER=groq|deepgram.
    default_provider = "faster-whisper"
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
