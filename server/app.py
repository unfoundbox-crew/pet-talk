"""FastAPI duplex server — uvicorn target ``server.app:app``.

This module assembles the app and is the stable import surface; the work
lives in focused modules:

    settings.py     runtime settings dataclass, provider construction
    runtime.py      live provider set, swap lock, memory, default persona
    frames.py       WS frame construction and safe delivery
    speak_queue.py  producer/consumer sentence queue (flush, resume_from)
    speech.py       LLM producer + TTS consumer, one sentence out
    grounding.py    repo-relative + AX grounding via async subprocess
    turn.py         one turn: route, stall, stream, speak
    ws.py           the /ws reader loop and barge
    routes_http.py  HTTP routes
    voices.py       personas/voices.yaml reader
    warmup.py       startup STT warm (the cold first turn's model load)

Wire protocol is unchanged (TECH-SPEC section 4): every frame carries
``turn_id``; ``barge`` kills playback and flushes; the worker path streams
sentences behind the stall. Fail-closed everywhere with named reasons.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import routes_http, runtime, ws as ws_module
from .auth import StudioAuthError, studio_token
from .audio_store import (
    AUDIO_STORE,
    MAX_AUDIO_STORE_ENTRIES,
    get_audio,
    has_audio,
    store_audio,
)
from .frames import Frame, error_frame, frame, new_turn_id, safe_send_json, send_error
from .grounding import (
    ax_snapshot,
    collect_grounding,
    get_session_grounding,
    hotkey_bin,
    repo_root,
)
from .logs import swallowed
from .memory import Hippocampus
from .persona import Persona, delete_persona, list_personas, load_persona, save_persona
from .providers import ProviderError, make_llm, make_stt, make_tts, route_text
from .provider_factory import ProviderSet, build_providers
from .settings import (
    REPO_ROOT,
    RUNTIME_SETTINGS,
    SERVER_DIR,
    TURNS_PATH,
    VOICES_PATH,
    RuntimeSettings,
)
from .speak_queue import ResumePoint, SpeakQueue, SpokenSentence
from .control import Control, check_deterministic_control
from .persona_runtime import build_system_prompt, resolve_persona
from .stall import get_or_synth_stall, stall_cache_key, warm_stall_cache
from .warmup import silent_pcm16, stt_warm_enabled, warm_stt
from .speech import TurnResult, run_speech, speak_sentence
from .turn import handle_turn, handle_turn_task
from .voices import load_voices, parse_voices_minimal

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("PET_TALK_CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]

app = FastAPI(title="pet-talk duplex v0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # vite dev; same-origin needs no CORS
    # No cookies or Authorization ride these requests — the studio token does,
    # in an explicit header. allow_credentials=True would let a browsing page
    # replay the user's session against a mutating route.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StudioAuthError)
async def _studio_auth_error(_request: Request, exc: StudioAuthError) -> JSONResponse:
    """Fail closed with our own body shape, not FastAPI's ``{"detail": ...}``."""
    return JSONResponse({"ok": False, "reason": exc.reason}, status_code=401)


# Resolve (and, first run, generate + log the path of) the studio token at
# import time, so the path is in the log before the first client connects.
studio_token()
app.include_router(routes_http.router)
app.include_router(ws_module.router)


#: Strong references to the startup warm tasks (see below).
_WARM_TASK = None
_STT_WARM_TASK = None


def warm_tasks() -> list:
    """The startup warm tasks that were actually scheduled.

    A seam for the gate: `qa/test_turn_lifecycle.py` awaits these to prove the
    startup hook warms STT exactly once, without the flakiness of sleeping and
    hoping the loop got a turn.
    """
    return [t for t in (_WARM_TASK, _STT_WARM_TASK) if t is not None]


@app.on_event("startup")
async def _warm_on_startup() -> None:
    """Warm the two things a cold first turn otherwise pays for: the stall
    phrases (TTS) and the STT model.

    Both are scheduled as tasks and neither is awaited: the WS port must accept
    connections immediately, and a slow or unavailable backend must not hold
    the boot. They run concurrently because they are different providers, so
    the slower one does not gate the other.

    Why each exists, measured on this machine:

    * **Stall warm.** The cold first turn was the only thing breaching
      ``turn_worst_ms`` — 1455ms cold against 291ms warm (2026-09-12b).
      ``PET_TALK_STALL_WARM=0`` turns it off and says so in the log.
    * **STT warm.** With TTS and the stall cache both pre-warmed, the first
      turn after a boot still stalled 1264ms while faster-whisper loaded its
      weights on the first call; warm turns that day ran 317-383ms. One short
      transcription of silence at startup buys that load back.
      ``PET_TALK_STT_WARM=0`` turns it off and says so in the log.
    """
    import asyncio as _asyncio

    async def _warm() -> None:
        try:
            await warm_stall_cache(runtime.default_persona(), runtime.current().tts)
        except Exception as e:  # a failed warm must never take the server down
            swallowed("stall_warm_task_failed", e)

    async def _warm_stt() -> None:
        try:
            await warm_stt(runtime.current().stt)
        except Exception as e:  # warm_stt does not raise; belt and braces
            swallowed("stt_warm_task_failed", e)

    # The references are held on purpose. asyncio keeps only a weak reference to
    # a running task, so a bare `create_task(...)` whose result nobody holds can
    # be garbage-collected before it ever runs — measured here: the first boot
    # of this hook logged nothing at all because of exactly that.
    global _WARM_TASK, _STT_WARM_TASK
    _WARM_TASK = _asyncio.create_task(_warm(), name="stall-warm")
    _STT_WARM_TASK = _asyncio.create_task(_warm_stt(), name="stt-warm")

# --------------------------------------------------- compatibility names ---
# The live provider triple is published here because this is the documented
# seam: QA suites monkeypatch ``server.app.stt`` and a turn must observe it
# (see server/runtime.py). ``runtime.install`` keeps these in sync on swap.
stt = runtime.current().stt
llm = runtime.current().llm
tts = runtime.current().tts

memory: Hippocampus = runtime.memory
persona: Persona = runtime.default_persona()

# Older names kept so nothing downstream breaks on the split.
_audio_store = AUDIO_STORE
_store_audio = store_audio
_safe_send_json = safe_send_json
_load_voices = load_voices
_parse_voices_minimal = parse_voices_minimal
_get_or_synth_stall = get_or_synth_stall
_speak_sentence = speak_sentence
ws_endpoint = ws_module.ws_endpoint

__all__ = [
    "AUDIO_STORE",
    "Control",
    "Frame",
    "Hippocampus",
    "MAX_AUDIO_STORE_ENTRIES",
    "Persona",
    "ProviderError",
    "ProviderSet",
    "REPO_ROOT",
    "RUNTIME_SETTINGS",
    "ResumePoint",
    "RuntimeSettings",
    "SERVER_DIR",
    "SpeakQueue",
    "SpokenSentence",
    "TURNS_PATH",
    "TurnResult",
    "VOICES_PATH",
    "app",
    "ax_snapshot",
    "build_providers",
    "build_system_prompt",
    "check_deterministic_control",
    "collect_grounding",
    "delete_persona",
    "error_frame",
    "frame",
    "get_audio",
    "get_or_synth_stall",
    "get_session_grounding",
    "handle_turn",
    "handle_turn_task",
    "has_audio",
    "hotkey_bin",
    "list_personas",
    "llm",
    "load_persona",
    "load_voices",
    "make_llm",
    "make_stt",
    "make_tts",
    "memory",
    "new_turn_id",
    "persona",
    "repo_root",
    "resolve_persona",
    "route_text",
    "runtime",
    "safe_send_json",
    "save_persona",
    "send_error",
    "silent_pcm16",
    "speak_sentence",
    "stall_cache_key",
    "store_audio",
    "studio_token",
    "stt",
    "stt_warm_enabled",
    "tts",
    "warm_stt",
    "warm_tasks",
    "ws_endpoint",
]
