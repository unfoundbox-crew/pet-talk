#!/usr/bin/env python3
"""qa/test_hotkey.py — QA suite for the native macOS hotkey listener.

Fan rule: this file never runs `swiftc -O` to build a binary — heavy builds
happen on `ssh air` (see cli/hotkey/build.sh, `make build-hotkey`). Tests
that need the compiled listener require a prebuilt binary at
$PET_TALK_HOTKEY_BIN (default: bin/pet-talk-hotkey) and SKIP with a clear
reason when it is absent.

Silent/safety rule: this file never starts the real daemon, never plays
audio through afplay, and never SIGKILLs a real playback process — those
are exactly the things a QA gate must not do to a developer's machine.
Registration is a single stateless subprocess call (`--check-registration`),
which is safe to run; daemon lifecycle and barge/afplay behavior are
verified manually, not by this automated suite.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT_SRCS = sorted(glob.glob(os.path.join(ROOT, "cli", "hotkey", "*.swift")))
BIN_PATH = os.environ.get("PET_TALK_HOTKEY_BIN") or os.path.join(ROOT, "bin", "pet-talk-hotkey")
HAVE_BIN = os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK)


def _skip_if_no_bin():
    if not HAVE_BIN:
        raise unittest.SkipTest(
            "SKIP: no prebuilt hotkey binary at %s — build it on `ssh air` via "
            "`cli/hotkey/build.sh bin/` (or `make build-hotkey`), never here "
            "(fan rule); set PET_TALK_HOTKEY_BIN to point at it" % BIN_PATH
        )


class TestSwiftTypecheck(unittest.TestCase):
    """Cheap syntax/type check only — never a full `-O` build (fan rule)."""

    def test_typecheck_only(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise unittest.SkipTest("SKIP: swiftc not available on this host")
        if not SWIFT_SRCS:
            raise unittest.SkipTest("SKIP: no cli/hotkey/*.swift sources present yet")
        res = subprocess.run(["swiftc", "-typecheck"] + SWIFT_SRCS,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0,
                         f"swiftc -typecheck failed:\nStdout: {res.stdout}\nStderr: {res.stderr}")


class TestCarbonHotkeyRegistration(unittest.TestCase):
    """Stateless registration check — spawns and exits, no persistent daemon."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def test_keycode_and_modifier_registration(self):
        res = subprocess.run(
            [BIN_PATH, "--check-registration"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"Registration failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("48 (kVK_Tab)", res.stdout)
        self.assertIn("0x800", res.stdout)
        self.assertIn("PASS: Carbon RegisterEventHotKey succeeded", res.stdout)


class TestBargeInLatency(unittest.TestCase):
    """Disabled by design: a QA test must never start real afplay playback
    or SIGKILL it (night rule, COMMON.md). Verify manually with:
      afplay bake-deepgram_0.wav &
      bin/pet-talk-hotkey --barge-benchmark
    """

    def test_afplay_kill_under_50ms(self):
        raise unittest.SkipTest(
            "SKIP: disabled — this would start real afplay playback and "
            "SIGKILL it, which a QA test must never do; verify manually "
            "via `bin/pet-talk-hotkey --barge-benchmark` against a playing "
            "bake-deepgram_0.wav"
        )


class TestDaemonLifecycle(unittest.TestCase):
    """Disabled by design: a QA test must never start the real hotkey daemon."""

    def test_lifecycle_transitions(self):
        raise unittest.SkipTest(
            "SKIP: disabled — start/stop of the real daemon is not exercised "
            "by the automated QA gate; verify manually with "
            "`bin/pet-talk-hotkey start|status|stop`"
        )


class TestKillSwitchAndPause(unittest.TestCase):
    """Disabled by design: kill/pause/resume/toggle all start the real daemon."""

    def test_kill_switch_command(self):
        raise unittest.SkipTest(
            "SKIP: disabled — exercises the real daemon; verify manually "
            "with `bin/pet-talk-hotkey kill`"
        )

    def test_pause_resume_toggle_lifecycle(self):
        raise unittest.SkipTest(
            "SKIP: disabled — exercises the real daemon; verify manually "
            "with `bin/pet-talk-hotkey start|pause|resume|toggle|status`"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
