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
from .stall import get_or_synth_stall, stall_cache_key
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
    "speak_sentence",
    "stall_cache_key",
    "store_audio",
    "studio_token",
    "stt",
    "tts",
    "ws_endpoint",
]
