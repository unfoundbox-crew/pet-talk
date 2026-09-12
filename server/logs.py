"""One logger for the whole server.

Law 1 (AGENTS.md): fail closed with named reasons. Nothing is swallowed
silently — every caught exception goes through :func:`swallowed` with a
snake_case reason so it shows up in the log even when the loop stays up.
"""
from __future__ import annotations

import logging
import os
import re

LOGGER_NAME = "pet_talk.server"

log: logging.Logger = logging.getLogger(LOGGER_NAME)

if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    log.addHandler(_handler)
    log.propagate = False
log.setLevel(os.environ.get("PET_TALK_LOG_LEVEL", "INFO").upper())


def swallowed(
    reason: str, exc: BaseException | None = None, /, **context: object
) -> str:
    """Log an exception the caller deliberately kept from killing the loop.

    Returns the reason so callers can hand it straight to ``agent.error``.
    ``reason`` and ``exc`` are positional-only, so a context key of either
    name (``reason=`` is a natural one to want) cannot collide with them.
    """
    extras = " ".join(f"{k}={v!r}" for k, v in context.items())
    log.warning(
        "swallowed reason=%s %s%s",
        reason,
        extras,
        f" exc={type(exc).__name__}: {exc}" if exc is not None else "",
    )
    return reason


# ---------------------------------------------------------------------------
# Redaction: a home directory is the machine's owner, by name
# ---------------------------------------------------------------------------

#: ``/Users/saurabh/...`` and ``/home/saurabh/...`` name the person at the
#: keyboard. Any of it is a privacy leak in a frame, a log line or a receipt —
#: and an absolute path tells a reader nothing they can use anyway.
_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s:'\"]+")


def redact_home(text: object) -> str:
    """Replace every ``/Users/<name>`` or ``/home/<name>`` prefix with ``~``.

    One helper, used by :func:`server.frames.error_frame` (so no named reason's
    detail can carry a username — archie's stderr names paths) and by the
    receipt source path. Non-strings come back as ``str(text)`` so a caller
    never has to check first.
    """
    if text is None:
        return ""
    return _HOME_RE.sub("~", str(text))
