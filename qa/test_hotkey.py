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

import base64
import glob
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
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
        if sys.platform != "darwin":
            raise unittest.SkipTest(
                "SKIP: not macOS — these sources import AppKit/Carbon, so "
                "swiftc -typecheck fails on any other platform even when "
                "swiftc itself is installed (e.g. Linux CI runners)"
            )
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
        # `350.0` must not be a double-tap window any more. The native engine's
        # VAD threshold is also 350.0 (it mirrors cli/audio.py) and is not a
        # gesture timing, so those lines are named out rather than swallowing
        # the whole check.
        leftovers = [
            ln.strip() for ln in self.src.splitlines()
            if "350.0" in ln and "vad" not in ln.lower() and "cli/audio.py" not in ln
        ]
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
        # The transport follows the turn engine: the native engine emits the
        # frame on its own socket, the cli engine still spawns `pet-talk-cli
        # handover`. Both are legitimate; a third value is not.
        engine = self.state.get("turnEngine", {}).get("engine")
        expected = "native WSClient" if engine == "native" else "pet-talk-cli handover"
        self.assertEqual(ev.get("transport"), expected)


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


class TestServerHealthProbe(unittest.TestCase):
    """Wake-time health probe (main.swift `probeServerHealth`, called from
    `startNewTurn()` before spawning pet-talk-cli): `--self-test` stubs an
    unreachable port (127.0.0.1:1) and asserts the probe fails closed inside
    its own 300ms budget — see qa/test_launchd.py for the server-as-agent
    suite this pairs with."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def test_self_test_covers_the_health_probe(self):
        res = subprocess.run([BIN_PATH, "--self-test"], capture_output=True,
                             text=True, timeout=15, env=HEADLESS_ENV)
        self.assertEqual(res.returncode, 0, f"--self-test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("health probe fails closed on an unreachable port", res.stdout)
        self.assertIn("health probe stays within its timeout budget", res.stdout)
        # Every "PASS:" line for these two checks, never "FAIL:".
        for line in res.stdout.splitlines():
            if "health probe" in line:
                self.assertIn("PASS:", line, f"health probe check did not pass: {line}")


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


class TestCliLauncherIsResolvable(unittest.TestCase):
    """The daemon must be able to find a CLI to spawn.

    The regression this pins down: `bin/pet-talk-cli` was an untracked binary
    and nothing in the tree built it, so after a clean checkout the daemon
    started with "Target CLI: (unresolved)" and Option+Tab was a silent no-op
    on Saurabh's machine. `make build-cli` (which `make build-hotkey` now
    depends on) writes the launcher; `--dump-state` reports what would be
    spawned, so the resolution is testable and not just visible in a log.

    Hermetic: no daemon, no server, no audio. `--dump-state` is one stateless
    subprocess call, and `--help` exits without touching a socket or a mic.
    """

    def test_the_builder_exists_and_is_executable(self):
        builder = os.path.join(ROOT, "cli", "build-cli.sh")
        self.assertTrue(os.path.isfile(builder),
                        "cli/build-cli.sh is missing — nothing builds bin/pet-talk-cli")
        self.assertTrue(os.access(builder, os.X_OK), "cli/build-cli.sh is not executable")

    def test_the_makefile_builds_the_cli_before_the_daemon(self):
        with open(os.path.join(ROOT, "Makefile")) as f:
            mk = f.read()
        self.assertIn("build-cli:", mk, "no `make build-cli` target")
        self.assertIn("cli/build-cli.sh bin/", mk)
        self.assertIn("build-hotkey: build-cli", mk,
                      "build-hotkey must depend on build-cli — a daemon with no "
                      "CLI to spawn has a hotkey that does nothing")

    def test_the_launcher_prints_help_with_no_server(self):
        """Needs no server, no mic: proves the launcher starts the CLI at all.

        Specifically it proves the module form is used. `python3 cli/client.py`
        raises ImportError (cli/client.py does `from .audio import ...`, a
        relative import with no parent package), so a launcher that got this
        wrong would fail here and nowhere else until a hotkey press.
        """
        cli = os.path.join(ROOT, "bin", "pet-talk-cli")
        if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
            raise unittest.SkipTest(
                f"SKIP: no launcher at {cli} — run `make build-cli` (cheap, no "
                "compiler) to write it"
            )
        res = subprocess.run([cli, "--help"], capture_output=True, text=True,
                             timeout=60, env=HEADLESS_ENV)
        self.assertEqual(res.returncode, 0,
                         f"`pet-talk-cli --help` failed (rc={res.returncode}):\n"
                         f"{res.stderr}\n{res.stdout}")
        self.assertIn("pet-talk-cli", res.stdout)
        self.assertIn("--hotkey", res.stdout,
                      "--help did not print the real CLI's options, so the "
                      "launcher did not reach cli/client.py:main")

    def test_the_launcher_bakes_in_no_absolute_repo_path(self):
        cli = os.path.join(ROOT, "bin", "pet-talk-cli")
        if not os.path.isfile(cli):
            raise unittest.SkipTest("SKIP: no launcher at %s — run `make build-cli`" % cli)
        with open(cli) as f:
            text = f.read()
        self.assertNotIn(ROOT, text,
                         "the launcher must resolve the repo root from its own "
                         "location, never bake one in")
        self.assertIn("-m cli.client", text,
                      "the CLI must be started as a module, not as a script")

    def test_dump_state_resolves_the_cli_path(self):
        _skip_if_no_bin()
        cli = os.path.join(ROOT, "bin", "pet-talk-cli")
        if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
            raise unittest.SkipTest(
                f"SKIP: no launcher at {cli} — run `make build-cli` first; "
                "resolution is what this test reads"
            )
        res = subprocess.run([BIN_PATH, "--dump-state"], capture_output=True,
                             text=True, timeout=15, env=HEADLESS_ENV)
        if res.returncode != 0:
            raise AssertionError(
                f"--dump-state failed (rc={res.returncode}):\n{res.stderr}\n{res.stdout}")
        import json
        state = json.loads(res.stdout)
        info = state.get("cli")
        self.assertIsInstance(info, dict,
                             "--dump-state must report what Option+Tab would spawn")
        self.assertTrue(
            info.get("resolved"),
            "the daemon could not resolve a CLI launcher even though one "
            f"exists at {cli}: {info.get('reason')!r}",
        )
        self.assertTrue(str(info.get("path", "")).endswith("pet-talk-cli"),
                        f"resolved path is not the launcher: {info!r}")
        self.assertTrue(os.access(info["path"], os.X_OK),
                        f"resolved path is not executable: {info!r}")
# ---------------------------------------------------------------------------
# Native turn engine (cli/hotkey/AudioCapture.swift, WSClient.swift,
# ChunkPlayback.swift, TurnController.swift)
#
# Everything here is headless and hardware-free: the "microphone" is
# SyntheticAudioSource, the "speaker" is SilentPlaybackSink, and the studio is
# qa/fixtures/fake_ws_server.py. No daemon is started, nothing is played, and
# no real mic is opened (the one real-mic check lives in the binary behind
# PET_TALK_REAL_MIC=1 and is never run from here).
# ---------------------------------------------------------------------------

FAKE_SERVER = os.path.join(ROOT, "qa", "fixtures", "fake_ws_server.py")


def _skip_if_no_websockets():
    try:
        import websockets  # noqa: F401
    except ImportError:
        raise unittest.SkipTest(
            "SKIP: the `websockets` package is not installed, so the fake "
            "studio fixture cannot run (pip install websockets)"
        )


class FakeStudio:
    """Runs qa/fixtures/fake_ws_server.py for the life of one test."""

    def __init__(self, script="normal", require_token="", health_status=200):
        self.args = [sys.executable, FAKE_SERVER, "--script", script,
                     "--health-status", str(health_status)]
        if require_token:
            self.args += ["--require-token", require_token]
        self.proc = None
        self.log_path = None
        self.ws_url = None

    def __enter__(self):
        fd, self.log_path = tempfile.mkstemp(prefix="pet-talk-fake-ws-", suffix=".jsonl")
        os.close(fd)
        self.proc = subprocess.Popen(
            self.args + ["--log", self.log_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        line = self.proc.stdout.readline()
        if not line:
            self.__exit__(None, None, None)
            raise unittest.SkipTest("SKIP: the fake studio fixture did not start")
        info = json.loads(line)
        if not info.get("ok"):
            self.__exit__(None, None, None)
            raise unittest.SkipTest("SKIP: fake studio fixture: %s" % info.get("reason"))
        self.ws_url = info["ws_url"]
        return self

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        return False

    def received(self):
        """Every frame the client actually sent, in wire order."""
        if not self.log_path or not os.path.isfile(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def cleanup(self):
        if self.log_path and os.path.isfile(self.log_path):
            os.unlink(self.log_path)


def run_native_self_test(extra=None, env_extra=None, timeout=60):
    """`--self-test --turn-engine native`, headless. Returns (result, metrics)."""
    env = dict(HEADLESS_ENV)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(
        [BIN_PATH, "--self-test", "--turn-engine", "native"] + list(extra or []),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    metrics = {}
    for line in res.stdout.splitlines():
        if line.startswith("NATIVE-METRICS "):
            metrics = json.loads(line[len("NATIVE-METRICS "):])
    return res, metrics


class TestNativeEngineUnitChecks(unittest.TestCase):
    """The classes on their own: VAD constants and timing, the WAV parser, the
    barge generation counter, token resolution, engine precedence. No server."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        cls.res, cls.metrics = run_native_self_test()

    def test_all_unit_checks_pass(self):
        self.assertEqual(self.res.returncode, 0,
                         f"native self-test failed:\n{self.res.stdout}\n{self.res.stderr}")
        self.assertIn("PASS: native turn engine self-test", self.res.stdout)

    def test_vad_constants_match_the_cli(self):
        # One number moving here and not in cli/audio.py would be two products.
        self.assertIn("VAD threshold matches cli/audio.py — 350.0", self.res.stdout)
        self.assertIn("VAD trailing silence matches cli/audio.py — 800.0ms", self.res.stdout)
        self.assertIn("VAD confirm frames matches cli/audio.py — 2", self.res.stdout)
        self.assertIn("VAD initial-silence cutoff matches cli/audio.py — 4000.0ms", self.res.stdout)

    def test_vad_ends_the_turn_at_exactly_800ms(self):
        self.assertIn("VAD ends the turn on 800ms of silence — 800.0ms, 40 frames",
                      self.res.stdout)
        self.assertIn("VAD abandons a silent turn at 4000ms", self.res.stdout)

    def test_barge_discards_later_chunks(self):
        self.assertIn("late chunks are discarded, not spoken — discarded=2 played=1",
                      self.res.stdout)
        self.assertIn("PASS: no chunk played after the barge", self.res.stdout)

    def test_barge_cut_is_under_ten_ms(self):
        self.assertIn("PASS: barge cuts playback within 10ms", self.res.stdout)
        self.assertGreaterEqual(self.metrics.get("bargeToSilenceMs", -1), 0,
                                "barge-to-silence was not measured")
        self.assertLess(self.metrics["bargeToSilenceMs"], 10.0)

    def test_frame_geometry_and_safety_cap(self):
        self.assertIn("PASS: frame is 20ms of 16kHz mono — 320 samples", self.res.stdout)
        self.assertIn("PASS: safety cap is 20s — 20.0s", self.res.stdout)

    def test_real_mic_is_off_unless_asked_for(self):
        # The one hardware path in the binary must stay behind its env gate.
        self.assertIn("SKIP: real mic capture — set PET_TALK_REAL_MIC=1", self.res.stdout)


class TestNativeTurnAgainstFakeStudio(unittest.TestCase):
    """One whole turn over a real socket: frame order, VAD end-of-turn timing,
    chunk playback, word_times beats."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        _skip_if_no_websockets()
        with FakeStudio(script="normal") as studio:
            cls.res, cls.metrics = run_native_self_test(["--fake-server", studio.ws_url])
            cls.sent = studio.received()
            studio.cleanup()

    def test_self_test_passes(self):
        self.assertEqual(self.res.returncode, 0,
                         f"native turn failed:\n{self.res.stdout}\n{self.res.stderr}")

    def test_frame_order_on_the_wire(self):
        types = [f["type"] for f in self.sent]
        self.assertEqual(types[0], "user.start", types[:4])
        self.assertEqual(types[-1], "user.stop", types[-4:])
        self.assertTrue(all(t == "user.chunk" for t in types[1:-1]),
                        "something other than user.chunk streamed mid-turn: %s" % set(types[1:-1]))
        self.assertGreater(types.count("user.chunk"), 10, types.count("user.chunk"))

    def test_every_frame_carries_a_turn_id(self):
        # SPEC §4: a frame without turn_id is refused server-side by construction.
        for frame in self.sent:
            self.assertTrue(frame.get("turn_id"), frame)
        ids = {f["turn_id"] for f in self.sent}
        self.assertEqual(len(ids), 1, "one turn must use one turn_id: %s" % ids)

    def test_user_chunk_carries_16k_linear16(self):
        chunks = [f for f in self.sent if f["type"] == "user.chunk"]
        self.assertTrue(chunks)
        for frame in chunks[:5]:
            self.assertEqual(frame.get("sample_rate"), 16000, frame)
            raw = base64.b64decode(frame["chunk"])
            self.assertEqual(len(raw), 640, "a frame is 20ms of 16kHz mono = 640 bytes")

    def test_user_stop_carries_the_merged_buffer(self):
        stop = [f for f in self.sent if f["type"] == "user.stop"][-1]
        self.assertEqual(stop.get("sample_rate"), 16000)
        merged = base64.b64decode(stop["pcm_b64"])
        streamed = sum(
            len(base64.b64decode(f["chunk"])) for f in self.sent if f["type"] == "user.chunk"
        )
        self.assertEqual(len(merged), streamed,
                         "user.stop's buffer must be exactly what was streamed")

    def test_vad_end_of_turn_timing(self):
        # The synthetic source speaks for 700ms then goes quiet; the turn must
        # end on 800ms of trailing silence, the CLI's constant.
        self.assertEqual(self.metrics.get("vadTrailingSilenceMs"), 800)
        self.assertIn("PASS: VAD ended the turn on trailing silence", self.res.stdout)

    def test_chunks_played_and_word_times_drove_the_beat(self):
        self.assertGreater(self.metrics.get("chunksPlayed", 0), 0)
        self.assertEqual(self.metrics.get("playedAfterBarge"), 0)
        self.assertGreater(self.metrics.get("beats", 0), 0,
                           "agent.sentence word_times did not drive the glyph beat")

    def test_press_to_first_user_chunk_is_measured(self):
        ms = self.metrics.get("pressToFirstChunkMs", -1)
        self.assertGreaterEqual(ms, 0, "press-to-first-chunk was not measured")
        # Generous: this asserts the number is real, not a latency budget.
        # qa/budgets.json is the only place a budget lives.
        self.assertLess(ms, 2000, "%.1fms from hotkey to first user.chunk" % ms)


class TestNativeBargeAgainstFakeStudio(unittest.TestCase):
    """The barge half: the frame goes out, playback is cut, and the chunks the
    socket was already carrying are discarded instead of spoken."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        _skip_if_no_websockets()
        with FakeStudio(script="barge") as studio:
            cls.res, cls.metrics = run_native_self_test(
                ["--fake-server", studio.ws_url, "--barge-after-first-chunk"]
            )
            cls.sent = studio.received()
            studio.cleanup()

    def test_self_test_passes(self):
        self.assertEqual(self.res.returncode, 0,
                         f"native barge failed:\n{self.res.stdout}\n{self.res.stderr}")

    def test_barge_frame_is_sent_after_user_stop(self):
        types = [f["type"] for f in self.sent]
        self.assertIn("barge", types)
        self.assertLess(types.index("user.stop"), types.index("barge"))

    def test_late_chunks_are_discarded_not_spoken(self):
        self.assertGreater(self.metrics.get("chunksDiscarded", 0), 0,
                           "the fixture's in-flight chunks were not discarded")
        self.assertEqual(self.metrics.get("playedAfterBarge"), 0,
                         "a chunk played after the barge — the contract is broken")

    def test_barge_to_silence_measured(self):
        self.assertGreaterEqual(self.metrics.get("bargeToSilenceMs", -1), 0)
        self.assertLess(self.metrics["bargeToSilenceMs"], 10.0)

    def test_the_server_barge_ack_is_adopted(self):
        # server/ws.py mints a new turn_id on the state.listening ack.
        self.assertIn("[barge] server dropped", self.res.stdout)


class TestNativeFailurePaths(unittest.TestCase):
    """Fail closed, with a name. A studio that cannot be reached, or will not
    take our token, must never open the microphone."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def test_health_probe_failure_on_a_dead_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        res, metrics = run_native_self_test(
            ["--fake-server", "ws://127.0.0.1:%d/ws" % dead_port,
             "--expect-error", "health_probe_failed"]
        )
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("PASS: failed with the named reason health_probe_failed", res.stdout)
        self.assertIn("PASS: the microphone was never opened", res.stdout)
        self.assertEqual(metrics.get("userChunksSent"), 0)

    def test_health_probe_failure_on_a_degraded_studio(self):
        _skip_if_no_websockets()
        with FakeStudio(health_status=503) as studio:
            res, _ = run_native_self_test(
                ["--fake-server", studio.ws_url, "--expect-error", "health_probe_failed"]
            )
            studio.cleanup()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("PASS: failed with the named reason health_probe_failed", res.stdout)

    def test_wrong_token_is_named_not_retried_forever(self):
        _skip_if_no_websockets()
        with FakeStudio(require_token="fixture-secret") as studio:
            res, metrics = run_native_self_test(
                ["--fake-server", studio.ws_url, "--expect-error", "ws_unauthorized"],
                env_extra={"STUDIO_TOKEN": "the-wrong-one"},
            )
            studio.cleanup()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("PASS: failed with the named reason ws_unauthorized", res.stdout)
        self.assertEqual(metrics.get("userChunksSent"), 0)

    def test_agent_error_lands_on_the_capsule_error_state(self):
        _skip_if_no_websockets()
        with FakeStudio(script="noaudio") as studio:
            res, metrics = run_native_self_test(
                ["--fake-server", studio.ws_url, "--expect-error", "tts_unavailable"]
            )
            studio.cleanup()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("PASS: the capsule ended in its error state", res.stdout)
        self.assertEqual(metrics.get("states", [])[-1:], ["error"])


class TestTokenResolution(unittest.TestCase):
    """server/auth.py's order, ported: STUDIO_TOKEN, then STUDIO_TOKEN_FILE,
    then <repo>/.qa-scratch/studio.token. The value is never printed."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def _dump_state(self, env_extra):
        env = dict(HEADLESS_ENV)
        env.pop("STUDIO_TOKEN", None)
        env.pop("STUDIO_TOKEN_FILE", None)
        env.update(env_extra)
        res = subprocess.run([BIN_PATH, "--dump-state"], capture_output=True,
                             text=True, timeout=15, env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_env_token_wins(self):
        dump = self._dump_state({"STUDIO_TOKEN": "from-env"})
        self.assertEqual(dump["turnEngine"]["tokenSource"], "env")
        self.assertTrue(dump["turnEngine"]["tokenPresent"])
        self.assertNotIn("from-env", json.dumps(dump), "the token VALUE leaked into --dump-state")

    def test_token_file_is_read_when_env_is_unset(self):
        fd, path = tempfile.mkstemp(prefix="studio-token-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("  from-file\n")
            dump = self._dump_state({"STUDIO_TOKEN_FILE": path})
            self.assertEqual(dump["turnEngine"]["tokenSource"], "file")
            self.assertTrue(dump["turnEngine"]["tokenPresent"])
            self.assertNotIn("from-file", json.dumps(dump))
        finally:
            os.unlink(path)

    def test_empty_token_file_falls_through_by_name(self):
        fd, path = tempfile.mkstemp(prefix="studio-token-empty-")
        os.close(fd)
        try:
            dump = self._dump_state({"STUDIO_TOKEN_FILE": path})
            # Falls through to the generated file, or reports `none` — never a
            # silent empty token that looks like a real one.
            self.assertIn(dump["turnEngine"]["tokenSource"], ("generated", "none"))
        finally:
            os.unlink(path)


class TestTurnEngineConfig(unittest.TestCase):
    """`turn_engine` in config.yaml, PET_TALK_TURN_ENGINE, --turn-engine."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def _dump_state(self, env_extra=None, args=None):
        env = dict(HEADLESS_ENV)
        env.pop("PET_TALK_TURN_ENGINE", None)
        env.update(env_extra or {})
        res = subprocess.run([BIN_PATH, "--dump-state"] + list(args or []),
                             capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_native_is_the_default(self):
        dump = self._dump_state()
        self.assertEqual(dump["turnEngine"]["default"], "native")

    def test_env_selects_the_cli_fallback(self):
        dump = self._dump_state({"PET_TALK_TURN_ENGINE": "cli"})
        self.assertEqual(dump["turnEngine"]["engine"], "cli")

    def test_flag_beats_env(self):
        dump = self._dump_state({"PET_TALK_TURN_ENGINE": "cli"},
                                ["--turn-engine", "native"])
        self.assertEqual(dump["turnEngine"]["engine"], "native")

    def test_config_yaml_key_round_trips(self):
        fd, path = tempfile.mkstemp(prefix="pet-talk-config-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write('version: "1.0"\nturn_engine: "cli"\n')
            dump = self._dump_state({"PET_TALK_CONFIG_PATH": path})
            self.assertEqual(dump["turnEngine"]["configured"], "cli")
            self.assertEqual(dump["turnEngine"]["engine"], "cli")
        finally:
            os.unlink(path)

    def test_native_audio_contract_is_readable_from_the_binary(self):
        audio = self._dump_state()["nativeAudio"]
        self.assertEqual(audio["sampleRate"], 16000)
        self.assertEqual(audio["frameSamples"], 320)
        self.assertEqual(audio["frameMs"], 20)
        self.assertEqual(audio["vadThreshold"], 350)
        self.assertEqual(audio["vadSilenceMs"], 800)
        self.assertEqual(audio["maxTurnSeconds"], 20)
        self.assertEqual(audio["chunkMaxBytes"], 512 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
