"""Compact live grounding injected into the system prompt.

Three rules this module exists to keep:

* No absolute paths in tree. The repo root is derived from ``__file__``; any
  *extra* repos to report come from ``PET_TALK_GROUNDING_REPOS`` (a
  colon-separated list of paths), and there are none by default.
* Nothing blocks the event loop. Every subprocess goes through
  ``asyncio.create_subprocess_exec`` with a hard timeout.
* Bounded output. The whole block stays under
  :data:`MAX_GROUNDING_CHARS` so it cannot blow up a prompt.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Optional

from .logs import log, swallowed
from .settings import REPO_ROOT

MAX_GROUNDING_CHARS = 460
GIT_TIMEOUT_S = 1.0
AX_TIMEOUT_S = 0.3
#: How long a detached reaper waits on a killed child before giving up on it.
REAP_TIMEOUT_S = 5.0
AX_ENV_FLAG = "PET_TALK_AX"
AX_BIN_ENV = "PET_TALK_HOTKEY_BIN"
GROUNDING_REPOS_ENV = "PET_TALK_GROUNDING_REPOS"

#: Strong refs to detached reaper tasks, so they are not garbage collected
#: mid-flight (asyncio only keeps weak references to running tasks).
_REAPERS: set[asyncio.Task] = set()

VOICE_GUARD = (
    "(INTERNAL CONTEXT ONLY — never recite file paths, git commands, or raw "
    "ports aloud.)"
)


def repo_root() -> str:
    """This checkout's root, resolved from the module location."""
    return REPO_ROOT


def hotkey_bin() -> str:
    """Path to ``pet-talk-hotkey``: env override, else ``<repo>/bin/``."""
    return os.environ.get(AX_BIN_ENV) or os.path.join(repo_root(), "bin", "pet-talk-hotkey")


def ax_enabled() -> bool:
    return os.environ.get(AX_ENV_FLAG, "") == "1"


def grounding_repos() -> list[str]:
    """Extra repos to report, from ``PET_TALK_GROUNDING_REPOS``. Empty by default."""
    raw = os.environ.get(GROUNDING_REPOS_ENV, "")
    return [p for p in (part.strip() for part in raw.split(":")) if p]


@dataclass(frozen=True)
class AxSnapshot:
    """One line of accessibility context from the hotkey helper."""

    app: str
    window: str
    selection: Optional[str] = None
    path: Optional[str] = None

    def as_line(self) -> str:
        return f"Screen: {self.app} — {self.window}".strip()


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    """Wait on a killed child off the caller's critical path."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=REAP_TIMEOUT_S)
    except asyncio.TimeoutError:
        swallowed("grounding_subprocess_unreaped", None, pid=proc.pid)
    except Exception as e:  # pragma: no cover — reaping is best effort
        swallowed("grounding_subprocess_reap_failed", e, pid=proc.pid)


def _abandon(proc: "asyncio.subprocess.Process") -> None:
    """Kill a child and reap it in the background.

    Never await its exit here: a grandchild that inherited the stdout pipe
    keeps ``proc.wait()`` blocked until IT exits, which would make a 300ms
    timeout take as long as the command wanted to run (measured: 5.0s).
    """
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:  # pragma: no cover
            swallowed("grounding_subprocess_kill_failed", e, pid=proc.pid)
    task = asyncio.ensure_future(_reap(proc))
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)


async def _run(argv: list[str], timeout_s: float) -> Optional[str]:
    """Run a command off the loop with a hard timeout. None on any failure."""
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        swallowed("grounding_subprocess_timeout", None, argv=argv[0], timeout_s=timeout_s)
        if proc is not None:
            _abandon(proc)
        return None
    except FileNotFoundError as e:
        swallowed("grounding_subprocess_missing", e, argv=argv[0])
        return None
    except (OSError, ValueError) as e:
        swallowed("grounding_subprocess_failed", e, argv=argv[0])
        return None
    if proc.returncode != 0:
        swallowed("grounding_subprocess_nonzero", None, argv=argv[0], rc=proc.returncode)
        return None
    return stdout.decode("utf-8", "replace").strip()


async def git_head_line(repo_path: str) -> Optional[str]:
    """``<basename>: <short log>`` for one repo, or None."""
    if not os.path.isdir(repo_path):
        swallowed("grounding_repo_missing", None, repo=os.path.basename(repo_path))
        return None
    out = await _run(["git", "-C", repo_path, "log", "-n", "1", "--oneline"], GIT_TIMEOUT_S)
    if not out:
        return None
    return f"{os.path.basename(repo_path.rstrip(os.sep))}: {out.splitlines()[0][:60]}"


async def ax_snapshot() -> Optional[AxSnapshot]:
    """Ask ``pet-talk-hotkey ax`` what the user is looking at.

    Contract (COMMON.md): ONE JSON line on stdout, exit 0 either way —
    ``{"ok":true,"app":..,"window":..,"selection":..,"path":..}`` or
    ``{"ok":false,"reason":..}``. Any deviation injects nothing and logs why.
    """
    if not ax_enabled():
        return None
    out = await _run([hotkey_bin(), "ax"], AX_TIMEOUT_S)
    if not out:
        return None
    line = out.splitlines()[0].strip()
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        swallowed("ax_bad_json", e, line=line[:120])
        return None
    if not isinstance(data, dict):
        swallowed("ax_bad_json", None, line=line[:120])
        return None
    if not data.get("ok"):
        swallowed("ax_not_ok", None, ax_reason=data.get("reason", "unspecified"))
        return None
    app = str(data.get("app") or "").strip()
    window = str(data.get("window") or "").strip()
    if not app and not window:
        swallowed("ax_empty_context", None)
        return None
    return AxSnapshot(
        app=app or "unknown app",
        window=window or "unknown window",
        selection=(str(data["selection"]) if data.get("selection") else None),
        path=(str(data["path"]) if data.get("path") else None),
    )


def static_lines() -> list[str]:
    """Grounding that needs no subprocess (safe from sync callers)."""
    return ["Voice loop: pet-talk duplex, local-first tyres."]


def get_session_grounding() -> str:
    """Synchronous, subprocess-free grounding.

    Kept for callers that are not on the event loop; the live version with
    git and AX context is :func:`collect_grounding`.
    """
    return _assemble(static_lines())


def _assemble(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    parts.append(VOICE_GUARD)
    text = "\n".join(parts)
    if len(text) > MAX_GROUNDING_CHARS:
        text = text[: MAX_GROUNDING_CHARS - 1].rstrip() + "…"
    return text


async def collect_grounding() -> str:
    """Full grounding block: repo heads, AX screen line, voice guard."""
    repos = [repo_root(), *grounding_repos()]
    tasks = [git_head_line(path) for path in repos]
    results = await asyncio.gather(*tasks, ax_snapshot(), return_exceptions=True)

    parts: list[str] = []
    for item in results[: len(tasks)]:
        if isinstance(item, BaseException):
            swallowed("grounding_repo_task_failed", item)
        elif item:
            parts.append(item)

    ax = results[-1]
    if isinstance(ax, BaseException):
        swallowed("ax_task_failed", ax)
    elif isinstance(ax, AxSnapshot):
        parts.append(ax.as_line())

    parts.extend(static_lines())
    text = _assemble(parts)
    log.debug("grounding_built chars=%d parts=%d", len(text), len(parts))
    return text
