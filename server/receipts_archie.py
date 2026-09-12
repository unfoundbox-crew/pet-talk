"""server/receipts_archie.py — the Archie (AgentWorth) adapter.

pet-talk asks AgentWorth what actually happened on this machine: which
session wrote a file, which commits proved nothing, where a lane left off.
It asks the **`archie` CLI with `--json`**, never an MCP server and never a
coding agent in the loop — the voice loop has no Claude in it, so a tool that
needs one is not available to it.

Fail-closed, one reason each (AGENTS.md law 1):

  ``receipts_unavailable``  the binary is missing, exits non-zero, or prints
                            something that is not the JSON we asked for.
  ``receipt_stale_index``   raised by callers, not here: see
                            :func:`index_state` and ``server/receipts.py``.

Config only, no absolute paths in tree (law 3):

  ``ARCHIE_BIN``   path to the binary. Default: ``archie`` on ``PATH``.
  ``ARCHIE_REPO``  default repo/workspace for a receipt. Default: ``os.getcwd()``.
  ``ARCHIE_TIMEOUT_S``  per-call wall clock. Default 8.

Three seams exist so tests are hermetic — no subprocess, no git, no clock:
:func:`set_runner`, :func:`set_clock`, :func:`set_head_reader`. Each takes
``None`` to restore the real thing.

Field names below were probed against archie 0.1.23 on 2026-09-12, not
recalled. `session wake --json` carries `index.last_scanned_at`; that is the
only field either product publishes for scan freshness.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .logs import log
from .providers import ProviderError

UNAVAILABLE = "receipts_unavailable"

# --- seams -----------------------------------------------------------------

Runner = Callable[[list[str], str], str]
Clock = Callable[[], datetime]
HeadReader = Callable[[str], Optional[datetime]]

_runner: Optional[Runner] = None
_clock: Optional[Clock] = None
_head_reader: Optional[HeadReader] = None


def set_runner(fn: Optional[Runner]) -> None:
    """Replace the `archie` subprocess. ``None`` restores the real binary."""
    global _runner
    _runner = fn


def set_clock(fn: Optional[Clock]) -> None:
    """Replace "now". ``None`` restores the wall clock."""
    global _clock
    _clock = fn


def set_head_reader(fn: Optional[HeadReader]) -> None:
    """Replace the git HEAD commit-time read. ``None`` restores git."""
    global _head_reader
    _head_reader = fn


def now() -> datetime:
    return (_clock or (lambda: datetime.now(timezone.utc)))()


def default_repo() -> str:
    return os.environ.get("ARCHIE_REPO") or os.getcwd()


# --- the binary ------------------------------------------------------------


def archie_bin() -> str:
    """Resolve the CLI. Raises ``receipts_unavailable`` when there is none."""
    configured = os.environ.get("ARCHIE_BIN", "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise ProviderError(UNAVAILABLE, "ARCHIE_BIN is not an executable file")
    found = shutil.which("archie")
    if found:
        return found
    raise ProviderError(UNAVAILABLE, "no `archie` on PATH and ARCHIE_BIN unset")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("ARCHIE_TIMEOUT_S", "8"))
    except ValueError:
        return 8.0


def _real_runner(args: list[str], cwd: str) -> str:
    cmd = [archie_bin(), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd if os.path.isdir(cwd) else None,
            capture_output=True,
            text=True,
            timeout=_timeout_s(),
            check=False,
        )
    except FileNotFoundError as e:
        raise ProviderError(UNAVAILABLE, "archie binary vanished between resolve and run") from e
    except subprocess.TimeoutExpired as e:
        raise ProviderError(UNAVAILABLE, f"archie timed out after {_timeout_s()}s") from e
    if proc.returncode != 0:
        # stderr can name a path; keep the detail short and never a secret.
        first = (proc.stderr or "").strip().splitlines()
        raise ProviderError(
            UNAVAILABLE, f"archie exit {proc.returncode}: {first[0] if first else 'no stderr'}"
        )
    return proc.stdout


#: A path hint is a relative path or pattern, never an option and never an
#: escape upwards. `archie repo blame <hint>` is argv, so a hint of
#: ``--exec=...`` would be read as a flag by any CLI that grows one, and
#: ``../../..`` walks out of the repo the caller named. Both are refused by
#: name rather than quoted and hoped for; ``--`` below is the second lock.
BAD_HINT = "receipt_bad_path_hint"


def safe_positional(value: str, *, what: str = "path") -> str:
    """Check one positional argument. Raises ``receipt_bad_path_hint``.

    Refuses an empty value, anything starting with ``-`` (an option, not a
    path) and anything containing ``..`` (a walk out of the named repo).
    """
    text = str(value or "").strip()
    if not text:
        raise ProviderError(BAD_HINT, f"empty {what}")
    if text.startswith("-"):
        raise ProviderError(BAD_HINT, f"{what} looks like an option: {text!r}")
    if ".." in text:
        raise ProviderError(BAD_HINT, f"{what} escapes the repo: {text!r}")
    return text


def run_json(args: list[str], repo: Optional[str] = None) -> Any:
    """Run one `archie … --json` call and decode it. Fails closed."""
    cwd = repo or default_repo()
    raw = (_runner or _real_runner)(args, cwd)
    if not (raw or "").strip():
        raise ProviderError(UNAVAILABLE, f"archie printed nothing for: {' '.join(args)}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProviderError(UNAVAILABLE, f"archie printed non-JSON for: {' '.join(args)}") from e


# --- time ------------------------------------------------------------------


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse one RFC-3339 timestamp as archie prints it. None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _real_head_reader(repo: str) -> Optional[datetime]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%cI"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_ts(proc.stdout.strip())


def head_committed_at(repo: str) -> Optional[datetime]:
    return (_head_reader or _real_head_reader)(repo)


# --- the freshness law -----------------------------------------------------


@dataclass(frozen=True)
class IndexState:
    """What the index knows, and whether it knows it late.

    ``fresh`` is false whenever the last scan predates the repo's last
    commit: the index cannot have seen the work that commit contains, so it
    must not be allowed to assign blame for it.
    """

    repo: str
    last_scanned_at: Optional[datetime]
    head_committed_at: Optional[datetime]
    index_age_s: int
    behind_s: int
    fresh: bool


def index_state(repo: Optional[str] = None) -> IndexState:
    """Read scan freshness for one checkout. Raises ``receipts_unavailable``."""
    target = repo or default_repo()
    wake = run_json(["session", "wake", "--json", "--workspace", target], target)
    if not isinstance(wake, dict):
        raise ProviderError(UNAVAILABLE, "session wake did not return an object")
    scanned = parse_ts((wake.get("index") or {}).get("last_scanned_at")) or parse_ts(
        (wake.get("receipt") or {}).get("index_last_updated")
    )
    if scanned is None:
        raise ProviderError(UNAVAILABLE, "the index reports no last_scanned_at")
    head = head_committed_at(target)
    age = max(0, int((now() - scanned).total_seconds()))
    behind = 0
    if head is not None and head > scanned:
        behind = int((head - scanned).total_seconds())
    state = IndexState(
        repo=target,
        last_scanned_at=scanned,
        head_committed_at=head,
        index_age_s=age,
        behind_s=behind,
        fresh=behind == 0,
    )
    log.debug(
        "archie_index repo=%s age_s=%d behind_s=%d fresh=%s", target, age, behind, state.fresh
    )
    return state


def wake(repo: Optional[str] = None) -> dict:
    """`archie session wake --json` — the carry-forward for one checkout."""
    target = repo or default_repo()
    out = run_json(["session", "wake", "--json", "--workspace", target], target)
    if not isinstance(out, dict):
        raise ProviderError(UNAVAILABLE, "session wake did not return an object")
    return out


def suspect_commits(repo: Optional[str] = None) -> dict:
    """`archie repo suspect --json` — commits whose session proved nothing."""
    target = repo or default_repo()
    out = run_json(["repo", "suspect", "--json", "--repo", target], target)
    if not isinstance(out, dict):
        raise ProviderError(UNAVAILABLE, "repo suspect did not return an object")
    return out


def repo_blame(path: str, repo: Optional[str] = None) -> list[dict]:
    """`archie repo blame --json -- <path>` — who authored this file.

    The path is checked and passed after ``--``, so a hint that looks like an
    option can never be parsed as one. It arrives from a spoken sentence
    (:func:`server.receipts.path_hint`), which is user input by any honest
    reading.
    """
    target = repo or default_repo()
    safe = safe_positional(path, what="path hint")
    out = run_json(["repo", "blame", "--json", "--", safe], target)
    if isinstance(out, dict):  # tolerate a wrapped shape without guessing fields
        out = out.get("rows") or out.get("blame") or []
    if not isinstance(out, list):
        raise ProviderError(UNAVAILABLE, "repo blame did not return a list")
    return [row for row in out if isinstance(row, dict)]


def session_list(limit: int = 5, repo: Optional[str] = None) -> list[dict]:
    """`archie session list --json` — the most recent indexed sessions."""
    target = repo or default_repo()
    out = run_json(["session", "list", "--json", "--limit", str(limit)], target)
    if not isinstance(out, list):
        raise ProviderError(UNAVAILABLE, "session list did not return a list")
    return [row for row in out if isinstance(row, dict)]
