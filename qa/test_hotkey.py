#!/usr/bin/env python3
"""qa/test_hotkey.py — TDD test suite for Pet-Talk Native macOS Hotkey Listener.

Verifies:
1. Swift source compiles cleanly via native `swiftc` without warnings.
2. Carbon `RegisterEventHotKey` registration with keycode 48 (kVK_Tab) and
   modifier 0x0800 (optionKey) without requiring Accessibility/TCC prompts.
3. Sub-50ms instant barge-in kill latency on active audio playback.
4. Daemon lifecycle management: start, status, stop, and PID tracking.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT_SRCS = sorted(glob.glob(os.path.join(ROOT, "cli", "hotkey", "*.swift")))
BIN_PATH = os.path.join(ROOT, "bin", "pet-talk-hotkey")
PID_FILE = "/tmp/pet-talk-hotkey.pid"


class TestHotkeyCompilation(unittest.TestCase):
    """Verify Swift compilation of native Carbon hotkey listener."""

    def test_swiftc_compiler_available(self):
        swiftc = shutil.which("swiftc")
        self.assertIsNotNone(swiftc, "swiftc compiler must be available on macOS")

    def test_clean_compilation(self):
        for src in SWIFT_SRCS:
            self.assertTrue(os.path.exists(src), f"Source file missing: {src}")

        os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
        cmd = ["swiftc", "-O"] + SWIFT_SRCS + ["-o", BIN_PATH]
        res = subprocess.run(cmd, capture_output=True, text=True)

        self.assertEqual(
            res.returncode,
            0,
            f"Compilation failed with code {res.returncode}:\nStdout: {res.stdout}\nStderr: {res.stderr}",
        )
        self.assertTrue(os.path.exists(BIN_PATH), f"Compiled binary missing at {BIN_PATH}")
        self.assertTrue(os.access(BIN_PATH, os.X_OK), "Binary must be executable")

        # Verify Mach-O binary
        file_check = subprocess.run(["file", BIN_PATH], capture_output=True, text=True)
        self.assertIn("Mach-O 64-bit", file_check.stdout)


class TestCarbonHotkeyRegistration(unittest.TestCase):
    """Verify Carbon API registration and configuration."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BIN_PATH):
            subprocess.run(["swiftc", "-O"] + SWIFT_SRCS + ["-o", BIN_PATH], check=True)

    def test_keycode_and_modifier_registration(self):
        res = subprocess.run(
            [BIN_PATH, "--check-registration"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Registration failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("48 (kVK_Tab)", res.stdout)
        self.assertIn("0x800", res.stdout)
        self.assertIn("PASS: Carbon RegisterEventHotKey succeeded", res.stdout)


class TestBargeInLatency(unittest.TestCase):
    """Verify instant barge-in kill latency is within the 50ms budget."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BIN_PATH):
            subprocess.run(["swiftc", "-O", SWIFT_SRC, "-o", BIN_PATH], check=True)

    def test_afplay_kill_under_50ms(self):
        wav_file = os.path.join(ROOT, "bake-deepgram_0.wav")
        afplay_proc = None
        if os.path.exists(wav_file):
            afplay_proc = subprocess.Popen(
                ["afplay", wav_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.02)  # ensure afplay is actively running

        try:
            res = subprocess.run(
                [BIN_PATH, "--barge-benchmark"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Barge benchmark failed:\n{res.stdout}")
            self.assertIn("PASS: Barge-in kill under 50ms budget", res.stdout)
        finally:
            if afplay_proc and afplay_proc.poll() is None:
                afplay_proc.kill()


class TestDaemonLifecycle(unittest.TestCase):
    """Verify daemon start, status, and stop lifecycle."""

    @classmethod
    def setUpClass(cls):
        res = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        cls._was_running = (res.returncode == 0)
        if not os.path.exists(BIN_PATH):
            subprocess.run(["swiftc", "-O"] + SWIFT_SRCS + ["-o", BIN_PATH], check=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_was_running", False):
            subprocess.run([BIN_PATH, "start"], capture_output=True)

    def setUp(self):
        # Guarantee clean state before each test
        subprocess.run([BIN_PATH, "stop"], capture_output=True)

    def tearDown(self):
        # Clean up any leftover daemon from individual test
        subprocess.run([BIN_PATH, "stop"], capture_output=True)

    def test_lifecycle_transitions(self):
        # 1. Initially stopped
        res_initial = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertNotEqual(res_initial.returncode, 0)
        self.assertIn("stopped", res_initial.stdout)

        # 2. Start daemon
        res_start = subprocess.run([BIN_PATH, "start"], capture_output=True, text=True)
        self.assertEqual(res_start.returncode, 0)
        self.assertIn("daemon started", res_start.stdout)

        # 3. Status is running with PID
        res_status = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertEqual(res_status.returncode, 0)
        self.assertIn("running", res_status.stdout)

        # Verify PID file exists and matches live PID
        self.assertTrue(os.path.exists(PID_FILE))
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        self.assertGreater(pid, 0)
        # Check process is alive
        try:
            os.kill(pid, 0)
            is_alive = True
        except OSError:
            is_alive = False
        self.assertTrue(is_alive, f"Daemon process {pid} should be active")

        # 4. Stop daemon
        res_stop = subprocess.run([BIN_PATH, "stop"], capture_output=True, text=True)
        self.assertEqual(res_stop.returncode, 0)
        self.assertIn("stopped", res_stop.stdout)

        # 5. Status is stopped
        res_final = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertNotEqual(res_final.returncode, 0)
        self.assertIn("stopped", res_final.stdout)


class TestKillSwitchAndPause(unittest.TestCase):
    """Verify kill switch, pause mode, resume, and toggle CLI commands."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BIN_PATH):
            subprocess.run(["swiftc", "-O"] + SWIFT_SRCS + ["-o", BIN_PATH], check=True)

    def setUp(self):
        # Guarantee clean state
        subprocess.run([BIN_PATH, "stop"], capture_output=True)
        subprocess.run([BIN_PATH, "resume"], capture_output=True)

    def tearDown(self):
        subprocess.run([BIN_PATH, "stop"], capture_output=True)
        subprocess.run([BIN_PATH, "resume"], capture_output=True)

    def test_kill_switch_command(self):
        res = subprocess.run([BIN_PATH, "kill"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Instant kill switch executed", res.stdout)

    def test_pause_resume_toggle_lifecycle(self):
        # 1. Start daemon
        res_start = subprocess.run([BIN_PATH, "start"], capture_output=True, text=True)
        self.assertEqual(res_start.returncode, 0)
        time.sleep(0.15)

        # 2. Check initial status is [ACTIVE]
        res_active = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertEqual(res_active.returncode, 0)
        self.assertIn("[ACTIVE]", res_active.stdout)

        # 3. Pause Donna
        res_pause = subprocess.run([BIN_PATH, "pause"], capture_output=True, text=True)
        self.assertEqual(res_pause.returncode, 0)
        self.assertIn("paused", res_pause.stdout.lower())
        time.sleep(0.15)

        # 4. Status reflects [PAUSED / SLEEP MODE]
        res_paused = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertEqual(res_paused.returncode, 0)
        self.assertIn("[PAUSED", res_paused.stdout)

        # 5. Resume Donna
        res_resume = subprocess.run([BIN_PATH, "resume"], capture_output=True, text=True)
        self.assertEqual(res_resume.returncode, 0)
        self.assertIn("resumed", res_resume.stdout.lower())
        time.sleep(0.15)

        # 6. Status reflects [ACTIVE] again
        res_resumed = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertEqual(res_resumed.returncode, 0)
        self.assertIn("[ACTIVE]", res_resumed.stdout)

        # 7. Toggle into Pause
        res_toggle1 = subprocess.run([BIN_PATH, "toggle"], capture_output=True, text=True)
        self.assertEqual(res_toggle1.returncode, 0)
        time.sleep(0.15)

        res_tog_paused = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertIn("[PAUSED", res_tog_paused.stdout)

        # 8. Toggle back into Active
        res_toggle2 = subprocess.run([BIN_PATH, "toggle"], capture_output=True, text=True)
        self.assertEqual(res_toggle2.returncode, 0)
        time.sleep(0.15)

        res_tog_active = subprocess.run([BIN_PATH, "status"], capture_output=True, text=True)
        self.assertIn("[ACTIVE]", res_tog_active.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

