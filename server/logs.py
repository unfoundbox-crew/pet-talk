"""One logger for the whole server.

Law 1 (AGENTS.md): fail closed with named reasons. Nothing is swallowed
silently — every caught exception goes through :func:`swallowed` with a
snake_case reason so it shows up in the log even when the loop stays up.
"""
from __future__ import annotations

import logging
import os

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


def swallowed(reason: str, exc: BaseException | None = None, **context: object) -> str:
    """Log an exception the caller deliberately kept from killing the loop.

    Returns the reason so callers can hand it straight to ``agent.error``.
    """
    extras = " ".join(f"{k}={v!r}" for k, v in context.items())
    log.warning(
        "swallowed reason=%s %s%s",
        reason,
        extras,
        f" exc={type(exc).__name__}: {exc}" if exc is not None else "",
    )
    return reason
