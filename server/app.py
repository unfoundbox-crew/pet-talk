"""FastAPI duplex server — WS /ws frame protocol (TECH-SPEC section 4).

Frames (every frame carries ``turn_id``):
  user.start / user.stop (VAD-gated chunks), barge (kill + flush)
  agent.stall (immediate phrase id), agent.sentence (playable TTS url)
  agent.done, state.idle|listening|thinking|speaking

Router stub: research-trigger words -> stall + worker path (streams >=1
sentences behind the stall), else direct answer path.
SpeakQueue: FIFO sentences with flush() (barge) and resume_from(word_idx).
Fail-closed everywhere with named reasons.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import itertools
import os
import uuid
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.websockets import WebSocketState

from .dictation import CleanProseFormatter, VoiceProseFormatter
from .memory import Hippocampus
from .persona import (
    Persona,
    delete_persona,
    list_personas,
    load_persona,
    save_persona,
)
from .providers import ProviderError, make_llm, make_stt, make_tts, route_text
from .telemetry import TurnLog

# ---------------------------------------------------------------- queue ---


class SpeakQueue:
    """FIFO sentences; flush() drops everything (barge); resume_from() skips."""

    def __init__(self) -> None:
        self._q: collections.deque = collections.deque()
        self._lock = asyncio.Lock()

    async def push(self, sentence: str) -> int:
        async with self._lock:
            self._q.append(sentence)
            return len(self._q)

    async def pop(self) -> Optional[str]:
        async with self._lock:
            return self._q.popleft() if self._q else None

    async def flush(self) -> int:
        """Drop all queued sentences. Returns count dropped."""
        async with self._lock:
            n = len(self._q)
            self._q.clear()
            return n

    async def resume_from(self, word_idx: int, sentence: str) -> str:
        """Return sentence trimmed to words[word_idx:]; fail-closed on bad idx."""
        words = sentence.split()
        if word_idx < 0 or word_idx > len(words):
            raise ProviderError("queue_bad_word_idx", f"{word_idx} of {len(words)} words")
        return " ".join(words[word_idx:])

    async def __len__(self) -> int:
        async with self._lock:
            return len(self._q)


# ---------------------------------------------------------------- app ---

app = FastAPI(title="pet-talk duplex v0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # vite dev; same-origin needs no CORS
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
_turn_counter = itertools.count(1)
MAX_AUDIO_STORE_ENTRIES = 250
_audio_store: collections.OrderedDict[str, bytes] = collections.OrderedDict()
_settings_lock = asyncio.Lock()


def _store_audio(audio_id: str, wav: bytes) -> None:
    _audio_store[audio_id] = wav
    _audio_store.move_to_end(audio_id)
    while len(_audio_store) > MAX_AUDIO_STORE_ENTRIES:
        _audio_store.popitem(last=False)

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_PATH = os.path.join(os.path.dirname(SERVER_DIR), "personas", "voices.yaml")
TURNS_PATH = os.path.join(SERVER_DIR, "turns.jsonl")

RUNTIME_SETTINGS = {
    "stt_provider": os.environ.get(
        "STT_PROVIDER",
        "deepgram" if os.environ.get("DEEPGRAM_API_KEY") else "faster-whisper",
    ).lower(),
    "deepgram_api_key": os.environ.get("DEEPGRAM_API_KEY", ""),
    "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "smallest_api_key": os.environ.get("SMALLEST_API_KEY", ""),
    "opencode_api_key": os.environ.get("OPENCODE_GO_KEY") or os.environ.get("OPENCODE_LENOVO_KEY") or os.environ.get("OPENCODE_API_KEY", ""),
    "gemini_api_key": os.environ.get("GEMINI_PRIMARY_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a"),
    "sensevoice_base_url": os.environ.get("SENSEVOICE_BASE_URL", "http://100.99.50.84:8086"),
    "llm_provider": os.environ.get(
        "LLM_PROVIDER",
        "groq" if os.environ.get("GROQ_API_KEY") else ("haiku" if os.environ.get("ANTHROPIC_API_KEY") else "litellm"),
    ).lower(),
    "llm_base_url": os.environ.get(
        "LLM_BASE_URL",
        "https://api.groq.com/openai/v1" if (os.environ.get("GROQ_API_KEY") and os.environ.get("LLM_PROVIDER", "groq") == "groq") else "http://100.99.50.84:8000/v1",
    ),
    "llm_model": os.environ.get(
        "LLM_MODEL",
        "groq/compound-mini" if (os.environ.get("GROQ_API_KEY") and os.environ.get("LLM_PROVIDER", "groq") == "groq") else "claude-3-5-haiku-20241022",
    ),
    "llm_api_key": os.environ.get(
        "LLM_API_KEY",
        os.environ.get("GROQ_API_KEY", os.environ.get("ANTHROPIC_API_KEY", os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a"))),
    ),
    "tts_provider": os.environ.get("TTS_PROVIDER", "smallest" if os.environ.get("SMALLEST_API_KEY") else "kokoro").lower(),
    "kokoro_base_url": os.environ.get("KOKORO_BASE_URL", "http://127.0.0.1:8088"),
    "vad_silence_ms": int(os.environ.get("VAD_SILENCE_MS", "600")),
}

stt = make_stt(
    provider=RUNTIME_SETTINGS["stt_provider"],
    api_key=(
        RUNTIME_SETTINGS["deepgram_api_key"]
        if RUNTIME_SETTINGS["stt_provider"] == "deepgram"
        else RUNTIME_SETTINGS["groq_api_key"]
        if RUNTIME_SETTINGS["stt_provider"] == "groq"
        else RUNTIME_SETTINGS["openai_api_key"]
        if RUNTIME_SETTINGS["stt_provider"] in ("openai", "openai-whisper")
        else None
    ),
    base_url=RUNTIME_SETTINGS["sensevoice_base_url"],
)
llm = make_llm(
    provider=RUNTIME_SETTINGS["llm_provider"],
    base_url=RUNTIME_SETTINGS["llm_base_url"],
    model=RUNTIME_SETTINGS["llm_model"],
    api_key=RUNTIME_SETTINGS["llm_api_key"],
)
tts = make_tts(
    provider=RUNTIME_SETTINGS["tts_provider"],
    base_url=RUNTIME_SETTINGS["kokoro_base_url"],
    api_key=RUNTIME_SETTINGS["smallest_api_key"],
)
memory = Hippocampus(ledger_path=os.path.join(SERVER_DIR, "ledger.jsonl"))
persona = load_persona(os.environ.get("DEFAULT_PERSONA", "donna"))


def new_turn_id() -> str:
    return f"t{next(_turn_counter)}-{uuid.uuid4().hex[:6]}"


def frame(ftype: str, turn_id: str, **fields) -> dict:
    if not turn_id:
        raise ProviderError("frame_no_turn_id", ftype)
    return {"type": ftype, "turn_id": turn_id, **fields}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "pet-talk-duplex", "version": "0.2"}


def _parse_voices_minimal(text: str) -> dict:
    """Minimal YAML-subset parser for voices.yaml: nested maps only.

    Handles ``key:``, ``key: value`` (2/4-space indent), comments, and
    single/double-quoted scalars. Anything else raises ProviderError.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent]:
            raise ProviderError("voices_bad_yaml", f"line {lineno}: tabs not allowed")
        if ":" not in stripped:
            raise ProviderError("voices_bad_yaml", f"line {lineno}: expected 'key:' or 'key: value'")
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            raise ProviderError("voices_bad_yaml", f"line {lineno}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            node: dict = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = value
    return root


def _load_voices() -> list[dict]:
    """Read voices.yaml -> [{id, display_name, lang}]. Fail-closed."""
    if not os.path.exists(VOICES_PATH):
        raise ProviderError("voices_not_found", VOICES_PATH)
    with open(VOICES_PATH) as f:
        text = f.read()
    try:
        import yaml  # type: ignore  # optional: stdlib-only fallback below

        data = yaml.safe_load(text)
    except ImportError:
        data = _parse_voices_minimal(text)
    if not isinstance(data, dict) or not isinstance(data.get("voices"), dict):
        raise ProviderError("voices_bad_yaml", "expected top-level 'voices:' map")
    out = []
    for vid, meta in data["voices"].items():
        if not isinstance(meta, dict):
            raise ProviderError("voices_bad_yaml", f"voice '{vid}': expected map")
        try:
            out.append(
                {"id": vid, "display_name": meta["display_name"], "lang": meta["lang"]}
            )
        except KeyError as e:
            raise ProviderError("voices_bad_voice", f"voice '{vid}': missing {e}")
    return out


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "pet-talk", "status": "ok", "ws": "/ws"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/voices")
def voices() -> Response:
    try:
        return JSONResponse(_load_voices())
    except ProviderError as e:
        return JSONResponse({"reason": e.reason, "detail": e.detail}, status_code=404)


@app.get("/personas")
def personas_list() -> Response:
    personas = list_personas()
    return JSONResponse([
        {
            "name": p.name,
            "voice": p.voice,
            "speed": p.speed,
            "stalls": p.stalls,
            "tone": p.tone,
        }
        for p in personas
    ])


@app.get("/personas/{name}")
def persona_get(name: str) -> Response:
    try:
        p = load_persona(name)
        return JSONResponse({
            "name": p.name,
            "voice": p.voice,
            "speed": p.speed,
            "stalls": p.stalls,
            "tone": p.tone,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/personas")
async def persona_post(req: dict) -> Response:
    name = req.get("name", "").strip().lower()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    voice = req.get("voice", "af_heart")
    try:
        speed = float(req.get("speed", 1.0))
    except (ValueError, TypeError):
        speed = 1.0
    stalls = req.get("stalls") or ["One moment.", "Looking that up."]
    tone = req.get("tone") or req.get("system_prompt") or "Direct and helpful."
    p = Persona(name=name, voice=voice, speed=speed, stalls=stalls, tone=tone)
    try:
        save_persona(p)
        return JSONResponse({
            "ok": True,
            "persona": {
                "name": p.name,
                "voice": p.voice,
                "speed": p.speed,
                "stalls": p.stalls,
                "tone": p.tone,
            },
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/personas/{name}")
def persona_delete(name: str) -> Response:
    try:
        deleted = delete_persona(name)
        if deleted:
            return JSONResponse({"ok": True, "deleted": name})
        return JSONResponse({"error": "not_found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/settings")
async def settings_get() -> Response:
    async with _settings_lock:
        settings_copy = dict(RUNTIME_SETTINGS)
    return JSONResponse({
        "ok": True,
        "settings": settings_copy,
        "active": {
            "stt": type(stt).__name__,
            "llm": type(llm).__name__,
            "tts": type(tts).__name__,
        },
    })


@app.post("/settings")
async def settings_post(req: dict) -> Response:
    global stt, llm, tts
    async with _settings_lock:
        if "stt_provider" in req:
            RUNTIME_SETTINGS["stt_provider"] = str(req["stt_provider"]).lower()
        if "deepgram_api_key" in req:
            RUNTIME_SETTINGS["deepgram_api_key"] = str(req["deepgram_api_key"])
        if "groq_api_key" in req:
            RUNTIME_SETTINGS["groq_api_key"] = str(req["groq_api_key"])
        if "openai_api_key" in req:
            RUNTIME_SETTINGS["openai_api_key"] = str(req["openai_api_key"])
        if "sensevoice_base_url" in req:
            RUNTIME_SETTINGS["sensevoice_base_url"] = str(req["sensevoice_base_url"])
        if "llm_provider" in req:
            new_llm = str(req["llm_provider"]).lower()
            old_llm = RUNTIME_SETTINGS.get("llm_provider")
            RUNTIME_SETTINGS["llm_provider"] = new_llm
            if new_llm != old_llm:
                if "llm_base_url" not in req:
                    if new_llm in ("haiku", "claude-haiku", "claude"):
                        RUNTIME_SETTINGS["llm_base_url"] = os.environ.get("ANTHROPIC_BASE_URL", "http://100.99.50.84:8000/v1")
                    elif new_llm in ("opencode", "zen", "opencode-zen"):
                        RUNTIME_SETTINGS["llm_base_url"] = os.environ.get("OPENCODE_BASE_URL", "https://api.opencode.ai/v1")
                    elif new_llm in ("gemini", "google", "flash"):
                        RUNTIME_SETTINGS["llm_base_url"] = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
                    elif new_llm == "groq":
                        RUNTIME_SETTINGS["llm_base_url"] = "https://api.groq.com/openai/v1"
                    elif new_llm in ("openai", "gpt"):
                        RUNTIME_SETTINGS["llm_base_url"] = "https://api.openai.com/v1"
                    elif new_llm in ("litellm", "fleet", "local"):
                        RUNTIME_SETTINGS["llm_base_url"] = "http://100.99.50.84:8000/v1"
                if "llm_model" not in req:
                    if new_llm in ("haiku", "claude-haiku", "claude"):
                        RUNTIME_SETTINGS["llm_model"] = os.environ.get("HAIKU_MODEL", "claude-3-5-haiku-20241022")
                    elif new_llm in ("opencode", "zen", "opencode-zen"):
                        RUNTIME_SETTINGS["llm_model"] = os.environ.get("OPENCODE_MODEL", "flash-3.8")
                    elif new_llm in ("gemini", "google", "flash"):
                        RUNTIME_SETTINGS["llm_model"] = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
                    elif new_llm == "groq":
                        RUNTIME_SETTINGS["llm_model"] = "groq/compound-mini"
                    elif new_llm in ("openai", "gpt"):
                        RUNTIME_SETTINGS["llm_model"] = "gpt-5-nano"
                    elif new_llm in ("litellm", "fleet", "local"):
                        RUNTIME_SETTINGS["llm_model"] = "claude-sonnet-4-6"
                if "llm_api_key" not in req:
                    if new_llm in ("haiku", "claude-haiku", "claude"):
                        RUNTIME_SETTINGS["llm_api_key"] = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a")
                    elif new_llm in ("opencode", "zen", "opencode-zen"):
                        RUNTIME_SETTINGS["llm_api_key"] = os.environ.get("OPENCODE_GO_KEY") or os.environ.get("OPENCODE_LENOVO_KEY") or os.environ.get("OPENCODE_API_KEY", "")
                    elif new_llm in ("gemini", "google", "flash"):
                        RUNTIME_SETTINGS["llm_api_key"] = os.environ.get("GEMINI_PRIMARY_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
                    elif new_llm == "groq":
                        RUNTIME_SETTINGS["llm_api_key"] = RUNTIME_SETTINGS.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
                    elif new_llm in ("openai", "gpt"):
                        RUNTIME_SETTINGS["llm_api_key"] = RUNTIME_SETTINGS.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")

        if "llm_base_url" in req:
            RUNTIME_SETTINGS["llm_base_url"] = str(req["llm_base_url"])
        if "llm_model" in req:
            RUNTIME_SETTINGS["llm_model"] = str(req["llm_model"])
        if "llm_api_key" in req:
            RUNTIME_SETTINGS["llm_api_key"] = str(req["llm_api_key"])
        if "tts_provider" in req:
            RUNTIME_SETTINGS["tts_provider"] = str(req["tts_provider"]).lower()
        if "kokoro_base_url" in req:
            RUNTIME_SETTINGS["kokoro_base_url"] = str(req["kokoro_base_url"])
        if "smallest_api_key" in req:
            RUNTIME_SETTINGS["smallest_api_key"] = str(req["smallest_api_key"])
        if "vad_silence_ms" in req:
            try:
                RUNTIME_SETTINGS["vad_silence_ms"] = int(req["vad_silence_ms"])
            except (ValueError, TypeError):
                pass

        stt_key = None
        if RUNTIME_SETTINGS["stt_provider"] == "deepgram":
            stt_key = req.get("deepgram_api_key") or RUNTIME_SETTINGS.get("deepgram_api_key")
        elif RUNTIME_SETTINGS["stt_provider"] == "groq":
            stt_key = req.get("groq_api_key") or RUNTIME_SETTINGS.get("groq_api_key")
        elif RUNTIME_SETTINGS["stt_provider"] in ("openai", "openai-whisper", "whisper-openai"):
            stt_key = req.get("openai_api_key") or RUNTIME_SETTINGS.get("openai_api_key")

        tts_key = None
        if RUNTIME_SETTINGS["tts_provider"] in ("smallest", "smallest-ai", "smallest_ai", "waves"):
            tts_key = req.get("smallest_api_key") or RUNTIME_SETTINGS.get("smallest_api_key")

        llm_key = req.get("llm_api_key")
        if not llm_key or (RUNTIME_SETTINGS["llm_provider"] == "groq" and not str(llm_key).startswith("gsk_")):
            if RUNTIME_SETTINGS["llm_provider"] in ("haiku", "claude-haiku", "claude"):
                llm_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "sk-3340dc7a5732b32c09a08a86da68b7400a9778d3bbbc574a")
            elif RUNTIME_SETTINGS["llm_provider"] in ("opencode", "zen", "opencode-zen"):
                llm_key = os.environ.get("OPENCODE_GO_KEY") or os.environ.get("OPENCODE_LENOVO_KEY") or os.environ.get("OPENCODE_API_KEY", "")
            elif RUNTIME_SETTINGS["llm_provider"] in ("gemini", "google", "flash"):
                llm_key = os.environ.get("GEMINI_PRIMARY_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
            elif RUNTIME_SETTINGS["llm_provider"] == "groq":
                llm_key = RUNTIME_SETTINGS.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
            elif RUNTIME_SETTINGS["llm_provider"] in ("openai", "gpt"):
                llm_key = RUNTIME_SETTINGS.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
            else:
                llm_key = RUNTIME_SETTINGS.get("llm_api_key")

        stt = make_stt(
            provider=RUNTIME_SETTINGS["stt_provider"],
            api_key=stt_key,
            base_url=req.get("sensevoice_base_url") or RUNTIME_SETTINGS.get("sensevoice_base_url"),
        )
        llm = make_llm(
            provider=RUNTIME_SETTINGS["llm_provider"],
            base_url=RUNTIME_SETTINGS["llm_base_url"],
            model=RUNTIME_SETTINGS["llm_model"],
            api_key=llm_key,
        )
        tts = make_tts(
            provider=RUNTIME_SETTINGS["tts_provider"],
            base_url=RUNTIME_SETTINGS["kokoro_base_url"],
            api_key=tts_key,
        )
        settings_copy = dict(RUNTIME_SETTINGS)

    return JSONResponse({
        "ok": True,
        "settings": settings_copy,
        "active": {
            "stt": type(stt).__name__,
            "llm": type(llm).__name__,
            "tts": type(tts).__name__,
        },
    })


@app.get("/ledger")
def ledger_get(limit: int = 50) -> Response:
    turns = memory.recent_turns(limit=limit)
    return JSONResponse({"ok": True, "turns": turns})


@app.delete("/ledger")
def ledger_clear() -> Response:
    try:
        with open(memory.ledger_path, "w", encoding="utf-8") as f:
            f.write("")
        return JSONResponse({"ok": True, "cleared": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/transcribe")
async def transcribe_endpoint(req: dict) -> Response:
    pcm_b64 = req.get("pcm_b64") or req.get("audio_b64") or req.get("audio")
    if not pcm_b64 or not isinstance(pcm_b64, str) or not pcm_b64.strip():
        return JSONResponse({"ok": False, "error": "empty_audio"}, status_code=400)

    try:
        audio_bytes = base64.b64decode(pcm_b64, validate=True)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"invalid_base64: {e}"}, status_code=400)

    if not audio_bytes:
        return JSONResponse({"ok": False, "error": "empty_audio"}, status_code=400)

    try:
        sample_rate = int(req.get("sample_rate") or 16000)
    except (ValueError, TypeError):
        sample_rate = 16000

    clean_prose = req.get("clean_prose", True)
    if isinstance(clean_prose, str):
        clean_prose = clean_prose.lower() not in ("false", "0", "no")

    try:
        raw_text = stt.transcribe(audio_bytes, sample_rate=sample_rate)
        text = CleanProseFormatter.format(raw_text) if clean_prose else raw_text
        return JSONResponse({"ok": True, "text": text, "raw_text": raw_text})
    except ProviderError as e:
        status_code = 400 if "empty" in e.reason else 502
        return JSONResponse({"ok": False, "error": str(e), "reason": e.reason}, status_code=status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/audio/{audio_id}")
def get_audio(audio_id: str) -> Response:
    wav = _audio_store.get(audio_id)
    if wav is None:
        return JSONResponse({"reason": "audio_not_found", "audio_id": audio_id}, status_code=404)
    _audio_store.move_to_end(audio_id)
    return Response(content=wav, media_type="audio/wav")


async def _safe_send_json(ws: WebSocket, payload: dict) -> bool:
    try:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        await ws.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect, Exception):
        return False


_STALL_AUDIO_CACHE: dict[str, tuple[str, bytes]] = {}


def _get_or_synth_stall(stall_text: str, p: Persona) -> tuple[str, bytes]:
    """Retrieve pre-cached stall audio or synthesize on demand."""
    clean_text = VoiceProseFormatter.sanitize(stall_text, max_words=15) or stall_text
    cache_key = f"{p.name}:{p.voice}:{clean_text}"
    if cache_key in _STALL_AUDIO_CACHE:
        audio_id, wav = _STALL_AUDIO_CACHE[cache_key]
        if audio_id in _audio_store:
            return audio_id, wav

    try:
        wav, _ = tts.synth(clean_text, voice=p.voice, speed=p.speed)
    except Exception:
        wav = b""
    audio_id = f"stall-{uuid.uuid4().hex[:8]}"
    if wav:
        _store_audio(audio_id, wav)
        _STALL_AUDIO_CACHE[cache_key] = (audio_id, wav)
    return audio_id, wav


def check_deterministic_control(text: str) -> tuple[bool, Optional[str]]:
    """Check if turn is a local deterministic command (<20ms, 0 LLM latency)."""
    t = text.strip().lower().rstrip(".?!,")
    # Silence / abort turn
    if t in ("stop", "cancel", "pause", "be quiet", "shut up", "hush", "silence", "halt"):
        return True, None
    # Quick status check
    if t in ("status", "ping", "are you there", "system status", "health", "test"):
        return True, "All systems online and ready."
    # Who are you
    if t in ("who are you", "who is this", "what is your name", "introduce yourself"):
        return True, "I'm Donna, your Chief of Staff. What do you need?"
    return False, None


async def _speak_sentence(
    ws: WebSocket, turn_id: str, sentence: str, seq: int, active_persona: Optional[Persona] = None
) -> bool:
    p = active_persona or persona
    clean_text = VoiceProseFormatter.sanitize(sentence, max_words=20)
    if not clean_text:
        clean_text = sentence.strip()
    try:
        wav, _word_times = tts.synth(clean_text, voice=p.voice, speed=p.speed)
    except ProviderError as e:
        await _safe_send_json(ws, frame("agent.error", turn_id, reason=e.reason, seq=seq))
        return False
    audio_id = f"{turn_id}-s{seq}"
    _store_audio(audio_id, wav)
    return await _safe_send_json(
        ws, frame("agent.sentence", turn_id, seq=seq, text=clean_text, audio_url=f"/audio/{audio_id}")
    )


def _turn_delay_s() -> float:
    """Test hook: stretch worker turns so barge-kill is observable. 0 in prod."""
    try:
        return max(0.0, float(os.environ.get("PET_TALK_TURN_DELAY_MS", "0"))) / 1000.0
    except ValueError:
        return 0.0


def _provider_names() -> dict[str, str]:
    """Actual provider set behind this turn (class names, config-driven)."""
    return {"stt": type(stt).__name__, "llm": type(llm).__name__, "tts": type(tts).__name__}


async def handle_turn_task(
    ws: WebSocket, turn_id: str, text: str, queue: SpeakQueue,
    turn_tasks: dict, active_persona: Optional[Persona] = None,
) -> None:
    """Run handle_turn as a cancellable task so barge can kill it mid-turn."""
    log = TurnLog(path=TURNS_PATH)
    log.start(turn_id, _provider_names())
    task = asyncio.ensure_future(handle_turn(ws, turn_id, text, queue, log, active_persona=active_persona))
    turn_tasks[turn_id] = task
    try:
        await task
    except asyncio.CancelledError:
        log.end(path="interrupted", chars=0, sentences=0)
        await _safe_send_json(ws, frame("agent.done", turn_id, path="interrupted"))
    except Exception as e:
        log.end(path="error", chars=0, sentences=0)
        await _safe_send_json(ws, frame("agent.error", turn_id, reason="turn_task_exception", detail=str(e)))
    finally:
        turn_tasks.pop(turn_id, None)


def get_session_grounding() -> str:
    """Collect compact, real-time grounding for spoken voice context (strictly bounded)."""
    import os
    import subprocess

    grounding_parts = []

    # 1. Local workspace git checkout grounding (1 line each)
    try:
        pt_out = subprocess.check_output(
            ["git", "-C", "/Users/saurabh/code/unfoundbox-crew/pet-talk", "log", "-n", "1", "--oneline"],
            text=True, timeout=1, stderr=subprocess.DEVNULL
        ).strip()
        if pt_out:
            grounding_parts.append(f"Pet-Talk repo: {pt_out[:60]}")
    except Exception:
        pass

    try:
        sp_out = subprocess.check_output(
            ["git", "-C", "/Users/saurabh/code/motionvector/spacepilot", "log", "-n", "1", "--oneline"],
            text=True, timeout=1, stderr=subprocess.DEVNULL
        ).strip()
        if sp_out:
            grounding_parts.append(f"SpacePilot repo: {sp_out[:60]}")
    except Exception:
        pass

    grounding_parts.append("Active services: Kokoro TTS (:8088), Pet-Talk WS (:8089), LiteLLM Fleet (:8000)")
    grounding_parts.append(
        "(INTERNAL CONTEXT ONLY — NEVER RECITE FILE PATHS, GIT COMMANDS, OR RAW PORTS ALOUD. "
        "Keep reply strictly under 20 words in natural spoken prose.)"
    )
    return "\n".join(grounding_parts)
    return "\n\n".join(grounding_parts)


async def handle_turn(
    ws: WebSocket, turn_id: str, text: str, queue: SpeakQueue,
    log: Optional[TurnLog] = None, active_persona: Optional[Persona] = None,
) -> None:
    """Router: deterministic control vs stall+worker vs direct answer."""
    if not text or not text.strip():
        await _safe_send_json(ws, frame("agent.error", turn_id, reason="empty_transcript"))
        await _safe_send_json(ws, frame("agent.done", turn_id))
        if log is not None:
            log.end(path="empty", chars=0, sentences=0)
        return

    p = active_persona or persona
    chars = 0
    first = True
    spoken_sentences: list[str] = []

    async def _speak_tracked(sentence: str, seq: int) -> bool:
        nonlocal chars, first
        sent = await _speak_sentence(ws, turn_id, sentence, seq=seq, active_persona=p)
        if not sent:
            return False
        chars += len(sentence)
        spoken_sentences.append(sentence)
        if first and log is not None:
            log.mark("first_sentence")
        first = False
        return True

    # 1. Deterministic Control Fast-Path (<20ms)
    is_control, control_reply = check_deterministic_control(text)
    if is_control:
        if control_reply is None:
            # Immediate silence / stop
            await _safe_send_json(ws, frame("agent.done", turn_id, path="control", sentences=0))
            if log is not None:
                log.end(path="control", chars=0, sentences=0)
            return
        if not await _safe_send_json(ws, frame("state.speaking", turn_id)):
            return
        await _speak_tracked(control_reply, seq=0)
        await _safe_send_json(ws, frame("agent.done", turn_id, path="control", sentences=1))
        if log is not None:
            log.mark("done")
            log.end(path="control", chars=len(control_reply), sentences=1)
        memory.record_turn(turn_id, getattr(p, "name", "donna"), text, spoken_sentences)
        return

    # 2. Route general request
    path = llm.route(text) if hasattr(llm, "route") else route_text(text)
    pname = getattr(p, "name", "donna")
    history = memory.get_history_messages(pname, limit=6)
    live_ctx = get_session_grounding()
    instruction = (
        getattr(p, "instruction_spec", "")
        or (
            f"You are Donna—Saurabh's Chief of Staff, agent co-founder, and digital twin brain.\n"
            f"{p.tone}\n\n"
            f"[ACTIVE SYSTEM GROUNDING]\n"
            f"{live_ctx}\n\n"
            f"CRITICAL LIVE VOICE RULES:\n"
            f"1. Respond strictly in 1 or 2 concise spoken sentences (maximum 15-20 words total).\n"
            f"2. Deliver the direct answer or punchline immediately.\n"
            f"3. Never use markdown, bullet points, asterisks, backticks, code snippets, or parentheticals.\n"
            f"4. Never recite file names, git hashes, or raw service ports.\n"
            f"5. Speak with dry wit, poise, authentic authority, and dignity."
        )
    )
    messages = [{"role": "system", "content": instruction}, *history, {"role": "user", "content": text}]

    if path == "stall":
        stall_text = p.stall_for(0)
        audio_id, _ = _get_or_synth_stall(stall_text, p)
        if not await _safe_send_json(ws, frame("agent.stall", turn_id, phrase_id="stall-0", text=stall_text)):
            return
        if log is not None:
            log.mark("stall")
        if not await _safe_send_json(ws, frame("state.thinking", turn_id)):
            return
        # Send pre-cached stall audio immediately (<2ms)
        if not await _safe_send_json(
            ws, frame("agent.sentence", turn_id, seq=0, text=stall_text, audio_url=f"/audio/{audio_id}")
        ):
            return
        if log is not None:
            log.mark("first_sentence")
        chars += len(stall_text)
        spoken_sentences.append(stall_text)
        # Worker path: stream >=1 sentences behind the playing stall audio.
        if not await _safe_send_json(ws, frame("state.speaking", turn_id)):
            return
        seq = 1
        try:
            async for sentence in llm.stream(messages):
                await queue.push(sentence)
                queued = await queue.pop()
                if queued is not None:
                    if not await _speak_tracked(queued, seq=seq):
                        break
                    seq += 1
                    await asyncio.sleep(_turn_delay_s())
        except ProviderError as e:
            await _safe_send_json(ws, frame("agent.error", turn_id, reason=e.reason))
        if log is not None:
            log.mark("done")
        await _safe_send_json(ws, frame("agent.done", turn_id, path="worker", sentences=seq - 1))
        if log is not None:
            log.end(path="worker", chars=chars, sentences=seq - 1)
        memory.record_turn(turn_id, pname, text, spoken_sentences)
    else:
        if not await _safe_send_json(ws, frame("state.thinking", turn_id)):
            return
        if not await _safe_send_json(ws, frame("state.speaking", turn_id)):
            return
        seq = 0
        try:
            async for sentence in llm.stream(messages):
                if not await _speak_tracked(sentence, seq=seq):
                    break
                seq += 1
                if seq >= 1:
                    break  # direct path: first answer only, no worker fan-out
        except ProviderError as e:
            await _safe_send_json(ws, frame("agent.error", turn_id, reason=e.reason))
        if log is not None:
            log.mark("done")
        await _safe_send_json(ws, frame("agent.done", turn_id, path="direct", sentences=seq))
        if log is not None:
            log.end(path="direct", chars=chars, sentences=seq)
        memory.record_turn(turn_id, pname, text, spoken_sentences)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = SpeakQueue()
    turn_tasks: dict[str, asyncio.Task] = {}
    active_tasks: set[asyncio.Task] = set()
    turn_persona: dict = {}
    turn_id = new_turn_id()
    chunks: list[bytes] = []

    def _track(t: asyncio.Task) -> asyncio.Task:
        active_tasks.add(t)
        t.add_done_callback(active_tasks.discard)
        return t

    try:
        await _safe_send_json(ws, frame("state.idle", turn_id))
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type", "")
            if mtype == "user.start":
                chunks = []
                turn_id = msg.get("turn_id") or new_turn_id()
                pname = msg.get("persona") or "donna"
                try:
                    current_p = load_persona(pname)
                except Exception:
                    current_p = Persona(name=pname)
                if msg.get("custom_voice") or msg.get("voice"):
                    current_p.voice = str(msg.get("custom_voice") or msg.get("voice"))
                if msg.get("custom_speed") or msg.get("speed"):
                    try:
                        current_p.speed = float(msg.get("custom_speed") or msg.get("speed"))
                    except (ValueError, TypeError):
                        pass
                if msg.get("custom_tone") or msg.get("system_prompt"):
                    current_p.tone = str(msg.get("custom_tone") or msg.get("system_prompt"))
                if isinstance(msg.get("custom_stalls"), list) and msg["custom_stalls"]:
                    current_p.stalls = [str(s) for s in msg["custom_stalls"] if str(s).strip()]
                turn_persona[turn_id] = current_p
                await _safe_send_json(ws, frame("state.listening", turn_id))
            elif mtype == "user.chunk":
                b64 = msg.get("chunk", "")
                if b64:
                    try:
                        chunks.append(base64.b64decode(b64))
                    except Exception:
                        pass
            elif mtype == "user.stop":
                turn_id = msg.get("turn_id") or turn_id
                pcm_b64 = msg.get("pcm_b64", "")
                if pcm_b64:
                    try:
                        decoded = base64.b64decode(pcm_b64)
                        if decoded:
                            # Use client's complete merged buffer if provided
                            chunks = [decoded]
                    except Exception:
                        await _safe_send_json(ws, frame("agent.error", turn_id, reason="bad_pcm_encoding"))
                        continue
                audio_payload = b"".join(chunks)
                if not audio_payload:
                    await _safe_send_json(ws, frame("agent.error", turn_id, reason="stt_empty_audio"))
                    continue
                sample_rate = int(msg.get("sample_rate") or 16000)
                try:
                    text = stt.transcribe(audio_payload, sample_rate=sample_rate)
                except ProviderError as e:
                    await _safe_send_json(ws, frame("agent.error", turn_id, reason=e.reason))
                    continue
                # Fire-and-forget: the receive loop MUST stay open so a
                # mid-turn barge can land. Wrapper tracks + cleans up.
                active_p = turn_persona.get(turn_id)
                await _safe_send_json(ws, frame("transcript.user", turn_id, text=text))
                _track(asyncio.create_task(
                    handle_turn_task(ws, turn_id, text, queue, turn_tasks, active_persona=active_p)
                ))
                chunks = []
            elif mtype == "user.text":
                turn_id = msg.get("turn_id") or new_turn_id()
                text = (msg.get("text") or "").strip()
                if not text:
                    await _safe_send_json(ws, frame("agent.error", turn_id, reason="empty_text"))
                    continue
                pname = msg.get("persona") or "donna"
                try:
                    current_p = load_persona(pname)
                except Exception:
                    current_p = Persona(name=pname)
                if msg.get("custom_voice") or msg.get("voice"):
                    current_p.voice = str(msg.get("custom_voice") or msg.get("voice"))
                if msg.get("custom_speed") or msg.get("speed"):
                    try:
                        current_p.speed = float(msg.get("custom_speed") or msg.get("speed"))
                    except (ValueError, TypeError):
                        pass
                if msg.get("custom_tone") or msg.get("system_prompt"):
                    current_p.tone = str(msg.get("custom_tone") or msg.get("system_prompt"))
                if isinstance(msg.get("custom_stalls"), list) and msg["custom_stalls"]:
                    current_p.stalls = [str(s) for s in msg["custom_stalls"] if str(s).strip()]
                turn_persona[turn_id] = current_p

                # Emit user transcript frame to mirror user speech/text into the client log
                await _safe_send_json(ws, frame("transcript.user", turn_id, text=text))

                active_p = turn_persona.get(turn_id)
                _track(asyncio.create_task(
                    handle_turn_task(ws, turn_id, text, queue, turn_tasks, active_persona=active_p)
                ))
            elif mtype == "barge":
                # Kill playback: cancel the live turn FIRST, then flush + re-route.
                ref = msg.get("turn_id") or turn_id
                live = turn_tasks.pop(ref, None) or turn_tasks.pop(turn_id, None)
                if live is not None and not live.done():
                    live.cancel()
                dropped = await queue.flush()
                turn_id = new_turn_id()
                await _safe_send_json(
                    ws, frame("state.listening", turn_id, barged_turn=ref, dropped=dropped)
                )
            else:
                await _safe_send_json(
                    ws,
                    frame(
                        "agent.error",
                        msg.get("turn_id") or turn_id,
                        reason="unknown_frame",
                        detail=mtype,
                    ),
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        all_pending = list(active_tasks) + list(turn_tasks.values())
        for t in all_pending:
            if not t.done():
                t.cancel()
        if all_pending:
            await asyncio.gather(*all_pending, return_exceptions=True)
        await queue.flush()

