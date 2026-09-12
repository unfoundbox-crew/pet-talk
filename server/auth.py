"""Local access control: the studio token and the egress allowlist.

Why this module exists (the hole it closes): ``POST /settings`` used to be
unauthenticated, and it accepts ``*_base_url`` while keeping the stored
credential. Any local process — or a page served from a CORS-allowed origin —
could point ``llm_base_url`` at its own host, trigger a turn, and receive
``Authorization: Bearer <the user's key>``. Two locks, both required:

1. **Token.** Every mutating HTTP route and the WS handshake require the
   header ``X-Studio-Token``. The value comes from env ``STUDIO_TOKEN``, else
   the file named by ``STUDIO_TOKEN_FILE``, else a random token generated at
   startup and written 0600 to ``<repo>/.qa-scratch/studio.token`` (gitignored)
   so a local client can read it. The path is logged once; the value never is.
2. **Allowlist.** A ``*_base_url`` must resolve to loopback, an RFC1918
   address, a host named in ``PET_TALK_ALLOWED_HOSTS``, or one of the provider
   hostnames the code already ships as defaults. Anything else is refused with
   ``base_url_not_allowed`` — fail closed, law 1.

Reads (``GET /health``, ``/voices``, ``/audio/{id}``) stay open: they carry no
credential and the cockpit needs them before it has read the token.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import urllib.parse
from typing import Optional

from fastapi import HTTPException, Request, WebSocket

from .logs import log
from .settings import REPO_ROOT

#: Header carrying the studio token. Also accepted as a ``?token=`` query
#: parameter on the WS handshake only — browsers cannot set headers on a
#: ``new WebSocket()`` request, and the cockpit is a browser.
STUDIO_TOKEN_HEADER = "x-studio-token"
WS_TOKEN_QUERY = "token"

#: Where a generated token is written when neither env var is configured.
TOKEN_DIR = os.path.join(REPO_ROOT, ".qa-scratch")
TOKEN_PATH = os.path.join(TOKEN_DIR, "studio.token")

#: Provider hostnames already hardcoded as defaults in ``server/providers``
#: and ``server/settings.py``. Kept in one place so the allowlist cannot drift
#: from what the tyres actually dial; qa/test_security.py asserts the match.
PROVIDER_HOSTS = frozenset(
    {
        "api.anthropic.com",  # providers/llm.py, settings.LLM_BASE_URL_DEFAULTS
        "api.deepgram.com",  # providers/stt.py LISTEN_URL, tts.py SPEAK_URL
        "api.elevenlabs.io",  # providers/tts.py ElevenLabsTTS.API
        "api.groq.com",  # providers/stt.py, providers/llm.py
        "api.openai.com",  # providers/stt.py, providers/llm.py
        "api.opencode.ai",  # providers/llm.py
        "api.smallest.ai",  # providers/tts.py SmallestAI API_URL
        "generativelanguage.googleapis.com",  # providers/llm.py, settings.py
    }
)

_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

_token: Optional[str] = None


# ----------------------------------------------------------------- token ---


class StudioAuthError(HTTPException):
    """401 with a named reason. Handled in app.py so the body stays our shape."""

    def __init__(self, reason: str = "unauthorized") -> None:
        super().__init__(status_code=401, detail=reason)
        self.reason = reason


def _read_token_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_generated_token() -> str:
    token = secrets.token_urlsafe(32)
    os.makedirs(TOKEN_DIR, exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.chmod(TOKEN_PATH, 0o600)
    return token


def _resolve_token() -> str:
    """env STUDIO_TOKEN -> STUDIO_TOKEN_FILE -> the generated file -> generate.

    An existing generated file is reused so a restart does not invalidate the
    token a running cockpit already read.
    """
    env_token = (os.environ.get("STUDIO_TOKEN") or "").strip()
    if env_token:
        log.info("studio_token source=env:STUDIO_TOKEN")
        return env_token

    token_file = (os.environ.get("STUDIO_TOKEN_FILE") or "").strip()
    if token_file:
        from_file = _read_token_file(token_file)
        if from_file:
            log.info("studio_token source=STUDIO_TOKEN_FILE path=%s", token_file)
            return from_file

    existing = _read_token_file(TOKEN_PATH)
    if existing:
        log.info("studio_token source=generated path=%s (reused)", TOKEN_PATH)
        return existing

    token = _write_generated_token()
    log.info(
        "studio_token source=generated path=%s mode=0600 — local clients read "
        "the token from this file (VITE_STUDIO_TOKEN for the web cockpit)",
        TOKEN_PATH,
    )
    return token


def studio_token() -> str:
    """The expected token. Resolved once per process; never logged."""
    global _token
    if _token is None:
        _token = _resolve_token()
    return _token


def reset_token_cache() -> None:
    """Test hook: forget the resolved token so env changes take effect."""
    global _token
    _token = None


def token_matches(supplied: Optional[str]) -> bool:
    expected = studio_token()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(supplied), expected)


def require_studio_token(request: Request) -> None:
    """FastAPI dependency for every mutating route. Fails closed with 401."""
    if not token_matches(request.headers.get(STUDIO_TOKEN_HEADER)):
        raise StudioAuthError()


def ws_token(ws: WebSocket) -> Optional[str]:
    header = ws.headers.get(STUDIO_TOKEN_HEADER)
    if header:
        return header
    return ws.query_params.get(WS_TOKEN_QUERY)


def ws_token_ok(ws: WebSocket) -> bool:
    return token_matches(ws_token(ws))


# ------------------------------------------------------------- allowlist ---


def allowed_hosts() -> frozenset[str]:
    """Provider defaults plus anything ``PET_TALK_ALLOWED_HOSTS`` names."""
    extra = {
        h.strip().lower()
        for h in (os.environ.get("PET_TALK_ALLOWED_HOSTS") or "").split(",")
        if h.strip()
    }
    return frozenset(PROVIDER_HOSTS | extra)


def base_url_allowed(url: str) -> bool:
    """True when a ``*_base_url`` may be dialled with a stored credential."""
    raw = str(url or "").strip()
    if not raw:
        return True  # empty means "unset"; the provider picks its own default
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        host = (parsed.hostname or "").lower()
    except ValueError:  # malformed IPv6 literal
        return False
    if not host:
        return False
    if host in allowed_hosts():
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")
    return ip.is_loopback or any(ip in net for net in _RFC1918)


def offending_base_url(req: dict) -> Optional[str]:
    """Name of the first ``*_base_url`` field in ``req`` that fails the
    allowlist, or None when every one of them passes."""
    for name, value in req.items():
        if not isinstance(name, str) or not name.endswith("_base_url"):
            continue
        if not base_url_allowed(str(value or "")):
            return name
    return None
