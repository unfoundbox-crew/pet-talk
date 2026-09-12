#!/usr/bin/env python3
"""qa/test_receipts.py — Archie receipts and the freshness law.

Contract under test (docs/SPEC.md §4.2 `agent.receipt`, §4.3 reasons):

  - No spoken claim about work stands without a receipt. A sentence that
    claims work (tests green, a commit landed, a file changed) either
    carries an `agent.receipt` frame naming its source, or is refused by
    name — never spoken bare.
  - Freshness law: if the Archie index was last scanned before the repo's
    last commit, `fresh=false`, and any blame or "who broke" claim is
    refused with `receipt_stale_index` plus a spoken line that says how far
    behind the index is. The claim never reaches the wire.
  - Two voice routes are answered from Archie with receipts: "who broke X"
    (repo suspect / repo blame) and "where was I with X" (session wake /
    session list).

Hermetic by construction: every Archie call goes through an injected runner
(`receipts_archie.set_runner`) that returns fixture JSON, and git is faked by
injecting the HEAD commit time. No subprocess, no network, no index read.

The last test is gated behind PET_TALK_REAL_ENGINE=1: it runs the real
`archie` CLI against the live index on this machine and prints the index age.

Stdlib only. Run: `python3 qa/test_receipts.py -v`.
Exit 0 = pass/skip, nonzero = FAIL.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("STT_PROVIDER", "stub")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("TTS_PROVIDER", "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")

from server import receipts, receipts_archie  # noqa: E402
from server.providers import ProviderError  # noqa: E402

REPO = "/tmp/pet-talk-fixture-repo"
TURN = "t1-abc123"

# ---------------------------------------------------------------------------
# Fixtures: verbatim-shaped `archie <cmd> --json` payloads. Field names come
# from archie 0.1.23 on this machine (probed 2026-09-12), not from memory.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 12, 8, 13, 0, tzinfo=timezone.utc)
HEAD_AT = NOW - timedelta(minutes=30)          # last commit, 30 min ago
SCAN_FRESH = NOW - timedelta(minutes=7)        # scan after HEAD  -> fresh
SCAN_STALE = NOW - timedelta(minutes=41)       # scan before HEAD -> stale by 11 min


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def wake_fixture(scanned_at: datetime, *, session: bool = True) -> dict:
    """`archie session wake --json`."""
    return {
        "receipt": {
            "session_id": "9b17795f" if session else None,
            "repo": "unfoundbox-crew/pet-talk",
            "adapter": "claude_code",
            "generated_at": _iso(NOW),
            "index_last_updated": _iso(scanned_at),
            "redacted": False,
        },
        "workspace": REPO,
        "checkout": {
            "root": REPO,
            "is_worktree": True,
            "branch": "feat/overnight-hardening",
            "head_short": "b1848fd",
            "head_subject": "docs: record the first real end-to-end turn measurement",
            "dirty_files": 1,
        },
        "checkout_state": "found",
        "index": {"last_scanned_at": _iso(scanned_at), "source_changed_since_scan": None},
        "session": (
            {
                "session_id": "9b17795f-17a0-4f2a-8b8f-5cdb974fb59c",
                "adapter": "claude_code",
                "task": "receipts lane: the freshness law",
                "total_tokens": 18400,
                "started_at": _iso(NOW - timedelta(hours=2)),
            }
            if session
            else None
        ),
        "next": {"blocker": None, "step": "write the chip"},
        "gaps": [] if session else ["no_session_for_repo"],
    }


SUSPECT_FIXTURE = {
    "repo": REPO,
    "repo_identity": "unfoundbox-crew/pet-talk",
    "range": "origin/main..HEAD",
    "window_hours": 24,
    "commits_scanned": 3,
    "attributed": 1,
    "unattributed": 0,
    # SuspectCommit / SuspectSession, as archie 0.1.23 actually emits them
    # (apps/cli/src/commands/suspect.rs). The older shape in
    # docs/specs/suspect-commits.md is not what ships.
    "suspect": [
        {
            "sha": "d01eaae9c1f04b6e2a7b8c3d5e6f70819a2b3c4d",
            "short_sha": "d01eaae",
            "subject": "fix(providers): default to groq when GROQ_API_KEY is present",
            "committed_at": _iso(NOW - timedelta(hours=6)),
            "sessions": [
                {
                    "session_id": "7a02",
                    "adapter": "codex",
                    "model": "gpt-5-codex",
                    "outcome": None,
                    "reasons": [{"code": "no_test_run", "detail": "no test command in this session"}],
                    "evidence_path": "server/speak_queue.py",
                    "occurred_at": _iso(NOW - timedelta(hours=7)),
                    "risk_unknown": False,
                }
            ],
        }
    ],
    "prompt": "1 of 3 commits on this branch proved nothing.",
    "receipt": {
        "counted_at": _iso(NOW),
        "index_last_session_at": _iso(SCAN_FRESH),
        "db_path": "/Users/x/.agentworth/agentworth.db",
    },
}

BLAME_FIXTURE = [
    {
        "session_id": "7a02",
        "adapter": "codex",
        "started_at": _iso(NOW - timedelta(days=1)),
        "models_used": ["gpt-5-codex"],
        "total_tokens": 18400,
        "file_path": "server/speak_queue.py",
        "action": "write",
        "modified_at": _iso(NOW - timedelta(days=1)),
        "model": "gpt-5-codex",
    }
]

SESSION_LIST_FIXTURE = [
    {
        "session_id": "7a02",
        "adapter": "codex",
        "models_used": ["gpt-5-codex"],
        "total_tokens": 18400,
        "started_at": _iso(NOW - timedelta(hours=5)),
        "primary_outcome": "commit_observed",
        "source_path": "/Users/x/.codex/sessions/7a02.jsonl",
    }
]


def fake_archie(
    scanned_at: datetime,
    *,
    session: bool = True,
    fail: bool = False,
    delay_s: float = 0.0,
):
    """A runner standing in for the `archie` binary. Returns stdout JSON.

    ``delay_s`` blocks the calling thread, which is how the "a slow receipt
    must not stall the loop" test stands in for a real `archie` that takes a
    second and a half to answer.
    """

    def runner(args: list[str], cwd: str) -> str:
        if delay_s:
            time.sleep(delay_s)
        if fail:
            raise ProviderError("receipts_unavailable", "archie not on PATH (fixture)")
        joined = " ".join(args)
        if "session wake" in joined:
            return json.dumps(wake_fixture(scanned_at, session=session))
        if "repo suspect" in joined:
            return json.dumps(SUSPECT_FIXTURE)
        if "repo blame" in joined:
            return json.dumps(BLAME_FIXTURE)
        if "session list" in joined:
            return json.dumps(SESSION_LIST_FIXTURE)
        raise ProviderError("receipts_unavailable", f"unfixtured archie call: {joined}")

    return runner


class ReceiptsTestCase(unittest.TestCase):
    """Shared teardown: never leave an injected runner behind."""

    def tearDown(self) -> None:
        receipts_archie.set_runner(None)
        receipts_archie.set_clock(None)
        receipts_archie.set_head_reader(None)

    def arrange(self, scanned_at: datetime, **kw) -> None:
        receipts_archie.set_runner(fake_archie(scanned_at, **kw))
        receipts_archie.set_clock(lambda: NOW)
        receipts_archie.set_head_reader(lambda repo: HEAD_AT)


# ---------------------------------------------------------------------------
# 1. The classifier over the spoken sentence
# ---------------------------------------------------------------------------


class TestClaimClassifier(ReceiptsTestCase):
    def test_work_claims_are_detected_with_their_kind(self):
        cases = [
            ("The tests are green.", "tests"),
            ("I ran the suite and it passed.", "tests"),
            ("The commit landed on the branch.", "commit"),
            ("I pushed that change.", "commit"),
            ("I changed the speak queue file.", "file"),
            ("A Codex session wrote it, Tuesday.", "blame"),
            ("Who broke the speak queue?", "blame"),
            ("Where was I with the receipts lane?", "session"),
        ]
        for text, kind in cases:
            with self.subTest(text=text):
                claim = receipts.classify_claim(text)
                self.assertIsNotNone(claim, f"{text!r} is a claim about work and was not classified")
                self.assertEqual(claim.kind, kind, f"{text!r} classified as {claim.kind}, expected {kind}")

    def test_ordinary_conversation_is_not_a_work_claim(self):
        for text in [
            "All systems online and ready.",
            "I'm Archie. What do you need?",
            "The weather looks fine today.",
            "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(
                    receipts.classify_claim(text),
                    f"{text!r} is not a claim about work but was classified as one",
                )

    def test_a_persona_hint_does_not_invent_a_claim(self):
        self.assertIsNone(
            receipts.classify_claim("Good morning.", persona="archie"),
            "the persona hint turned a greeting into a work claim",
        )


# ---------------------------------------------------------------------------
# 2. attach(): every work claim carries a receipt, or is refused by name
# ---------------------------------------------------------------------------


class TestAttach(ReceiptsTestCase):
    def test_a_receipt_frame_carries_every_contract_field(self):
        self.arrange(SCAN_FRESH)
        f = receipts.attach(TURN, "A Codex session wrote it, Tuesday.", repo=REPO)
        self.assertIsNotNone(f, "a work claim produced no frame at all")
        self.assertEqual(f["type"], "agent.receipt")
        self.assertEqual(f["turn_id"], TURN)
        for key in ("claim", "source", "tokens", "cost_usd", "index_age_s", "fresh"):
            self.assertIn(key, f, f"the receipt frame is missing {key!r}")
        for key in ("kind", "id", "repo", "path"):
            self.assertIn(key, f["source"], f"the receipt source is missing {key!r}")
        self.assertIn(f["source"]["kind"], ("session", "commit", "scan"))
        self.assertIsInstance(f["tokens"], int)
        self.assertTrue(f["fresh"], "a scan newer than HEAD reported fresh=false")
        self.assertIsInstance(f["index_age_s"], int)

    def test_a_non_claim_produces_no_frame(self):
        self.arrange(SCAN_FRESH)
        self.assertIsNone(
            receipts.attach(TURN, "All systems online and ready.", repo=REPO),
            "an ordinary sentence produced a receipt frame",
        )

    def test_a_work_claim_with_no_receipt_available_is_refused_by_name(self):
        self.arrange(SCAN_FRESH, fail=True)
        f = receipts.attach(TURN, "The tests are green.", repo=REPO)
        self.assertIsNotNone(f, "an unbacked work claim produced no frame, so nothing refused it")
        self.assertEqual(f["type"], "agent.error")
        self.assertEqual(f["reason"], "receipts_unavailable")
        self.assertNotEqual(f["type"], "agent.receipt")

    def test_a_claim_with_an_empty_receipt_set_is_refused_by_name(self):
        self.arrange(SCAN_FRESH, session=False)
        f = receipts.attach(TURN, "Where was I with the receipts lane?", repo=REPO)
        self.assertEqual(f["type"], "agent.error")
        self.assertEqual(f["reason"], "receipt_missing")

    def test_a_blame_claim_on_a_stale_index_never_reaches_the_wire(self):
        self.arrange(SCAN_STALE)
        f = receipts.attach(TURN, "A Codex session wrote it, Tuesday.", repo=REPO)
        self.assertEqual(f["type"], "agent.error", "a stale index still emitted a receipt")
        self.assertEqual(f["reason"], "receipt_stale_index")
        self.assertIn("behind the last commit", f["spoken"])


# ---------------------------------------------------------------------------
# 3. The freshness law and its spoken refusal
# ---------------------------------------------------------------------------


class TestArgvInjection(ReceiptsTestCase):
    """A spoken sentence becomes argv. It may not become a flag or a path walk.

    `repo blame <hint>` built argv with no `--`, and `path_hint` happily
    matched `--exec=id.py` and `../../../etc/passwd.py` — a dictated or
    attacker-supplied sentence reaching a subprocess's option parser.
    """

    def test_a_hint_that_looks_like_an_option_is_refused(self):
        self.assertEqual(receipts.path_hint("who broke --json.py"), "")
        self.assertEqual(receipts.path_hint("who broke -rf.py"), "")
        # `--exec=id.py` leaves only `id.py` in the match — a safe hint, and
        # the `--` separator covers what the regex cannot see.
        self.assertEqual(receipts.path_hint("who broke --exec=id.py"), "id.py")

    def test_a_hint_that_walks_out_of_the_repo_is_refused(self):
        self.assertEqual(receipts.path_hint("who broke ../../../etc/passwd.py"), "")
        self.assertEqual(receipts.path_hint("who broke ../secrets.json"), "")

    def test_an_ordinary_hint_still_resolves(self):
        self.assertEqual(receipts.path_hint("who broke server/speech.py"), "server/speech.py")
        self.assertEqual(receipts.path_hint("who broke the speak queue"), "who_broke_speak")

    def test_safe_positional_names_each_refusal(self):
        for bad in ("--json", "-x", "a/../b", "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ProviderError) as caught:
                    receipts_archie.safe_positional(bad)
                self.assertEqual(caught.exception.reason, receipts_archie.BAD_HINT)

    def test_repo_blame_puts_the_path_after_a_double_dash(self):
        seen: list[list[str]] = []

        def runner(args: list[str], cwd: str) -> str:
            seen.append(list(args))
            return json.dumps(BLAME_FIXTURE)

        receipts_archie.set_runner(runner)
        receipts_archie.repo_blame("server/speech.py", REPO)
        self.assertTrue(seen, "repo blame never ran")
        args = seen[0]
        self.assertIn("--", args, f"no argv separator: {args}")
        self.assertEqual(
            args[args.index("--") + 1:],
            ["server/speech.py"],
            f"the path is not the only thing after --: {args}",
        )
        self.assertLess(args.index("--"), len(args) - 1)

    def test_repo_blame_refuses_an_option_shaped_path_without_running(self):
        ran = []
        receipts_archie.set_runner(lambda args, cwd: ran.append(args) or "[]")
        with self.assertRaises(ProviderError) as caught:
            receipts_archie.repo_blame("--version", REPO)
        self.assertEqual(caught.exception.reason, receipts_archie.BAD_HINT)
        self.assertEqual(ran, [], "archie was run with an option-shaped path")


class TestFreshnessLaw(ReceiptsTestCase):
    def test_a_scan_older_than_head_is_not_fresh(self):
        self.arrange(SCAN_STALE)
        state = receipts_archie.index_state(REPO)
        self.assertFalse(state.fresh, "a scan older than HEAD reported fresh=true")
        self.assertEqual(state.behind_s, 11 * 60)

    def test_a_scan_newer_than_head_is_fresh(self):
        self.arrange(SCAN_FRESH)
        state = receipts_archie.index_state(REPO)
        self.assertTrue(state.fresh)
        self.assertEqual(state.behind_s, 0)
        self.assertEqual(state.index_age_s, 7 * 60)

    def test_the_refusal_line_obeys_the_noun_rule(self):
        line = receipts.stale_index_line(11 * 60)
        self.assertEqual(line, "My index is 11 minutes behind the last commit. Rescan?")
        words = line.split()
        self.assertLessEqual(len(words), 45, f"the refusal line is {len(words)} words, ceiling is 45")
        self.assertTrue(line[0].isupper(), "the refusal line has no subject at the front")
        self.assertNotIn("_", line, "the refusal line leaks an engineering identifier")

    def test_one_minute_behind_is_singular(self):
        self.assertEqual(
            receipts.stale_index_line(60), "My index is 1 minute behind the last commit. Rescan?"
        )


# ---------------------------------------------------------------------------
# 4. The two voice routes
# ---------------------------------------------------------------------------


class TestVoiceRoutes(ReceiptsTestCase):
    def test_who_broke_answers_from_suspect_commits_with_a_receipt(self):
        self.arrange(SCAN_FRESH)
        answer = receipts.who_broke("the speak queue", repo=REPO)
        self.assertIsNone(answer.refused, f"the route refused with {answer.refused}")
        self.assertTrue(answer.spoken, "the route answered with nothing")
        # An adapter key is an identifier, not a name a person says out loud.
        self.assertNotIn("claude_code", answer.spoken)
        self.assertIn("Codex", answer.spoken, "the adapter was not named in a way a person says")
        self.assertLessEqual(len(answer.spoken.split()), 45)
        self.assertIsNotNone(answer.receipt, "the route answered without a receipt")
        self.assertEqual(answer.receipt["source"]["kind"], "commit")
        self.assertEqual(answer.receipt["source"]["id"], "d01eaae")
        self.assertTrue(answer.receipt["fresh"])

    def test_who_broke_is_refused_on_a_stale_index_and_says_so(self):
        self.arrange(SCAN_STALE)
        answer = receipts.who_broke("the speak queue", repo=REPO)
        self.assertEqual(answer.refused, "receipt_stale_index")
        self.assertIsNone(answer.receipt, "a refused route still produced a receipt")
        self.assertEqual(answer.spoken, "My index is 11 minutes behind the last commit. Rescan?")

    def test_where_was_i_answers_from_carry_forward_with_a_receipt(self):
        self.arrange(SCAN_FRESH)
        answer = receipts.where_was_i("the receipts lane", repo=REPO)
        self.assertIsNone(answer.refused, f"the route refused with {answer.refused}")
        self.assertIsNotNone(answer.receipt, "the route answered without a receipt")
        self.assertEqual(answer.receipt["source"]["kind"], "session")
        self.assertEqual(answer.receipt["tokens"], 18400)
        self.assertLessEqual(len(answer.spoken.split()), 45)

    def test_where_was_i_survives_a_stale_index_because_it_assigns_no_blame(self):
        self.arrange(SCAN_STALE)
        answer = receipts.where_was_i("the receipts lane", repo=REPO)
        self.assertIsNone(answer.refused, "a session recall was refused for staleness it does not need")
        self.assertFalse(answer.receipt["fresh"], "a stale index reported fresh=true on a session recall")

    def test_a_route_with_no_archie_at_all_is_refused_by_name(self):
        self.arrange(SCAN_FRESH, fail=True)
        answer = receipts.who_broke("the speak queue", repo=REPO)
        self.assertEqual(answer.refused, "receipts_unavailable")
        self.assertTrue(answer.spoken, "an unavailable index refused silently")


# ---------------------------------------------------------------------------
# 5. Frames fail closed
# ---------------------------------------------------------------------------


class TestNoAbsolutePathsOnTheWire(ReceiptsTestCase):
    """Law 3: a frame never carries an absolute local path.

    `agent.receipt.source.path` forwarded archie's `file_path` verbatim, and
    `agent.error.detail` forwarded archie's stderr verbatim — both of which name
    `/Users/<name>`, i.e. the machine's owner, to anyone holding the socket.
    """

    def test_the_helper_replaces_a_home_directory_with_a_tilde(self):
        from server.logs import redact_home

        self.assertEqual(
            redact_home("archie exit 1: cannot open /Users/saurabh/code/x/y.py"),
            "archie exit 1: cannot open ~/code/x/y.py",
        )
        self.assertEqual(redact_home("/home/bob/.codex/s.jsonl"), "~/.codex/s.jsonl")
        self.assertEqual(redact_home("server/speech.py"), "server/speech.py")
        self.assertEqual(redact_home(None), "")

    def test_an_absolute_blame_path_becomes_repo_relative(self):
        self.assertEqual(
            receipts._short_path(f"{REPO}/server/speech.py", REPO), "server/speech.py"
        )

    def test_an_unanchorable_absolute_path_falls_back_to_the_basename(self):
        short = receipts._short_path("/Users/someone/elsewhere/secret_plan.py", REPO)
        self.assertEqual(short, "secret_plan.py")
        self.assertNotIn("/Users/", short)

    def test_a_receipt_frame_carries_no_absolute_path(self):
        absolute = dict(BLAME_FIXTURE[0])
        absolute["file_path"] = f"{REPO}/server/speak_queue.py"

        def runner(args: list[str], cwd: str) -> str:
            joined = " ".join(args)
            if "session wake" in joined:
                return json.dumps(wake_fixture(SCAN_FRESH))
            if "repo suspect" in joined:
                return json.dumps(SUSPECT_FIXTURE)
            if "repo blame" in joined:
                return json.dumps([absolute])
            raise ProviderError("receipts_unavailable", joined)

        receipts_archie.set_runner(runner)
        receipts_archie.set_clock(lambda: NOW)
        receipts_archie.set_head_reader(lambda repo: HEAD_AT)
        f = receipts.attach(TURN, "A Codex session wrote server/speak_queue.py.", repo=REPO)
        self.assertEqual(f["type"], "agent.receipt")
        self.assertEqual(f["source"]["path"], "server/speak_queue.py")
        self.assertNotIn("/Users/", json.dumps(f))
        self.assertFalse(
            f["source"]["path"].startswith("/"), f"absolute path on the wire: {f['source']}"
        )

    def test_an_error_detail_never_carries_a_username(self):
        from server.frames import error_frame

        f = error_frame(
            TURN,
            "receipts_unavailable",
            "archie exit 2: no such file /Users/saurabh/code/pet-talk/server/ws.py",
        )
        self.assertNotIn("/Users/", f["detail"])
        self.assertIn("~/code/pet-talk/server/ws.py", f["detail"])

    def test_a_refused_receipt_carries_a_redacted_detail(self):
        def runner(args: list[str], cwd: str) -> str:
            raise ProviderError(
                "receipts_unavailable", "archie died reading /Users/saurabh/.codex/x.jsonl"
            )

        receipts_archie.set_runner(runner)
        receipts_archie.set_clock(lambda: NOW)
        receipts_archie.set_head_reader(lambda repo: HEAD_AT)
        f = receipts.attach(TURN, "The tests are green.", repo=REPO)
        self.assertEqual(f["type"], "agent.error")
        self.assertNotIn("/Users/", json.dumps(f))


class TestFrameDiscipline(ReceiptsTestCase):
    def test_a_receipt_without_a_turn_id_fails_closed(self):
        self.arrange(SCAN_FRESH)
        with self.assertRaises(ProviderError) as ctx:
            receipts.attach("", "The tests are green.", repo=REPO)
        self.assertEqual(ctx.exception.reason, "frame_no_turn_id")

    def test_the_receipt_frame_carries_no_engineering_identifier_in_spoken_copy(self):
        self.arrange(SCAN_STALE)
        f = receipts.attach(TURN, "A Codex session wrote it, Tuesday.", repo=REPO)
        self.assertNotIn("agent.receipt", f["spoken"])
        self.assertNotIn("turn_id", f["spoken"])


class TestTurnHook(ReceiptsTestCase):
    """`receipts.emit` is the one-line hook turn.py gets. It never raises."""

    def test_emit_sends_one_frame_per_claim_and_skips_chat(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from starlette.websockets import WebSocketState

        self.arrange(SCAN_FRESH)
        ws = MagicMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.send_json = AsyncMock()
        spoken = ["All systems online and ready.", "A Codex session wrote it, Tuesday."]
        sent = asyncio.run(receipts.emit(ws, TURN, spoken))
        self.assertEqual(sent, 1, "emit proved the wrong number of sentences")
        frames = [c.args[0] for c in ws.send_json.await_args_list]
        self.assertEqual([f["type"] for f in frames], ["agent.receipt"])

    def test_emit_never_raises_on_a_bad_turn_id(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        self.arrange(SCAN_FRESH)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        self.assertEqual(asyncio.run(receipts.emit(ws, "", ["The tests are green."])), 0)

    def test_a_slow_receipt_never_blocks_the_event_loop(self):
        """The reviewer's probe: `attach` ran `subprocess.run(timeout=8)` on the
        loop, so a 1.5 s archie froze everything — 0 heartbeat ticks during it.
        Off-thread, the same 1.5 s must let the loop keep ticking."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from starlette.websockets import WebSocketState

        self.arrange(SCAN_FRESH, delay_s=1.5)
        os.environ["PET_TALK_RECEIPT_TIMEOUT_S"] = "5.0"
        ws = MagicMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.send_json = AsyncMock()

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.1)
                ticks += 1

        async def run() -> int:
            beat = asyncio.create_task(heartbeat())
            try:
                return await receipts.emit(ws, TURN, ["The tests are green."])
            finally:
                beat.cancel()

        try:
            sent = asyncio.run(run())
        finally:
            os.environ.pop("PET_TALK_RECEIPT_TIMEOUT_S", None)
        self.assertEqual(sent, 1, "the slow receipt should still have landed")
        self.assertGreaterEqual(
            ticks, 10, f"loop was blocked: only {ticks} heartbeat ticks in ~1.5s"
        )

    def test_a_receipt_over_its_timeout_is_named_and_skipped(self):
        """A receipt may never delay `agent.done` past the configured timeout."""
        import asyncio
        import time as _time
        from unittest.mock import AsyncMock, MagicMock

        from starlette.websockets import WebSocketState

        self.arrange(SCAN_FRESH, delay_s=1.5)
        os.environ["PET_TALK_RECEIPT_TIMEOUT_S"] = "0.2"
        ws = MagicMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.send_json = AsyncMock()
        async def run() -> tuple[int, float]:
            # Measured INSIDE the loop: what matters is when `emit` returns to
            # the turn, not when asyncio.run finally reaps the orphaned thread.
            started = _time.monotonic()
            sent = await receipts.emit(ws, TURN, ["The tests are green."])
            return sent, _time.monotonic() - started

        try:
            sent, elapsed = asyncio.run(run())
        finally:
            os.environ.pop("PET_TALK_RECEIPT_TIMEOUT_S", None)
        self.assertEqual(sent, 0, "a timed-out receipt must send no frame")
        self.assertLess(elapsed, 1.0, f"emit waited {elapsed:.2f}s past its 0.2s timeout")

    def test_the_receipt_timeout_default_is_two_seconds(self):
        os.environ.pop("PET_TALK_RECEIPT_TIMEOUT_S", None)
        self.assertEqual(receipts.receipt_timeout_s(), 2.0)
        os.environ["PET_TALK_RECEIPT_TIMEOUT_S"] = "not-a-number"
        try:
            self.assertEqual(receipts.receipt_timeout_s(), 2.0)
        finally:
            os.environ.pop("PET_TALK_RECEIPT_TIMEOUT_S", None)

    def test_emit_is_off_when_receipts_are_disabled(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        self.arrange(SCAN_FRESH)
        os.environ["RECEIPTS_ENABLED"] = "0"
        try:
            ws = MagicMock()
            ws.send_json = AsyncMock()
            self.assertEqual(asyncio.run(receipts.emit(ws, TURN, ["The tests are green."])), 0)
        finally:
            os.environ.pop("RECEIPTS_ENABLED", None)


# ---------------------------------------------------------------------------
# 6. The cockpit chip: shape, the single accent, and Archie's absence
#
# Read as text on purpose. These are rules about a surface, and a rule that
# lives only in a comment is a rule that gets edited away.
# ---------------------------------------------------------------------------

CHIP_TSX = os.path.join(ROOT, "web", "src", "components", "ReceiptChip.tsx")
APP_TSX = os.path.join(ROOT, "web", "src", "App.tsx")


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def strip_comments(source: str) -> str:
    """Code only. A comment naming a rule is not a breach of it."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


class TestReceiptChipSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(CHIP_TSX, encoding="utf-8") as fh:
            cls.chip = fh.read()
        with open(APP_TSX, encoding="utf-8") as fh:
            cls.app = fh.read()
        cls.chip_code = strip_comments(cls.chip)

    def test_the_receipt_never_renders_the_archie_mascot(self):
        """The receipt is one of Archie's three forbidden places.

        AgentWorth docs/DESIGN.md: "Where he never goes: the landing hero,
        the receipt, --json or --quiet output. He is a state, not a texture."
        """
        for forbidden in ("ArchieGlyph", "<Archie", "ArchieMascot", "mascot", "hound"):
            self.assertNotIn(
                forbidden,
                self.chip_code,
                f"the receipt chip references {forbidden!r}; the receipt is a forbidden place",
            )
        self.assertNotIn("from \"./Archie", self.chip_code)

    def test_the_chip_geometry_is_the_approved_shape(self):
        for token in ("var(--font-mono)", "9.5px", "1px solid", "3px 7px", 'borderRadius: "3px"'):
            self.assertIn(token, self.chip_code, f"the chip lost its approved {token!r}")

    def test_the_chip_hand_rolls_no_colour(self):
        """Every colour is a token. Lane 4's design-token gate enforces this
        repo-wide; asserting it here too means this lane cannot break it."""
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", self.chip_code)
        self.assertEqual(hexes, [], f"the chip hand-rolled {hexes}; use a CSS token")
        self.assertIn("var(--pt-listening-signal)", self.chip_code)
        self.assertIn("var(--mv-danger)", self.chip_code, "staleness has no danger token")

    def test_the_accent_appears_once_and_only_on_the_total_line(self):
        """One violet per receipt, on the number that sums it up."""
        self.assertEqual(
            self.chip_code.count("var(--mv-accent)"),
            1,
            "the accent is referenced more than once; it carries the total line and nothing else",
        )
        total_block = self.chip_code.split('data-testid="receipt-total"', 1)
        self.assertEqual(len(total_block), 2, "the chip has no total line to carry the accent")
        self.assertIn("T.accent", total_block[1][:400], "the total line is not the accent's line")
        before_total = total_block[0]
        self.assertNotIn("T.accent", before_total, "the accent is used before the total line")

    def test_the_chip_carries_source_tokens_and_index_age(self):
        for probe in ("source.id", "source.path", "formatTokens(tokens)", "formatIndexAge"):
            self.assertIn(probe, self.chip, f"the chip does not show {probe}")
        self.assertIn("data-receipt-fresh", self.chip, "the chip does not expose index freshness")

    def test_clicking_the_source_chip_copies_the_session_id(self):
        self.assertIn("clipboard", self.chip, "the source chip does not copy anything")
        self.assertIn("writeText", self.chip)
        self.assertIn("void write(source.id)", self.chip, "the chip copies something other than the id")

    def test_the_app_renders_the_chip_under_the_spoken_line(self):
        self.assertIn('from "./components/ReceiptChip"', self.app, "App.tsx never imports the chip")
        self.assertIn("<ReceiptChip receipt={line.receipt} />", self.app)
        self.assertIn("asReceiptFrame(frame)", self.app, "App.tsx never narrows an agent.receipt frame")


# ---------------------------------------------------------------------------
# 7. Real engine: the live index on this machine
# ---------------------------------------------------------------------------


class TestRealArchieIndex(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("PET_TALK_REAL_ENGINE") == "1",
        "SKIP real archie index: PET_TALK_REAL_ENGINE != 1",
    )
    def test_the_live_index_reports_its_age_and_freshness(self):
        receipts_archie.set_runner(None)
        receipts_archie.set_clock(None)
        receipts_archie.set_head_reader(None)
        try:
            state = receipts_archie.index_state(ROOT)
        except ProviderError as e:
            self.skipTest(f"SKIP real archie index: {e.reason} ({e.detail})")
        print(f"    [RECEIPT] archie index age: {state.index_age_s}s ({state.index_age_s // 60}m)")
        print(f"    [RECEIPT] last scanned: {state.last_scanned_at}")
        print(f"    [RECEIPT] repo HEAD:     {state.head_committed_at}")
        print(f"    [RECEIPT] fresh: {state.fresh}  behind: {state.behind_s}s")
        self.assertIsInstance(state.index_age_s, int)
        self.assertGreaterEqual(state.index_age_s, 0)
        if not state.fresh:
            line = receipts.stale_index_line(state.behind_s)
            print(f"    [RECEIPT] refusal line: {line}")
            self.assertIn("behind the last commit", line)

    @unittest.skipUnless(
        os.environ.get("PET_TALK_REAL_ENGINE") == "1",
        "SKIP real archie routes: PET_TALK_REAL_ENGINE != 1",
    )
    def test_the_who_broke_route_answers_from_the_live_index(self):
        receipts_archie.set_runner(None)
        receipts_archie.set_clock(None)
        receipts_archie.set_head_reader(None)
        answer = receipts.answer_voice_route("who broke speak_queue.py", repo=ROOT)
        self.assertIsNotNone(answer, "the route did not match a live question")
        print(f"    [RECEIPT] spoken:  {answer.spoken}")
        print(f"    [RECEIPT] refused: {answer.refused}")
        print(f"    [RECEIPT] receipt: {answer.receipt}")
        self.assertTrue(answer.spoken, "the live route said nothing at all")
        self.assertLessEqual(len(answer.spoken.split()), 45)
        if answer.refused is None:
            self.assertIsNotNone(answer.receipt, "a live answer carried no receipt")
            for key in ("source", "tokens", "index_age_s", "fresh"):
                self.assertIn(key, answer.receipt)
        else:
            self.assertIn(answer.refused, receipts.REASONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
