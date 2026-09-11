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
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
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
    "what is going on",
    "what's going on",
    "how are things",
    "status report",
    "check on",
    "explain",
    "analyze",
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
    """Real backend: Any OpenAI-compatible endpoint via config.
    Uses httpx async SSE streaming with automatic remote-to-local fallback.
    """

    def __init__(
        self,
        base_url: str = "http://100.99.50.84:8000/v1",
        model: str = "claude-sonnet-4-6",
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = (
            api_key
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a")
        )

    def route(self, text: str) -> str:
        return route_text(text)

    async def _resolve_endpoint(self) -> str:
        """Resolve base_url, falling back from offline Lenovo (100.99.50.84) to local 127.0.0.1."""
        target = self.base_url
        if "100.99.50.84" in target:
            import socket

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect(("100.99.50.84", 8000))
            except Exception:
                target = target.replace("100.99.50.84:8000", "127.0.0.1:8000")
            finally:
                sock.close()
        return target

    def _build_payload(self, messages: Any, system_prompt: str = "") -> dict[str, Any]:
        """Format request payload with model-specific parameters.
        Reasoning models (gpt-5, o1, o3) use max_completion_tokens and omit temperature.
        Standard models use max_tokens and temperature.
        """
        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        if isinstance(messages, list):
            for m in messages:
                if isinstance(m, tuple) and len(m) == 2:
                    formatted_messages.append({"role": m[0], "content": m[1]})
                elif isinstance(m, dict):
                    formatted_messages.append(m)
        elif isinstance(messages, str):
            formatted_messages.append({"role": "user", "content": messages})

        is_reasoning = any(x in self.model for x in ("gpt-5", "o1", "o3"))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "stream": True,
        }
        if is_reasoning:
            payload["max_completion_tokens"] = 300
        else:
            payload["max_tokens"] = 50
            payload["temperature"] = 0.7
        return payload

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        import json as _json

        try:
            import httpx
        except ImportError:
            httpx = None

        endpoint = await self._resolve_endpoint()
        url = f"{endpoint}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or 'x'}",
        }
        payload = self._build_payload(messages)

        if httpx is not None:
            buf = ""
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as resp:
                        if resp.status_code != 200:
                            err_body = await resp.aread()
                            raise ProviderError(
                                "llm_stream_failed",
                                f"HTTP {resp.status_code}: {err_body.decode(errors='replace')}",
                            )
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = _json.loads(data_str)
                                delta = chunk["choices"][0]["delta"].get("content") or ""
                            except Exception:
                                continue
                            buf += delta
                            if "<think>" in buf and "</think>" in buf:
                                import re
                                buf = re.sub(r"<think>[\s\S]*?</think>", "", buf).lstrip()
                            elif "<think>" in buf and "</think>" not in buf:
                                continue
                            while True:
                                split_idx = -1
                                for i, ch in enumerate(buf):
                                    if ch in (".", "!", "?", "\n"):
                                        if i + 1 == len(buf) or buf[i + 1].isspace():
                                            split_idx = i + 1
                                            break
                                    elif ch in (";", "—") and len(buf[:i].split()) >= 5:
                                        split_idx = i + 1
                                        break
                                    elif ch == "," and len(buf[:i].split()) >= 8:
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
                            if "<think>" in buf and "</think>" in buf:
                                import re
                                buf = re.sub(r"<think>[\s\S]*?</think>", "", buf).strip()
                            elif "<think>" in buf:
                                buf = ""
                            if buf.strip():
                                yield buf.strip()
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError("llm_stream_failed", str(e))
        else:
            try:
                from openai import AsyncOpenAI  # type: ignore

                client = AsyncOpenAI(base_url=endpoint, api_key=self.api_key or "x")
                create_params = {
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                }
                if is_reasoning:
                    create_params["max_completion_tokens"] = 300
                else:
                    create_params["max_tokens"] = 50
                    create_params["temperature"] = 0.7
                resp = await client.chat.completions.create(**create_params)
                buf = ""
                async for chunk in resp:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if not delta:
                        continue
                    buf += delta
                    if "<think>" in buf and "</think>" in buf:
                        import re
                        buf = re.sub(r"<think>[\s\S]*?</think>", "", buf).lstrip()
                    elif "<think>" in buf and "</think>" not in buf:
                        continue
                    while True:
                        split_idx = -1
                        for i, ch in enumerate(buf):
                            if ch in (".", "!", "?", "\n"):
                                if i + 1 == len(buf) or buf[i + 1].isspace():
                                    split_idx = i + 1
                                    break
                            elif ch in (";", "—") and len(buf[:i].split()) >= 5:
                                split_idx = i + 1
                                break
                            elif ch == "," and len(buf[:i].split()) >= 8:
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
                    if "<think>" in buf and "</think>" in buf:
                        import re
                        buf = re.sub(r"<think>[\s\S]*?</think>", "", buf).strip()
                    elif "<think>" in buf:
                        buf = ""
                    if buf.strip():
                        yield buf.strip()
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
        env_tok = (
            os.environ.get("SPACEPILOT_TOKEN")
            or os.environ.get("STUDIO_TOKEN")
            or os.environ.get("KOKORO_TOKEN")
        )
        if env_tok and env_tok.strip():
            self._token = env_tok.strip()
            return self._token
        token_file = os.environ.get("STUDIO_TOKEN_FILE", "")
        if token_file and os.path.exists(token_file):
            try:
                with open(token_file) as f:
                    tok = f.read().strip()
                if tok:
                    self._token = tok
                    return self._token
            except OSError:
                pass
        # Auto-fetch token from daemon /api/token
        try:
            req = urllib.request.Request(f"{self.base_url}/api/token")
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json as _json

                data = _json.loads(resp.read().decode())
                tok = data.get("token", "")
                if tok:
                    self._token = tok
                    return self._token
        except Exception:
            pass
        raise ProviderError("tts_no_token", "Could not obtain SpacePilot token from env, file, or /api/token")

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
            else self.base_url + (file_path if file_path.startswith("/") else f"/{file_path}")
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


# -------------------------------------------------- Smallest.ai (Lightning TTS) ---


class SmallestAITTS(TTSProvider):
    """Cloud tyre: Smallest AI Lightning TTS (https://api.smallest.ai/waves/v1/tts).
    Ultra-low latency Indian English & Hindi natural voice synthesis.
    Model: lightning_v3.1_pro, Voice: meher (or emily, radha, etc.).
    Uses stdlib (urllib).
    """

    API_URL = "https://api.smallest.ai/waves/v1/tts"

    def __init__(
        self,
        api_key: Optional[str] = None,
        voice_id: str = "meher",
        model: str = "lightning_v3.1_pro",
        sample_rate: int = 24000,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("SMALLEST_API_KEY", "")
        )
        self.voice_id = voice_id
        self.model = model
        self.sample_rate = sample_rate

    def synth(self, text: str, voice: str = "meher", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        if not self.api_key:
            raise ProviderError("tts_no_key", "SMALLEST_API_KEY env or key not provided")

        chosen_voice = voice if voice and voice not in ("af_heart", "default", "") else self.voice_id
        payload = {
            "text": text.strip(),
            "voice_id": chosen_voice,
            "model": self.model,
            "sample_rate": self.sample_rate,
            "speed": speed,
            "output_format": "wav",
        }
        data = json_dumps(payload).encode()
        req = urllib.request.Request(
            self.API_URL,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "audio/wav",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise ProviderError("tts_request_failed", f"HTTP {resp.status}")
                body = resp.read()
                if not body:
                    raise ProviderError("tts_empty_audio", "Smallest AI returned 0 bytes")
                return body, []
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("tts_request_failed", f"smallest.ai: {e}")


# -------------------------------------------------- STT Helpers & Providers ---


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


class DeepgramSTT(STTProvider):
    """Cloud flagship: Deepgram listen API. Same interface, env-selected.

    Select with STT_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default model nova-3. Uses only stdlib (urllib).
    """

    LISTEN_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, model: str = "nova-3", api_key: str = "") -> None:
        self.model = model
        self.api_key = api_key

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


class DeepgramTTS(TTSProvider):
    """Cloud tyre: Deepgram speak API (Aura family). Same interface.

    Select with TTS_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default voice aura-2-thalia-en (Aura-2 family). Uses only stdlib.
    """

    SPEAK_URL = "https://api.deepgram.com/v1/speak"

    def __init__(self, model: str = "aura-2-thalia-en", api_key: str = "") -> None:
        self.model = model
        self.api_key = api_key

    def _key(self) -> str:
        key = self.api_key or os.environ.get("DEEPGRAM_API_KEY", "")
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


def make_tts(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    voice: Optional[str] = None,
) -> TTSProvider:
    """Tyre switch: TTS_PROVIDER=kokoro|smallest|deepgram|elevenlabs|stub (default: kokoro)."""
    which = (provider or os.environ.get("TTS_PROVIDER", "kokoro")).lower()
    if which in ("smallest", "smallest-ai", "smallest_ai", "waves"):
        return SmallestAITTS(
            api_key=api_key or os.environ.get("SMALLEST_API_KEY"),
            voice_id=voice or "meher",
        )
    if which == "kokoro":
        return KokoroSpacePilotTTS(base_url=base_url or os.environ.get("KOKORO_BASE_URL", "http://127.0.0.1:8088"))
    if which == "elevenlabs":
        return ElevenLabsTTS()
    if which == "deepgram":
        return DeepgramTTS()
    if which == "stub":
        return StubTTS()
    return KokoroSpacePilotTTS(base_url=base_url or os.environ.get("KOKORO_BASE_URL", "http://127.0.0.1:8088"))


def make_stt(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> STTProvider:
    """Tyre switch for STT provider:
    deepgram | groq | sensevoice | whisperkit | faster-whisper | mlx | openai | stub.
    Fail-closed: raises ProviderError on unknown provider or missing credentials.
    """
    default_provider = "deepgram" if os.environ.get("DEEPGRAM_API_KEY") else "faster-whisper"
    which = (provider or os.environ.get("STT_PROVIDER", default_provider)).lower().strip()
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
            base_url=base_url or os.environ.get("SENSEVOICE_BASE_URL", "http://100.99.50.84:8086"),
        )
    raise ProviderError("stt_unknown_provider", f"unknown STT provider: {which}")


def make_llm(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> LLMProvider:
    """Tyre switch: LLM_PROVIDER=groq|openai|litellm|fleet|local|stub."""
    which = (provider or os.environ.get("LLM_PROVIDER", "litellm")).lower()
    if which == "groq":
        b_url = base_url or "https://api.groq.com/openai/v1"
        m = model or "groq/compound-mini"
        key = api_key or os.environ.get("GROQ_API_KEY", "")
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)
    if which in ("openai", "gpt"):
        b_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        m = model or os.environ.get("OPENAI_MODEL", "gpt-5-nano")
        key = (
            api_key
            or os.environ.get("OPENAI_API_KEY", "")
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)
    if which in ("litellm", "fleet", "local"):
        b_url = base_url or os.environ.get("LLM_BASE_URL", "http://100.99.50.84:8000/v1")
        m = model or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
        key = (
            api_key
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a")
        )
        return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)
    if which == "stub":
        return StubLLM()
    b_url = base_url or os.environ.get("LLM_BASE_URL", "http://100.99.50.84:8000/v1")
    m = model or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    key = (
        api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a")
    )
    return OpenAICompatibleLLM(base_url=b_url, model=m, api_key=key)


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
