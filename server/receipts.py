"""server/receipts.py — no spoken claim about work stands without a receipt.

Two rules, both fail-closed (AGENTS.md law 1):

1. **Receipt rule.** When a sentence claims work was done — tests green, a
   commit landed, a file changed, someone broke something — it either ships
   an ``agent.receipt`` frame naming the session, commit or scan it came
   from, or it is refused by name. It is never spoken bare.

2. **Freshness law.** If the Archie index was last scanned before the repo's
   last commit, the index cannot have seen that commit's work. Every blame
   claim is then refused with ``receipt_stale_index`` and the spoken line
   *"My index is N minutes behind the last commit. Rescan?"* — never an
   answer, never a silent guess.

Named reasons, the whole catalogue:

  ``receipt_missing``       the claim is about work, and nothing in the index
                            proves it.
  ``receipt_stale_index``   the scan predates HEAD; a blame claim is refused.
  ``receipts_unavailable``  the `archie` CLI is missing or would not answer.

The wire frame (docs/SPEC.md §4.2)::

    agent.receipt {
      turn_id, claim,
      source: { kind: session|commit|scan, id, repo, path },
      tokens, cost_usd|null, index_age_s, fresh
    }

Spoken copy obeys the Noun Rule: a subject at the front, ~20 words, 45 hard
ceiling, no frame names and no engineering identifiers.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from . import receipts_archie as archie
from .frames import error_frame, frame
from .logs import log, redact_home
from .providers import ProviderError

RECEIPT_FRAME = "agent.receipt"

MISSING = "receipt_missing"
STALE_INDEX = "receipt_stale_index"
UNAVAILABLE = "receipts_unavailable"
#: A hint that would have become argv but is an option or a path walk. Raised
#: by :func:`server.receipts_archie.safe_positional`; refused, never quoted.
BAD_HINT = archie.BAD_HINT

REASONS = frozenset({MISSING, STALE_INDEX, UNAVAILABLE, BAD_HINT})

WORD_CEILING = 45

# ---------------------------------------------------------------------------
# The classifier: is this sentence a claim about work?
#
# Deliberately small and ordered. A keyword set plus an optional persona hint,
# not a model: it runs on every spoken sentence, inside the turn, and a
# classifier that needs a network call cannot.
# ---------------------------------------------------------------------------

CLAIM_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Attribution first: "a session wrote it" is blame, not a file change.
    (
        "blame",
        (
            r"who broke",
            r"\bbroke\b",
            r"\bblame\b",
            r"\bsuspect\b",
            r"never (?:verified|proved|checked)",
            r"(?:\w+ )?session (?:wrote|authored|touched|changed)",
            r"\bauthored\b",
            r"proved nothing",
        ),
    ),
    (
        "session",
        (
            r"where (?:was|were) (?:i|we)",
            r"where did (?:i|we) leave",
            r"left off",
            r"carry (?:it )?forward",
            r"last session",
            r"picking up where",
        ),
    ),
    (
        "tests",
        (
            r"\btests? (?:are|is|were|all)? ?(?:green|passing|passed)",
            r"\b(?:suite|tests?|checks?|ci|gate) (?:passed|passes|is green|are green)",
            r"\ban? \w+ (?:suite|gate) (?:passed|is green)",
            r"\bit passed\b",
            r"\ball green\b",
            r"zero fail",
        ),
    ),
    (
        "commit",
        (
            r"\bcommit(?:ted|s)?\b",
            r"\blanded\b",
            r"\bpushed\b",
            r"\bmerged\b",
            r"\bopened a (?:pr|pull request)",
        ),
    ),
    (
        "file",
        (
            r"\b(?:changed|edited|rewrote|wrote|updated|fixed|added|removed|deleted)\b",
            r"\brefactored\b",
        ),
    ),
)

# Blame-shaped claims are the ones a stale index must never answer.
BLAME_KINDS = frozenset({"blame"})

_COMPILED = tuple(
    (kind, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for kind, patterns in CLAIM_KINDS
)


@dataclass(frozen=True)
class Claim:
    """One spoken sentence that asserts something about work done."""

    text: str
    kind: str
    matched: str


def classify_claim(text: str, persona: Optional[str] = None) -> Optional[Claim]:
    """Return the Claim this sentence makes, or ``None`` if it makes none.

    ``persona`` is a hint only. A persona that talks about repositories makes
    an ambiguous sentence more likely to be a claim; it never turns a
    sentence with no claim keyword into one.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    for kind, patterns in _COMPILED:
        for pattern in patterns:
            hit = pattern.search(text)
            if hit:
                log.debug("claim kind=%s persona=%s matched=%r", kind, persona, hit.group(0))
                return Claim(text=text.strip(), kind=kind, matched=hit.group(0))
    return None


# ---------------------------------------------------------------------------
# Spoken copy. Noun Rule: subject first, ~20 words, 45 ceiling.
# ---------------------------------------------------------------------------


def stale_index_line(behind_s: int) -> str:
    """The refusal a stale index earns, instead of an answer."""
    minutes = max(1, int(round(max(0, behind_s) / 60.0)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"My index is {minutes} {unit} behind the last commit. Rescan?"


REFUSAL_LINES = {
    MISSING: "My index has nothing that proves this, so I am not going to claim it.",
    UNAVAILABLE: "My session index is not answering, so I cannot prove that right now.",
    BAD_HINT: "I could not read that as a file name, so I am not going to claim it.",
}


def refusal_line(reason: str, behind_s: int = 0) -> str:
    if reason == STALE_INDEX:
        return stale_index_line(behind_s)
    return REFUSAL_LINES.get(reason, REFUSAL_LINES[UNAVAILABLE])


ADAPTER_NAMES = {
    "claude_code": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
    "cursor": "Cursor",
    "goose": "Goose",
    "herdr": "Herdr",
    "aider": "Aider",
}


def humanize_adapter(adapter: Any) -> str:
    """A name a person says out loud. `claude_code` is an identifier, not a name."""
    key = str(adapter or "").strip().lower()
    if not key:
        return "an agent"
    return ADAPTER_NAMES.get(key, key.replace("_", " ").title())


def _cap(text: str, words: int = 20) -> str:
    parts = (text or "").split()
    if len(parts) <= words:
        return text
    return " ".join(parts[:words]).rstrip(",;:") + "."


# ---------------------------------------------------------------------------
# Receipt payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    kind: str  # session | commit | scan
    id: str
    repo: str
    path: str


def _payload(claim_text: str, source: Source, tokens: int, state: archie.IndexState) -> dict:
    """The receipt body, shared by the frame and the voice routes.

    ``cost_usd`` is always ``None`` today: archie 0.1.23's `repo blame`,
    `repo suspect`, `session wake` and `session list` JSON all report tokens
    and no dollar figure (probed 2026-09-12). Reported upstream, not faked
    here — a computed price would be a number nobody measured.
    """
    return {
        "claim": claim_text,
        "source": {
            "kind": source.kind,
            "id": source.id,
            "repo": source.repo,
            "path": source.path,
        },
        "tokens": int(tokens or 0),
        "cost_usd": None,
        "index_age_s": state.index_age_s,
        "fresh": state.fresh,
    }


def _repo_identity(repo: str) -> str:
    """A short repo name for the wire. Never an absolute local path (law 3)."""
    trimmed = (repo or "").rstrip("/")
    parts = [p for p in trimmed.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else ""


def _short_path(path: str, repo: str = "") -> str:
    """A path a person can read, never an absolute one (law 3).

    Order: strip a worktree prefix, else make it relative to the repo the
    receipt names, else fall back to the basename. A receipt used to carry
    ``/Users/<name>/code/...`` straight onto the wire, which names the machine's
    owner and tells the reader nothing they can act on.
    """
    trimmed = (path or "").rstrip("/")
    if not trimmed:
        return ""
    marker = "/.claude/worktrees/"
    if marker in trimmed:
        tail = trimmed.split(marker, 1)[1]
        return tail.split("/", 1)[1] if "/" in tail else tail
    if not os.path.isabs(trimmed):
        return trimmed
    base = (repo or "").rstrip("/")
    if base and trimmed.startswith(base + "/"):
        return trimmed[len(base) + 1:]
    # No repo to anchor it to: the basename is the most it may say.
    return redact_home(os.path.basename(trimmed))


_PATH_HINT = re.compile(r"[\w./-]*\.(?:py|ts|tsx|js|jsx|swift|rs|sh|md|json|yaml|yml)\b")
_STOPWORDS = frozenset({"the", "a", "an", "my", "our", "that", "this", "in", "file", "module"})


def path_hint(text: str) -> str:
    """A path or pattern for `archie repo blame` from what was said.

    A literal filename wins. Otherwise the remaining nouns become an
    underscore pattern — "the speak queue" -> "speak_queue" — which is how
    `repo blame` matches, by path pattern.

    The hint becomes argv. A spoken (or dictated, or attacker-supplied)
    sentence could name ``--exec=id.py`` or ``../../../etc/passwd.py``, both of
    which the filename regex happily matches; a leading ``-`` and any ``..``
    are refused here and the claim falls through to ``receipt_missing``
    instead. ``receipts_archie.safe_positional`` and the ``--`` separator are
    the other two locks.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    literal = _PATH_HINT.search(text)
    if literal:
        candidate = literal.group(0)
        if candidate.startswith("-") or ".." in candidate:
            log.info("receipt_bad_path_hint hint=%r", candidate)
            return ""
        return candidate
    words = [w for w in re.findall(r"[a-zA-Z0-9_]+", text.lower()) if w not in _STOPWORDS]
    return "_".join(words[:3])


def _commit_source(repo: str, hint: str) -> tuple[Source, int, dict]:
    """Who wrote this file, and which unproven commit carries it.

    `repo blame` names the session and its token spend; `repo suspect` turns
    that session into the commit nobody proved. Neither call alone carries
    both, so a commit receipt needs both — and if blame has no row, there is
    nothing to prove the claim with and we say so (``receipt_missing``).
    """
    if not hint:
        raise ProviderError(MISSING, "the claim named no file to trace")
    rows = archie.repo_blame(hint, repo)
    if not rows:
        raise ProviderError(MISSING, f"no blame row for {hint!r}")
    row = max(rows, key=lambda r: str(r.get("modified_at") or ""))
    session_id = str(row.get("session_id") or "").strip()

    short_sha = ""
    report: dict = {}
    try:
        report = archie.suspect_commits(repo)
    except ProviderError as e:  # blame still proves authorship; note the gap
        log.debug("suspect_unavailable detail=%s", e.detail)
    for commit in report.get("suspect") or []:
        if not isinstance(commit, dict):
            continue
        ids = {
            str((s or {}).get("session_id") or "")
            for s in (commit.get("sessions") or [])
            if isinstance(s, dict)
        }
        if session_id and session_id in ids:
            short_sha = str(commit.get("short_sha") or commit.get("sha") or "")[:7]
            break

    source = Source(
        kind="commit" if short_sha else "session",
        id=short_sha or session_id,
        repo=_repo_identity(report.get("repo_identity") or repo),
        path=_short_path(str(row.get("file_path") or ""), repo),
    )
    if not source.id:
        raise ProviderError(MISSING, "the blame row carried no session id")
    merged = {**row, "short_sha": short_sha}
    return source, int(row.get("total_tokens") or 0), merged


def _session_source(repo: str) -> tuple[Source, int, dict]:
    """The session this checkout was last worked in."""
    report = archie.wake(repo)
    row = report.get("session")
    if not isinstance(row, dict) or not row.get("session_id"):
        raise ProviderError(MISSING, "the index holds no session for this checkout")
    return (
        Source(
            kind="session",
            id=str(row.get("session_id")),
            repo=_repo_identity((report.get("receipt") or {}).get("repo") or repo),
            path="",
        ),
        int(row.get("total_tokens") or 0),
        report,
    )


def _resolve_source(claim: Claim, repo: str) -> tuple[Source, int, dict]:
    """A commit receipt when the sentence names a file, else a session one."""
    if claim.kind in ("blame", "commit", "file"):
        hint = path_hint(claim.text)
        if hint:
            try:
                return _commit_source(repo, hint)
            except ProviderError as e:
                if e.reason != MISSING:
                    raise
                log.debug("commit_source_missing hint=%s detail=%s", hint, e.detail)
    return _session_source(repo)


# ---------------------------------------------------------------------------
# attach(): the one call site the turn needs
# ---------------------------------------------------------------------------


def _turn_id_of(turn: Any) -> str:
    if isinstance(turn, str):
        return turn
    for attr in ("turn_id", "id"):
        value = getattr(turn, attr, None)
        if isinstance(value, str):
            return value
    return ""


def _repo_of(turn: Any, repo: Optional[str]) -> str:
    if repo:
        return repo
    from_turn = getattr(turn, "repo", None)
    if isinstance(from_turn, str) and from_turn:
        return from_turn
    return archie.default_repo()


def attach(turn: Any, claim: Any, *, repo: Optional[str] = None, persona: Optional[str] = None):
    """Prove one spoken sentence, or refuse it by name.

    Returns a wire-ready frame dict, as :func:`server.frames.frame` returns:

      * ``None`` — the sentence claims nothing about work. Nothing to prove.
      * ``agent.receipt`` — the claim is backed; the frame names its source.
      * ``agent.error`` — the claim cannot stand. ``reason`` is one of
        ``receipt_missing``, ``receipt_stale_index``, ``receipts_unavailable``
        and ``spoken`` carries the line to say instead.

    ``turn`` may be a turn id or any object carrying ``turn_id``. ``claim``
    may be the spoken sentence or an already-classified :class:`Claim`.
    """
    turn_id = _turn_id_of(turn)
    target = _repo_of(turn, repo)
    found = claim if isinstance(claim, Claim) else classify_claim(claim, persona=persona)
    if found is None:
        return None

    behind = 0
    try:
        state = archie.index_state(target)
        behind = state.behind_s
        if found.kind in BLAME_KINDS and not state.fresh:
            raise ProviderError(STALE_INDEX, f"scan is {state.behind_s}s behind HEAD")
        source, tokens, _raw = _resolve_source(found, target)
    except ProviderError as e:
        if e.reason == "frame_no_turn_id":  # never swallow a frame bug
            raise
        reason = e.reason if e.reason in REASONS else UNAVAILABLE
        log.info("receipt_refused turn_id=%s reason=%s detail=%s", turn_id, reason, e.detail)
        return error_frame(
            turn_id,
            reason,
            e.detail,
            claim=found.text,
            claim_kind=found.kind,
            spoken=refusal_line(reason, behind),
        )

    return frame(RECEIPT_FRAME, turn_id, **_payload(found.text, source, tokens, state))


# ---------------------------------------------------------------------------
# Voice routes, answered from Archie with receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What a route says, what proves it, and why it refused if it did."""

    spoken: str
    receipt: Optional[dict] = None
    refused: Optional[str] = None


def who_broke(thing: str, *, repo: Optional[str] = None) -> Answer:
    """"Who broke <thing>" — answered from suspect commits and file blame.

    Refused on a stale index: an index that has not seen HEAD cannot name
    who broke anything in it.
    """
    target = repo or archie.default_repo()
    try:
        state = archie.index_state(target)
    except ProviderError:
        return Answer(spoken=refusal_line(UNAVAILABLE), refused=UNAVAILABLE)
    if not state.fresh:
        return Answer(spoken=stale_index_line(state.behind_s), refused=STALE_INDEX)
    try:
        source, tokens, row = _commit_source(target, path_hint(thing))
    except ProviderError as e:
        reason = e.reason if e.reason in REASONS else UNAVAILABLE
        return Answer(spoken=refusal_line(reason, state.behind_s), refused=reason)

    adapter = humanize_adapter(row.get("adapter"))
    where = source.path or _describe(thing)
    if source.kind == "commit":
        spoken = f"A {adapter} session wrote {where}, and its commit proved nothing."
    else:
        spoken = f"A {adapter} session wrote {where}, and no commit of it was ever proved."
    spoken = _cap(spoken, 24)
    return Answer(spoken=spoken, receipt=_payload(spoken, source, tokens, state))


def where_was_i(lane: str, *, repo: Optional[str] = None) -> Answer:
    """"Where was I with <lane>" — answered from the checkout's carry-forward.

    Not a blame claim, so a stale index does not refuse it: recalling what a
    session did is still true when the scan is old. The receipt says
    ``fresh: false`` so the cockpit shows how old.
    """
    target = repo or archie.default_repo()
    try:
        state = archie.index_state(target)
    except ProviderError:
        return Answer(spoken=refusal_line(UNAVAILABLE), refused=UNAVAILABLE)
    try:
        source, tokens, report = _session_source(target)
    except ProviderError as e:
        reason = e.reason if e.reason in REASONS else UNAVAILABLE
        return Answer(spoken=refusal_line(reason, state.behind_s), refused=reason)

    session = report.get("session") or {}
    task = str(session.get("task") or "").strip()
    step = str((report.get("next") or {}).get("step") or "").strip()
    if task and step:
        spoken = f"You left off on {task}. Next up: {step}."
    elif task:
        spoken = f"You left off on {task}."
    else:
        spoken = f"Your last session here touched {_describe(lane)}."
    spoken = _cap(spoken, 24)
    return Answer(spoken=spoken, receipt=_payload(spoken, source, tokens, state))


def _describe(thing: str) -> str:
    cleaned = (thing or "").strip().rstrip(".?!,")
    return cleaned or "this work"


# ---------------------------------------------------------------------------
# Route matching, for the deterministic control router
# ---------------------------------------------------------------------------

WHO_BROKE = re.compile(r"^who\s+broke\s+(.+)$", re.IGNORECASE)
WHERE_WAS_I = re.compile(
    r"^where\s+(?:was|were)\s+(?:i|we)\s+(?:with|on|at)?\s*(.*)$", re.IGNORECASE
)


def answer_voice_route(text: str, *, repo: Optional[str] = None) -> Optional[Answer]:
    """Match one of the two Archie routes and answer it, or return ``None``.

    This is what the deterministic router calls. It owns no state and starts
    no turn, so it is safe to call before the LLM is consulted.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip().rstrip(".?!,")
    hit = WHO_BROKE.match(t)
    if hit:
        return who_broke(hit.group(1), repo=repo)
    hit = WHERE_WAS_I.match(t)
    if hit:
        return where_was_i(hit.group(1), repo=repo)
    return None


def enabled() -> bool:
    """Receipts are on unless switched off by config (law 2)."""
    return os.environ.get("RECEIPTS_ENABLED", "1").strip().lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# The turn hook: one line at the call site, on purpose
# ---------------------------------------------------------------------------


DEFAULT_RECEIPT_TIMEOUT_S = 2.0


def receipt_timeout_s() -> float:
    """Wall clock one receipt may cost the turn. ``PET_TALK_RECEIPT_TIMEOUT_S``.

    A receipt is evidence, not the answer: past this the turn finishes without
    it. An unparseable or non-positive value falls back to the default and says
    so, rather than disabling the bound.
    """
    raw = os.environ.get("PET_TALK_RECEIPT_TIMEOUT_S", "")
    if not raw.strip():
        return DEFAULT_RECEIPT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("bad_receipt_timeout_env value=%r", raw)
        return DEFAULT_RECEIPT_TIMEOUT_S
    if value <= 0:
        log.warning("bad_receipt_timeout_env value=%r", raw)
        return DEFAULT_RECEIPT_TIMEOUT_S
    return value


async def emit(ws: Any, turn_id: str, sentences: Any, persona: Optional[str] = None) -> int:
    """Prove every sentence a turn spoke. Returns how many frames went out.

    This exists so `turn.py` needs exactly one line and no try/except: a
    receipt never raises at the call site, never blocks the turn, and a
    sentence that claims nothing costs one regex pass.

    ``attach`` is synchronous and shells out to the `archie` CLI
    (``subprocess.run(timeout=8)``). Called directly it ran that subprocess on
    the event loop, so one slow index froze every socket on the process — a
    1.5 s archie meant 1.5 s of no heartbeat, no barge, no frame read. It runs
    in a worker thread under :func:`receipt_timeout_s` instead; past the
    timeout the receipt is named ``receipt_timeout`` and skipped, so
    ``agent.done`` is never held up by more than that per sentence.
    """
    from .frames import safe_send_json

    if not enabled() or not turn_id:
        return 0
    timeout = receipt_timeout_s()
    sent = 0
    for text in list(sentences or []):
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(attach, turn_id, text, persona=persona),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # The thread may still be blocked in archie; it is orphaned on
            # purpose, and its result is discarded when it lands.
            log.info(
                "receipt_timeout turn_id=%s timeout_s=%.2f", turn_id, timeout
            )
            continue
        except ProviderError as e:  # a frame bug must not kill a finished turn
            log.info("receipt_emit_failed turn_id=%s reason=%s", turn_id, e.reason)
            continue
        if payload and await safe_send_json(ws, payload):
            sent += 1
    return sent
