"""HTTP routes: health, voices, personas, settings, ledger, transcribe, audio.

Two rules worth naming here:

* ``GET /settings`` never returns a credential. Any field whose name looks
  like a secret comes back as ``"***"`` with a companion ``secrets_set`` map;
  ``POST`` treats an incoming ``"***"`` as "leave it alone", so a UI that
  round-trips a masked read cannot wipe a live key.
* Every mutating route sits behind ``X-Studio-Token`` (see
  :mod:`server.auth`) and ``POST /settings`` additionally checks every
  ``*_base_url`` against the egress allowlist — an attacker-supplied endpoint
  plus a stored key is credential exfiltration, not configuration.
* ``POST /settings`` is atomic with respect to turns — the rebuild happens
  under the swap lock, and each turn snapshots providers once at its start.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

import os

from .audio_store import get_audio
from .auth import offending_base_url, require_studio_token
from .dictation import CleanProseFormatter
from .frames import parse_int_field
from .logs import log, swallowed
from .providers.tts import provider_voices
from . import runtime
from .persona import Persona, delete_persona, list_personas, load_persona, save_persona
from .providers import ProviderError
from .settings import SERVICE_NAME, SERVICE_VERSION
from . import runtime
from .voices import load_voices

router = APIRouter()


def _persona_payload(p: Persona) -> dict[str, Any]:
    return {
        "name": p.name,
        "voice": p.voice,
        "speed": p.speed,
        "stalls": p.stalls,
        "tone": p.tone,
    }


# --------------------------------------------------------------- health ---


@router.get("/")
def root() -> dict[str, str]:
    return {"service": "pet-talk", "status": "ok", "ws": "/ws"}


@router.get("/health")
def health() -> dict[str, Any]:
    """The one health route. ``degraded`` names every unusable tyre.

    ``ok`` was hardcoded true, so a server with no working TTS answered a
    health check with ``ok: true`` and a populated ``degraded`` — a monitor
    reading the one field it is supposed to read saw green. ``ok`` is now false
    whenever anything is degraded; the reasons stay in ``degraded``.
    """
    providers = runtime.current()
    degraded = list(providers.degraded)
    return {
        "ok": not degraded,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "providers": providers.class_names(),
        "degraded": degraded,
    }


@router.get("/voices")
def voices(provider: str = "") -> Response:
    try:
        if provider or os.environ.get("PET_TALK_VOICES_FROM_PROVIDER") == "1":
            name = provider or runtime.settings().tts_provider
            return JSONResponse({"provider": name, "voices": provider_voices(name)})
        return JSONResponse(load_voices())
    except ProviderError as e:
        return JSONResponse({"reason": e.reason, "detail": e.detail}, status_code=404)


# -------------------------------------------------------------- personas ---


@router.get("/personas")
def personas_list() -> Response:
    return JSONResponse([_persona_payload(p) for p in list_personas()])


@router.get("/personas/{name}")
def persona_get(name: str) -> Response:
    try:
        return JSONResponse(_persona_payload(load_persona(name)))
    except Exception as e:
        return JSONResponse(
            {"reason": "persona_unloadable", "error": str(e)}, status_code=404
        )


@router.post("/personas", dependencies=[Depends(require_studio_token)])
async def persona_post(req: dict) -> Response:
    name = str(req.get("name") or "").strip().lower()
    if not name:
        return JSONResponse(
            {"reason": "persona_no_name", "error": "name is required"}, status_code=400
        )
    try:
        speed = float(req.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    p = Persona(
        name=name,
        voice=str(req.get("voice") or "af_heart"),
        speed=speed,
        stalls=list(req.get("stalls") or ["One moment.", "Looking that up."]),
        tone=str(req.get("tone") or req.get("system_prompt") or "Direct and helpful."),
    )
    try:
        save_persona(p)
    except Exception as e:
        return JSONResponse(
            {"reason": "persona_save_failed", "error": str(e)}, status_code=400
        )
    return JSONResponse({"ok": True, "persona": _persona_payload(p)})


@router.delete("/personas/{name}", dependencies=[Depends(require_studio_token)])
def persona_delete(name: str) -> Response:
    try:
        if delete_persona(name):
            return JSONResponse({"ok": True, "deleted": name})
    except ValueError as e:
        return JSONResponse(
            {"reason": "persona_protected", "error": str(e)}, status_code=400
        )
    return JSONResponse({"reason": "not_found", "error": "not_found"}, status_code=404)


# -------------------------------------------------------------- settings ---


def _settings_payload() -> dict[str, Any]:
    redacted, secrets_set = runtime.settings().redacted()
    providers = runtime.current()
    return {
        "ok": True,
        "settings": redacted,
        "secrets_set": secrets_set,
        "active": providers.class_names(),
        "degraded": list(providers.degraded),
    }


@router.get("/settings")
async def settings_get() -> Response:
    async with runtime.swap_lock:
        return JSONResponse(_settings_payload())


@router.post("/settings", dependencies=[Depends(require_studio_token)])
async def settings_post(req: dict) -> Response:
    if not isinstance(req, dict):
        return JSONResponse(
            {"ok": False, "reason": "bad_request", "error": "body must be a JSON object"},
            status_code=400,
        )
    bad = offending_base_url(req)
    if bad is not None:
        log.warning("settings_rejected reason=base_url_not_allowed field=%s", bad)
        return JSONResponse(
            {"ok": False, "reason": "base_url_not_allowed", "field": bad},
            status_code=400,
        )
    await runtime.swap(req)
    payload = _settings_payload()
    if payload["degraded"]:
        log.warning("settings_applied_degraded degraded=%s", payload["degraded"])
    return JSONResponse(payload)


# ---------------------------------------------------------------- ledger ---


@router.get("/ledger")
def ledger_get(limit: int = 50) -> Response:
    return JSONResponse({"ok": True, "turns": runtime.memory.recent_turns(limit=limit)})


@router.delete("/ledger", dependencies=[Depends(require_studio_token)])
def ledger_clear() -> Response:
    try:
        with open(runtime.memory.ledger_path, "w", encoding="utf-8") as f:
            f.write("")
    except OSError as e:
        return JSONResponse(
            {"reason": swallowed("ledger_clear_failed", e), "error": str(e)},
            status_code=500,
        )
    return JSONResponse({"ok": True, "cleared": True})


# ------------------------------------------------------------ transcribe ---


@router.post("/transcribe", dependencies=[Depends(require_studio_token)])
async def transcribe_endpoint(req: dict) -> Response:
    pcm_b64 = req.get("pcm_b64") or req.get("audio_b64") or req.get("audio")
    if not pcm_b64 or not isinstance(pcm_b64, str) or not pcm_b64.strip():
        return JSONResponse({"ok": False, "error": "empty_audio"}, status_code=400)
    try:
        audio_bytes = base64.b64decode(pcm_b64, validate=True)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"invalid_base64: {e}"}, status_code=400
        )
    if not audio_bytes:
        return JSONResponse({"ok": False, "error": "empty_audio"}, status_code=400)

    try:
        sample_rate = parse_int_field(req, "sample_rate", 16000, minimum=1)
    except ProviderError as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "reason": e.reason}, status_code=400
        )

    clean_prose = req.get("clean_prose", True)
    if isinstance(clean_prose, str):
        clean_prose = clean_prose.lower() not in ("false", "0", "no")

    providers = await runtime.snapshot_under_lock()
    try:
        raw_text = await asyncio.to_thread(
            providers.stt.transcribe, audio_bytes, sample_rate=sample_rate
        )
    except ProviderError as e:
        status_code = 400 if "empty" in e.reason else 502
        return JSONResponse(
            {"ok": False, "error": str(e), "reason": e.reason}, status_code=status_code
        )
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "reason": swallowed("stt_failed", e)},
            status_code=500,
        )
    text = CleanProseFormatter.format(raw_text) if clean_prose else raw_text
    return JSONResponse({"ok": True, "text": text, "raw_text": raw_text})


# ----------------------------------------------------------------- audio ---


@router.get("/audio/{audio_id}")
def audio_get(audio_id: str) -> Response:
    wav = get_audio(audio_id)
    if wav is None:
        return JSONResponse(
            {"reason": "audio_not_found", "audio_id": audio_id}, status_code=404
        )
    return Response(content=wav, media_type="audio/wav")
