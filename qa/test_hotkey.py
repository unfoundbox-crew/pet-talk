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


# Headless: PET_TALK_HEADLESS=1 keeps every panel off screen while the state
# machine, springs and geometry still run (Saurabh, 2026-09-12).
HEADLESS_ENV = dict(os.environ, PET_TALK_HEADLESS="1", PET_TALK_SILENT="1")


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
            env=HEADLESS_ENV,
        )
        self.assertEqual(res.returncode, 0, f"Registration failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("48 (kVK_Tab)", res.stdout)
        self.assertIn("0x800", res.stdout)
        self.assertIn("PASS: Carbon RegisterEventHotKey succeeded", res.stdout)


class TestChordMapping(unittest.TestCase):
    """Option+Shift+Tab is hand-over; pause is reachable ONLY by a double-tap of
    Option+Tab inside 400 ms. Static source checks — no daemon, no audio."""

    MAIN_SRC = os.path.join(ROOT, "cli", "hotkey", "main.swift")

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(cls.MAIN_SRC):
            raise unittest.SkipTest(f"SKIP: {cls.MAIN_SRC} not present yet")
        with open(cls.MAIN_SRC, "r", encoding="utf-8") as f:
            cls.src = f.read()

    def test_option_shift_tab_is_handover_not_pause(self):
        self.assertTrue("handoverHotKeyModifier" in self.src,
                        "Option+Shift+Tab must be declared as the hand-over chord")
        self.assertTrue("UInt32(optionKey | shiftKey)" in self.src, "missing: UInt32(optionKey | shiftKey)")
        leftovers = [ln.strip() for ln in self.src.splitlines() if "pauseHotKeyModifier" in ln]
        self.assertEqual(leftovers, [], "Option+Shift+Tab must no longer be a pause chord")
        self.assertTrue("handleHandoverHotKeyTrigger" in self.src, "missing: handleHandoverHotKeyTrigger")

    def test_handover_trigger_emits_a_handover_event_and_never_pauses(self):
        idx = self.src.find("func handleHandoverHotKeyTrigger")
        self.assertNotEqual(idx, -1)
        block = self.src[idx:idx + 600]
        self.assertNotIn("togglePause", block,
                         "the hand-over chord must never toggle pause")
        self.assertIn("emitHandover", block)
        emit_idx = self.src.find("func emitHandover")
        self.assertNotEqual(emit_idx, -1, "a named hand-over emitter must exist")
        emit = self.src[emit_idx:emit_idx + 1800]
        self.assertIn('"handover"', emit,
                      "hand-over must be emitted to pet-talk-cli the way a wake turn is")

    def test_pause_is_reachable_only_from_the_double_tap_path(self):
        import re
        callers = []
        for m in re.finditer(r"togglePause\(\)", self.src):
            # find the enclosing `func <name>` above this call
            head = self.src[:m.start()]
            fidx = head.rfind("func ")
            name = self.src[fidx:fidx + 80].split("(")[0].replace("func ", "").strip()
            callers.append(name)
        allowed = {"handleHotKeyTrigger", "togglePause", "startListening", "doToggle"}
        for name in callers:
            self.assertIn(name, allowed,
                          f"togglePause() called from unexpected {name}() — pause must be "
                          "reachable only via the Option+Tab double-tap (and the explicit "
                          "pause/resume/toggle subcommands and SIGUSR1)")
        self.assertIn("handleHotKeyTrigger", callers,
                      "the double-tap path must still reach togglePause()")

    def test_double_tap_window_is_400ms_from_a_token(self):
        idx = self.src.find("func handleHotKeyTrigger")
        block = self.src[idx:idx + 900]
        self.assertIn("HUDTokens.doubleTapWindowMs", block,
                      "the double-tap window must read the 400ms token, not a literal")
        leftovers = [ln.strip() for ln in self.src.splitlines() if "350.0" in ln]
        self.assertEqual(leftovers, [], "the old 350ms double-tap window must be gone")

    def test_help_text_names_the_new_gestures(self):
        self.assertTrue("Hand-over" in self.src, "missing: Hand-over")
        self.assertTrue("Double-tap Option+Tab" in self.src, "missing: Double-tap Option+Tab")
        leftovers = [ln.strip() for ln in self.src.splitlines()
                     if "Option+Shift+Tab or double-tap to wake" in ln]
        self.assertEqual(leftovers, [],
                         "paused breadcrumb must no longer advertise Option+Shift+Tab as wake")


class TestDumpStateChords(unittest.TestCase):
    """`--dump-state` reports the live chord map: one JSON line, no daemon."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        res = subprocess.run([BIN_PATH, "--dump-state"], capture_output=True,
                             text=True, timeout=15, env=HEADLESS_ENV)
        # A present binary whose --dump-state fails is a real failure, never a skip.
        if res.returncode != 0:
            raise AssertionError(f"--dump-state failed (rc={res.returncode}):\n{res.stderr}\n{res.stdout}")
        import json
        cls.state = json.loads(res.stdout)

    def test_chords(self):
        c = self.state.get("chords", {})
        self.assertEqual(c.get("optionTab"), "ask")
        self.assertEqual(c.get("optionShiftTab"), "handover")
        self.assertEqual(c.get("optionTabDoubleTap"), "pause")
        self.assertEqual(c.get("doubleTapWindowMs"), 400.0)

    def test_handover_event_shape_is_reported(self):
        ev = self.state.get("handoverEvent")
        self.assertIsInstance(ev, dict, "the hand-over event shape must be machine-readable")
        self.assertEqual(ev.get("type"), "user.handover")
        self.assertIn("turn_id", ev.get("fields", []))
        self.assertEqual(ev.get("transport"), "pet-talk-cli handover")


class TestAXForceCastsAreConditional(unittest.TestCase):
    """FINDING 9: `ax` must never crash the daemon when the AX API hands back
    something that isn't an AXUIElement — `focusedRef as! AXUIElement` and
    `windowRef as! AXUIElement` must be conditional (`as?`) casts that emit
    `{"ok":false,"reason":"ax_unavailable"}` on failure instead of trapping.

    A real subprocess run of `ax` is the primary check: on a box without AX
    trust granted to this process (true in CI and in this worktree — verified:
    it returns ax_permission_denied before ever reaching the casts), it proves
    the function still runs to completion and prints valid JSON with no crash.
    Forcing the AX API to hand back a non-AXUIElement type requires driving a
    real front app with AX trust granted, which isn't available headless —
    so the cast sites themselves are also checked directly in source, which the
    finding allows when a runtime trigger for that exact branch is impossible."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        with open(os.path.join(ROOT, "cli", "hotkey", "main.swift"), "r", encoding="utf-8") as f:
            cls.src = f.read()

    def test_ax_subcommand_runs_to_completion_and_emits_valid_json(self):
        res = subprocess.run([BIN_PATH, "ax"], capture_output=True, text=True,
                              timeout=15, env=HEADLESS_ENV)
        self.assertEqual(res.returncode, 0, f"`ax` must exit 0 even on failure "
                          f"(fails closed with a named reason):\n{res.stderr}\n{res.stdout}")
        import json
        lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "`ax` must print exactly one JSON line")
        payload = json.loads(lines[0])
        self.assertIn("ok", payload)
        if payload["ok"] is False:
            self.assertIn("reason", payload, "a failed `ax` call must name its reason")

    def test_no_bare_force_cast_of_focused_or_window_ref(self):
        self.assertNotIn("focusedRef as! AXUIElement", self.src,
                          "focusedRef must not be force-cast")
        self.assertNotIn("windowRef as! AXUIElement", self.src,
                          "windowRef must not be force-cast")

    def test_focused_element_cast_is_conditional_and_fails_closed(self):
        idx = self.src.find("kAXFocusedUIElementAttribute")
        block = self.src[idx:idx + 400]
        self.assertIn("asAXUIElement(focusedRef)", block,
                      "the focused-element cast must go through the CFTypeID-checked helper")
        self.assertIn('\\"reason\\":\\"ax_unavailable\\"', block,
                      "a failed focused-element cast must emit the ax_unavailable reason")

    def test_focused_window_cast_is_conditional_and_fails_closed(self):
        idx = self.src.find("kAXFocusedWindowAttribute")
        block = self.src[idx:idx + 400]
        self.assertIn("asAXUIElement(windowRef)", block,
                      "the focused-window cast must go through the CFTypeID-checked helper")
        self.assertIn('\\"reason\\":\\"ax_unavailable\\"', block,
                      "a failed focused-window cast must emit the ax_unavailable reason")


class TestRegistrationVerifiesBothChords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def test_registration_reports_handover_chord(self):
        res = subprocess.run([BIN_PATH, "--check-registration"],
                             capture_output=True, text=True, timeout=15, env=HEADLESS_ENV)
        self.assertEqual(res.returncode, 0, f"Registration failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("0XA00", res.stdout.upper(),
                      "must report the Option+Shift+Tab (0xA00) hand-over chord")
        self.assertIn("handover", res.stdout.lower())


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
