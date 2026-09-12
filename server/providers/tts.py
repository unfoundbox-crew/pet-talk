"""Text-to-speech providers.

Contract: TECH-SPEC.md section 3 — `synth(text, voice, speed=1.0) -> (wav_bytes, word_times)`,
sync, raises `ProviderError` on failure. `word_times` is a list of
`{"word": str, "start_ms": int, "end_ms": int, "estimated": bool}` entries —
`estimated: False` only where the backend itself returned real per-word (or
per-character) timing; everything else is derived from WAV duration via
`estimate_word_times()` and marked `estimated: True`.
"""
from __future__ import annotations

import abc
import io
import math
import os
import struct
import threading
import time
import urllib.parse
import urllib.request
import wave
from typing import Optional

from ._shared import (
    DEFAULT_USER_AGENT,
    ProviderError,
    estimate_word_times,
    json_dumps,
    logger,
    pcm_duration_ms,
    redacted_repr,
)


def _sine_wav_bytes(
    duration_s: float = 0.5, freq_hz: float = 440.0, sample_rate: int = 22050
) -> bytes:
    """Deterministic sine WAV — no model, no download, no numpy needed."""
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
    #: True when ``synth`` accepts a ``cancel`` threading.Event and checks it
    #: inside its own sleep/poll loop. The server only passes the event to
    #: backends that advertise this, so the sync three-argument contract
    #: (TECH-SPEC section 3) still holds for every other tyre.
    supports_cancel = False

    @abc.abstractmethod
    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        """Text -> (wav_bytes, word_times). Raises ProviderError on failure."""
        raise NotImplementedError


class StubTTS(TTSProvider):
    """Writes a short sine WAV so the loop runs with NO model downloads.

    `delay_s` (default 0) sleeps before returning, so lifecycle tests can
    simulate a slow TTS backend without a real model. word_times are
    estimated (never real) and always carry `estimated: True`.
    """

    supports_cancel = True

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    def synth(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        cancel: Optional["threading.Event"] = None,
    ) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        if cancel is not None and cancel.is_set():
            raise ProviderError("tts_cancelled", "turn was barged before synth")
        if self.delay_s:
            # A barged turn must not hold a worker thread for the full delay.
            if cancel is not None:
                if cancel.wait(self.delay_s):
                    raise ProviderError("tts_cancelled", "turn barged mid-synth")
            else:
                time.sleep(self.delay_s)
        wav = _sine_wav_bytes()
        word_times = estimate_word_times(text, pcm_duration_ms(wav))
        return wav, word_times


class KokoroSpacePilotTTS(TTSProvider):
    """Real backend: SpacePilot daemon over HTTP.

    Endpoint shape (per coordinator brief):
      POST {base}/api/generate/voice {text, voice, speed} -> {job_id}
      GET  {base}/api/jobs/{id} -> {status, file_path?...}
      download file_path (server-relative) -> wav bytes
    Auth: bearer token read from STUDIO_TOKEN_FILE env path (fail-closed).
    Uses only stdlib (urllib) so no extra deps.

    No public schema documents word-level timestamps on the job-status
    response, so we don't assume a field name; if the daemon does provide
    one, `_extract_backend_word_times()` passes it through as real
    (`estimated: False`). Absent that, timing is estimated from the
    downloaded WAV's duration.
    """

    #: First poll goes out almost immediately and the interval backs off from
    #: there. A flat 0.5s tick charged every synth up to half a second of pure
    #: waiting: measured 2026-09-12, the same 12-word sentence took 1554ms p50
    #: on the flat tick and 1235ms p50 on a 25ms tick. Adaptive keeps that win
    #: without hammering the daemon on a long job.
    POLL_INTERVAL_S = 0.025
    POLL_MAX_INTERVAL_S = 0.1
    POLL_BACKOFF = 1.5
    supports_cancel = True

    def __init__(self, base_url: str = "http://127.0.0.1:8088", timeout_s: Optional[float] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else float(os.environ.get("KOKORO_TIMEOUT_S", "15"))
        self._token: Optional[str] = None

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("_token",))

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
                "User-Agent": DEFAULT_USER_AGENT,
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

    @staticmethod
    def _extract_backend_word_times(status: dict) -> Optional[list]:
        """Best-effort passthrough for a real word-timing field, if the
        daemon ever adds one. Returns None (caller then estimates) unless
        the field is present and shaped like our contract's list-of-dicts.
        """
        for key in ("word_times", "words", "timestamps"):
            raw = status.get(key)
            if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "word" in raw[0]:
                return [
                    {
                        "word": w.get("word", ""),
                        "start_ms": int(w.get("start_ms", w.get("start", 0))),
                        "end_ms": int(w.get("end_ms", w.get("end", 0))),
                        "estimated": bool(w.get("estimated", False)),
                    }
                    for w in raw
                ]
        return None

    def synth(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        cancel: Optional["threading.Event"] = None,
    ) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        if cancel is not None and cancel.is_set():
            raise ProviderError("tts_cancelled", "turn was barged before synth")
        job = self._request(
            "POST", "/api/generate/voice", {"text": text, "voice": voice, "speed": speed}
        )
        job_id = job.get("job_id", "")
        if not job_id:
            raise ProviderError("tts_no_job_id", f"daemon replied: {job}")
        deadline = time.time() + self.timeout_s
        file_path = ""
        status: dict = {}
        interval = self.POLL_INTERVAL_S
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                raise ProviderError("tts_cancelled", f"turn barged; abandoned job {job_id}")
            status = self._request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}")
            state = status.get("status", "")
            if state in ("done", "completed", "succeeded"):
                # audio_url is the routable download; file_path is server-local.
                file_path = status.get("audio_url", "") or status.get("file_path", "")
                break
            if state in ("failed", "error"):
                raise ProviderError("tts_job_failed", str(status))
            # Wait on the event, not the clock: a barge ends the poll at once.
            if cancel is not None:
                if cancel.wait(interval):
                    raise ProviderError(
                        "tts_cancelled", f"turn barged; abandoned job {job_id}"
                    )
            else:
                time.sleep(interval)
            interval = min(self.POLL_MAX_INTERVAL_S, interval * self.POLL_BACKOFF)
        if not file_path:
            raise ProviderError("tts_job_timeout", f"job {job_id} not done in {self.timeout_s}s")
        url = (
            file_path
            if file_path.startswith("http")
            else self.base_url + (file_path if file_path.startswith("/") else f"/{file_path}")
        )
        req = urllib.request.Request(
            url, headers={
                "User-Agent": DEFAULT_USER_AGENT,"X-SpacePilot-Token": self._auth_token()}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                wav = resp.read()
        except Exception as e:
            raise ProviderError("tts_download_failed", str(e))
        word_times = self._extract_backend_word_times(status)
        if word_times is None:
            word_times = estimate_word_times(text, pcm_duration_ms(wav))
        return wav, word_times


def _word_times_from_char_alignment(alignment: dict) -> list:
    """Group ElevenLabs' character-level alignment (from the
    `with-timestamps` endpoint) into word-level timings. Verified against
    ElevenLabs' documented response shape: `characters` plus
    `character_start_times_seconds` / `character_end_times_seconds`, both
    in seconds. Every entry here is real backend timing (`estimated: False`).
    """
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or len(chars) != len(starts) or len(starts) != len(ends):
        return []
    words: list = []
    cur_word = ""
    cur_start: Optional[float] = None
    cur_end = 0.0
    for ch, start, end in zip(chars, starts, ends):
        if ch.isspace():
            if cur_word:
                words.append(
                    {
                        "word": cur_word,
                        "start_ms": int(cur_start * 1000),
                        "end_ms": int(cur_end * 1000),
                        "estimated": False,
                    }
                )
                cur_word = ""
                cur_start = None
            continue
        if cur_start is None:
            cur_start = start
        cur_word += ch
        cur_end = end
    if cur_word and cur_start is not None:
        words.append(
            {"word": cur_word, "start_ms": int(cur_start * 1000), "end_ms": int(cur_end * 1000), "estimated": False}
        )
    return words


class KokoroLocalTTS(TTSProvider):
    """In-process Kokoro-82M on Apple Silicon via `mlx-audio`. No daemon, no
    network, no job queue, no poll tick.

    Select with ``TTS_PROVIDER=kokoro-local``. This is the default TTS tyre
    (2026-09-12) because it is 5x faster than the same model behind the
    SpacePilot daemon: 255ms p50 for a 12-word sentence against 1235ms for the
    daemon's POST/poll/download flow on its best tick, and 1939ms for Deepgram
    Aura-2 from this machine. Same weights (``hexgrad``/``prince-canuma``
    Kokoro-82M, ~330MB, already in the HF cache) — what goes away is the
    per-call HTTP round trips, the LUFS normalisation pass, and the file write
    plus download.

    **Still over budget, and not softened.** 255ms p50 vs a 200ms ``tts_ms``
    budget is a FAIL. Kokoro's real-time factor here is ~0.06, so any sentence
    past about nine words costs more than 200ms to synthesise in full, and
    this provider returns a complete WAV by contract. Meeting 200ms on a
    20-word sentence needs chunked synthesis that streams audio as it is
    produced — an architecture change (the ``audio_url``/`agent.sentence`
    contract assumes one finished WAV per sentence), not a provider tweak.
    What the product gets today: the first audible sentence is the persona's
    stall phrase, served from the RAM cache at zero synth cost, so the turn
    budget is met even while this one is not.

    Threading: MLX arrays are not safe to touch from two threads at once, and
    the server calls ``synth`` through ``asyncio.to_thread``. One lock
    serialises generation. The model is loaded and warmed once, in a
    background thread started at construction, so the first real turn does not
    pay the ~4.6s cold start (0.9s import and load, 3.7s first-synth graph
    build).

    Word timings are estimated from the WAV's duration like every other
    Kokoro path — mlx-audio's segments carry no per-word timestamps.
    """

    #: Any mlx-audio-compatible Kokoro repo; override with KOKORO_LOCAL_MODEL.
    DEFAULT_MODEL = "prince-canuma/Kokoro-82M"
    SAMPLE_RATE = 24000

    def __init__(self, model: Optional[str] = None, warm: bool = True) -> None:
        self.model_name = model or os.environ.get("KOKORO_LOCAL_MODEL", self.DEFAULT_MODEL)
        self._model = None
        self._lock = threading.Lock()
        self._load_error: Optional[ProviderError] = None
        if warm and os.environ.get("KOKORO_LOCAL_WARM", "1") != "0":
            # Daemon thread: construction stays cheap and non-blocking (the
            # factory contract), but a server that boots idle is warm by the
            # time a person speaks to it.
            threading.Thread(
                target=self._warm, name="kokoro-local-warm", daemon=True
            ).start()

    def __repr__(self) -> str:
        return f"KokoroLocalTTS(model={self.model_name!r}, loaded={self._model is not None})"

    def _warm(self) -> None:
        try:
            self.synth("Warming up.")
        except Exception as e:  # a failed warmup must never kill the process
            logger.warning("kokoro_local_warmup_failed: %s", e)

    def _load(self):
        """Load once. A load failure is remembered so every later call fails
        the same named way instead of re-paying a slow import to fail again."""
        if self._load_error is not None:
            raise self._load_error
        if self._model is not None:
            return self._model
        try:
            from mlx_audio.tts.utils import load_model  # type: ignore  # lazy, heavy
        except ImportError as e:
            self._load_error = ProviderError(
                "tts_mlx_audio_not_installed",
                f"{e} — pip install mlx-audio 'misaki[en]' into the interpreter "
                "running the server (Apple Silicon only)",
            )
            raise self._load_error
        try:
            self._model = load_model(self.model_name)
        except Exception as e:
            self._load_error = ProviderError("tts_local_load_failed", f"{self.model_name}: {e}")
            raise self._load_error
        return self._model

    def synth(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> tuple[bytes, list]:
        if not text or not text.strip():
            raise ProviderError("tts_empty_text", "nothing to synthesize")
        with self._lock:
            model = self._load()
            try:
                segments = list(model.generate(text=text, voice=voice, speed=speed))
            except Exception as e:
                raise ProviderError("tts_local_synth_failed", str(e))
        if not segments:
            raise ProviderError("tts_empty_audio", "kokoro-local produced no segments")
        wav = _wav_from_float_segments(segments, self.SAMPLE_RATE)
        return wav, estimate_word_times(text, pcm_duration_ms(wav))


def _wav_from_float_segments(segments: list, sample_rate: int) -> bytes:
    """Concatenate mlx-audio's float audio segments into one 16-bit WAV.

    The segments are MLX arrays of float samples in roughly [-1, 1]. numpy is
    already a hard dependency of mlx-audio, so using it here costs nothing.
    """
    import numpy as np  # type: ignore

    chunks = []
    for seg in segments:
        arr = np.asarray(seg.audio, dtype=np.float32).reshape(-1)
        chunks.append(arr)
        rate = getattr(seg, "sample_rate", None)
        if rate:
            sample_rate = int(rate)
    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class ElevenLabsTTS(TTSProvider):
    """Cloud tyre: ElevenLabs TTS. Same interface, env-selected, never default.

    Select with TTS_PROVIDER=elevenlabs. Key from ELEVENLABS_API_KEY env
    (Doppler). Uses the `with-timestamps` endpoint (verified against
    ElevenLabs' API docs) so word_times reflect real character-level
    alignment rather than an estimate. Uses only stdlib (urllib).
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
        import base64
        import json as _json

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
            f"{self.API}/{urllib.parse.quote(vid)}/with-timestamps",
            data=data,
            method="POST",
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "xi-api-key": self._key(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = _json.loads(resp.read().decode())
        except Exception as e:
            raise ProviderError("tts_request_failed", f"elevenlabs: {e}")
        audio_b64 = body.get("audio_base64", "")
        audio = base64.b64decode(audio_b64) if audio_b64 else b""
        if len(audio) < 1000:
            raise ProviderError("tts_empty_audio", f"elevenlabs returned {len(audio)} bytes")
        alignment = body.get("alignment") or {}
        word_times = _word_times_from_char_alignment(alignment)
        if not word_times:
            word_times = estimate_word_times(text, pcm_duration_ms(audio))
        return audio, word_times


class SmallestAITTS(TTSProvider):
    """Cloud tyre: Smallest AI Lightning TTS (https://api.smallest.ai/waves/v1/tts).
    Ultra-low latency Indian English & Hindi natural voice synthesis.
    Model: lightning_v3.1_pro, Voice: meher (or emily, radha, etc.).
    Uses stdlib (urllib). No documented word-timing field, so word_times
    are always estimated from WAV duration.
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

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

    def synth(self, text: str, voice: str = "meher", speed: float = 1.0) -> tuple[bytes, list]:
        import json as _json

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
                "User-Agent": DEFAULT_USER_AGENT,
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
                return body, estimate_word_times(text, pcm_duration_ms(body))
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("tts_request_failed", f"smallest.ai: {e}")


class DeepgramTTS(TTSProvider):
    """Cloud tyre: Deepgram speak API (Aura family). Same interface.

    Select with TTS_PROVIDER=deepgram. Key from DEEPGRAM_API_KEY env.
    Default voice aura-2-thalia-en (Aura-2 family). Uses only stdlib.
    Verified against Deepgram's docs: the speak endpoint returns raw audio
    bytes only (plus response headers) — no word timing — so word_times
    are always estimated from WAV duration.
    """

    SPEAK_URL = "https://api.deepgram.com/v1/speak"

    def __init__(self, model: str = "aura-2-thalia-en", api_key: str = "") -> None:
        self.model = model
        self.api_key = api_key

    def __repr__(self) -> str:
        return redacted_repr(self, secret_attrs=("api_key",))

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
                "User-Agent": DEFAULT_USER_AGENT,
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
        return audio, estimate_word_times(text, pcm_duration_ms(audio))


def make_tts(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    voice: Optional[str] = None,
) -> TTSProvider:
    """Tyre switch: TTS_PROVIDER=kokoro-local|kokoro|smallest|deepgram|elevenlabs|stub.

    Default is ``kokoro-local`` — the same Kokoro-82M weights in-process
    instead of behind the SpacePilot daemon, 5x faster on measurement
    (2026-09-12: 255ms p50 vs 1235ms vs Deepgram's 1939ms). ``kokoro`` (the
    daemon) stays one env var away for a machine without mlx-audio.

    Fail-closed: unknown provider names raise ProviderError instead of
    silently defaulting to Kokoro. Construction is cheap and does no
    network I/O (kokoro-local's model load happens on a background thread).
    """
    which = (provider or os.environ.get("TTS_PROVIDER", "kokoro-local")).lower()
    logger.debug("make_tts: selecting provider=%s", which)
    if which in ("kokoro-local", "kokoro_local", "kokoro-mlx", "mlx"):
        return KokoroLocalTTS()
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
    raise ProviderError("tts_unknown_provider", f"unknown TTS provider: {which}")
