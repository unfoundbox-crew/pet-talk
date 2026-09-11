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
_audio_store: dict[str, bytes] = {}  # audio_id -> wav bytes (in-process v0.2)

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_PATH = os.path.join(os.path.dirname(SERVER_DIR), "personas", "voices.yaml")
TURNS_PATH = os.path.join(SERVER_DIR, "turns.jsonl")

RUNTIME_SETTINGS = {
    "stt_provider": os.environ.get("STT_PROVIDER", "stub").lower(),
    "llm_provider": os.environ.get("LLM_PROVIDER", "stub").lower(),
    "llm_base_url": os.environ.get("LLM_BASE_URL", "http://100.99.50.84:8000/v1"),
    "llm_model": os.environ.get("LLM_MODEL", "claude-3-7-sonnet"),
    "tts_provider": os.environ.get("TTS_PROVIDER", "stub").lower(),
    "kokoro_base_url": os.environ.get("KOKORO_BASE_URL", "http://127.0.0.1:8088"),
    "vad_silence_ms": int(os.environ.get("VAD_SILENCE_MS", "600")),
}

stt = make_stt(provider=RUNTIME_SETTINGS["stt_provider"])
llm = make_llm(
    provider=RUNTIME_SETTINGS["llm_provider"],
    base_url=RUNTIME_SETTINGS["llm_base_url"],
    model=RUNTIME_SETTINGS["llm_model"],
)
tts = make_tts(
    provider=RUNTIME_SETTINGS["tts_provider"],
    base_url=RUNTIME_SETTINGS["kokoro_base_url"],
)
memory = Hippocampus(ledger_path=os.path.join(SERVER_DIR, "ledger.jsonl"))
persona = load_persona()  # built-in default until personas/ exists


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
def settings_get() -> Response:
    return JSONResponse({
        "ok": True,
        "settings": RUNTIME_SETTINGS,
        "active": {
            "stt": type(stt).__name__,
            "llm": type(llm).__name__,
            "tts": type(tts).__name__,
        },
    })


@app.post("/settings")
async def settings_post(req: dict) -> Response:
    global stt, llm, tts
    if "stt_provider" in req:
        RUNTIME_SETTINGS["stt_provider"] = str(req["stt_provider"]).lower()
    if "llm_provider" in req:
        RUNTIME_SETTINGS["llm_provider"] = str(req["llm_provider"]).lower()
    if "llm_base_url" in req:
        RUNTIME_SETTINGS["llm_base_url"] = str(req["llm_base_url"])
    if "llm_model" in req:
        RUNTIME_SETTINGS["llm_model"] = str(req["llm_model"])
    if "tts_provider" in req:
        RUNTIME_SETTINGS["tts_provider"] = str(req["tts_provider"]).lower()
    if "kokoro_base_url" in req:
        RUNTIME_SETTINGS["kokoro_base_url"] = str(req["kokoro_base_url"])
    if "vad_silence_ms" in req:
        try:
            RUNTIME_SETTINGS["vad_silence_ms"] = int(req["vad_silence_ms"])
        except (ValueError, TypeError):
            pass

    stt = make_stt(
        provider=RUNTIME_SETTINGS["stt_provider"],
        api_key=req.get("deepgram_api_key"),
    )
    llm = make_llm(
        provider=RUNTIME_SETTINGS["llm_provider"],
        base_url=RUNTIME_SETTINGS["llm_base_url"],
        model=RUNTIME_SETTINGS["llm_model"],
        api_key=req.get("llm_api_key"),
    )
    tts = make_tts(
        provider=RUNTIME_SETTINGS["tts_provider"],
        base_url=RUNTIME_SETTINGS["kokoro_base_url"],
    )
    return JSONResponse({
        "ok": True,
        "settings": RUNTIME_SETTINGS,
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


@app.get("/audio/{audio_id}")
def get_audio(audio_id: str) -> Response:
    wav = _audio_store.get(audio_id)
    if wav is None:
        return JSONResponse({"reason": "audio_not_found", "audio_id": audio_id}, status_code=404)
    return Response(content=wav, media_type="audio/wav")


async def _safe_send_json(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):
        return False


async def _speak_sentence(
    ws: WebSocket, turn_id: str, sentence: str, seq: int, active_persona: Optional[Persona] = None
) -> None:
    p = active_persona or persona
    try:
        wav, _word_times = tts.synth(sentence, voice=p.voice, speed=p.speed)
    except ProviderError as e:
        await _safe_send_json(ws, frame("agent.error", turn_id, reason=e.reason, seq=seq))
        return
    audio_id = f"{turn_id}-s{seq}"
    _audio_store[audio_id] = wav
    await _safe_send_json(
        ws, frame("agent.sentence", turn_id, seq=seq, text=sentence, audio_url=f"/audio/{audio_id}")
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
    # Fired from user.stop: TurnLog starts here, ends on done/interrupt.
    log = TurnLog(path=TURNS_PATH)
    log.start(turn_id, _provider_names())
    task = asyncio.ensure_future(handle_turn(ws, turn_id, text, queue, log, active_persona=active_persona))
    turn_tasks[turn_id] = task
    try:
        await task
    except asyncio.CancelledError:
        log.end(path="interrupted", chars=0, sentences=0)
        await ws.send_json(frame("agent.done", turn_id, path="interrupted"))
        raise
    finally:
        turn_tasks.pop(turn_id, None)


async def handle_turn(
    ws: WebSocket, turn_id: str, text: str, queue: SpeakQueue,
    log: Optional[TurnLog] = None, active_persona: Optional[Persona] = None,
) -> None:
    """Router stub: stall+worker path vs direct answer path."""
    if not text or not text.strip():
        await ws.send_json(frame("agent.error", turn_id, reason="empty_transcript"))
        await ws.send_json(frame("agent.done", turn_id))
        if log is not None:
            log.end(path="empty", chars=0, sentences=0)
        return
    p = active_persona or persona
    path = llm.route(text) if hasattr(llm, "route") else route_text(text)
    chars = 0
    first = True
    spoken_sentences: list[str] = []

    pname = getattr(p, "name", "donna")
    history = memory.get_history_messages(pname, limit=6)
    instruction = (
        getattr(p, "instruction_spec", "")
        or f"You are {pname}. {p.tone}\nRespond concisely in 1 to 2 spoken sentences."
    )
    messages = [{"role": "system", "content": instruction}, *history, {"role": "user", "content": text}]

    async def _speak_tracked(sentence: str, seq: int) -> None:
        nonlocal chars, first
        await _speak_sentence(ws, turn_id, sentence, seq=seq, active_persona=p)
        chars += len(sentence)
        spoken_sentences.append(sentence)
        if first and log is not None:
            log.mark("first_sentence")
        first = False

    if path == "stall":
        stall_text = p.stall_for(0)
        await ws.send_json(frame("agent.stall", turn_id, phrase_id="stall-0", text=stall_text))
        if log is not None:
            log.mark("stall")
        await ws.send_json(frame("state.thinking", turn_id))
        await _speak_tracked(stall_text, seq=0)
        # Worker path: stream >=1 sentences behind the playing stall audio.
        await ws.send_json(frame("state.speaking", turn_id))
        seq = 1
        try:
            async for sentence in llm.stream(messages):
                await queue.push(sentence)
                queued = await queue.pop()
                if queued is not None:
                    await _speak_tracked(queued, seq=seq)
                    seq += 1
                    # Test hook only: stretch the turn so barge-kill is observable.
                    # PET_TALK_TURN_DELAY_MS=0 (default) in production.
                    await asyncio.sleep(_turn_delay_s())
        except ProviderError as e:
            await ws.send_json(frame("agent.error", turn_id, reason=e.reason))
        if log is not None:
            log.mark("done")
        await ws.send_json(frame("agent.done", turn_id, path="worker", sentences=seq - 1))
        if log is not None:
            log.end(path="worker", chars=chars, sentences=seq - 1)
        # Record completed turn in durable Hippocampus ledger
        memory.record_turn(turn_id, pname, text, spoken_sentences)
    else:
        await ws.send_json(frame("state.thinking", turn_id))
        await ws.send_json(frame("state.speaking", turn_id))
        seq = 0
        try:
            async for sentence in llm.stream(messages):
                await _speak_tracked(sentence, seq=seq)
                seq += 1
                if seq >= 1:
                    break  # direct path: first answer only, no worker fan-out
        except ProviderError as e:
            await ws.send_json(frame("agent.error", turn_id, reason=e.reason))
        if log is not None:
            log.mark("done")
        await ws.send_json(frame("agent.done", turn_id, path="direct", sentences=seq))
        if log is not None:
            log.end(path="direct", chars=chars, sentences=seq)
        # Record completed turn in durable Hippocampus ledger
        memory.record_turn(turn_id, pname, text, spoken_sentences)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = SpeakQueue()
    turn_tasks: dict = {}
    turn_persona: dict = {}
    turn_id = new_turn_id()
    chunks: list[bytes] = []
    try:
        await ws.send_json(frame("state.idle", turn_id))
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
                await ws.send_json(frame("state.listening", turn_id))
            elif mtype == "user.stop":
                turn_id = msg.get("turn_id") or turn_id
                pcm_b64 = msg.get("pcm_b64", "")
                try:
                    pcm = base64.b64decode(pcm_b64) if pcm_b64 else b""
                except Exception:
                    await ws.send_json(frame("agent.error", turn_id, reason="bad_pcm_encoding"))
                    continue
                chunks.append(pcm)
                try:
                    text = stt.transcribe(b"".join(chunks))
                except ProviderError as e:
                    await ws.send_json(frame("agent.error", turn_id, reason=e.reason))
                    continue
                # Fire-and-forget: the receive loop MUST stay open so a
                # mid-turn barge can land. Wrapper tracks + cleans up.
                active_p = turn_persona.get(turn_id)
                asyncio.ensure_future(
                    handle_turn_task(ws, turn_id, text, queue, turn_tasks, active_persona=active_p)
                )
                chunks = []
            elif mtype == "barge":
                # Kill playback: cancel the live turn FIRST, then flush + re-route.
                ref = msg.get("turn_id") or turn_id
                live = turn_tasks.pop(ref, None) or turn_tasks.pop(turn_id, None)
                if live is not None and not live.done():
                    live.cancel()
                dropped = await queue.flush()
                turn_id = new_turn_id()
                await ws.send_json(
                    frame("state.listening", turn_id, barged_turn=ref, dropped=dropped)
                )
            else:
                await ws.send_json(
                    frame(
                        "agent.error",
                        msg.get("turn_id") or turn_id,
                        reason="unknown_frame",
                        detail=mtype,
                    )
                )
    except WebSocketDisconnect:
        await queue.flush()
