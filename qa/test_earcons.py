#!/usr/bin/env python3
"""qa/test_earcons.py — TDD test suite for Pet-Talk Acoustic Earcon & Config Engine.

Verifies:
1. Native macOS sound file existence for all default and alternate sound packs.
2. Swift source files exist and compile cleanly via `swiftc -O cli/hotkey/*.swift`.
3. Micro-acoustic playback latency meets the rigid SLA budget (< 2.0ms, measured < 0.2ms).
4. CLI commands & flags: test-audio, --sound-pack, --volume, --no-audio.
5. Configuration parsing and environment override via PET_TALK_CONFIG_PATH.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT_DIR = os.path.join(ROOT, "cli", "hotkey")
BIN_PATH = os.path.join(ROOT, "bin", "pet-talk-hotkey")

REQUIRED_SOUND_FILES = [
    "/System/Library/Sounds/Tink.aiff",
    "/System/Library/Sounds/Pop.aiff",
    "/System/Library/Sounds/Bottle.aiff",
    "/System/Library/Sounds/Basso.aiff",
]

CYBERPUNK_SOUND_FILES = [
    "/System/Library/Sounds/Submarine.aiff",
    "/System/Library/Sounds/Ping.aiff",
    "/System/Library/Sounds/Funk.aiff",
    "/System/Library/Sounds/Sosumi.aiff",
]


class TestSoundAssetAvailability(unittest.TestCase):
    """Verify native macOS system sounds exist on host."""

    def test_default_apple_minimal_sounds_exist(self):
        for path in REQUIRED_SOUND_FILES:
            self.assertTrue(
                os.path.isfile(path),
                f"Required system sound missing: {path}",
            )
            self.assertGreater(
                os.path.getsize(path),
                0,
                f"Sound file is empty: {path}",
            )

    def test_cyberpunk_sound_pack_assets_exist(self):
        for path in CYBERPUNK_SOUND_FILES:
            self.assertTrue(
                os.path.isfile(path),
                f"Cyberpunk sound pack asset missing: {path}",
            )


class TestEarconCompilation(unittest.TestCase):
    """Verify modular compilation of Swift hotkey & earcon sources."""

    def test_sources_exist(self):
        expected = ["main.swift", "earcons.swift", "config.swift"]
        for fname in expected:
            path = os.path.join(SWIFT_DIR, fname)
            self.assertTrue(os.path.isfile(path), f"Missing source file: {path}")

    def test_clean_compilation(self):
        swiftc = shutil.which("swiftc")
        self.assertIsNotNone(swiftc, "swiftc must be available on macOS")

        swift_files = sorted(glob.glob(os.path.join(SWIFT_DIR, "*.swift")))
        os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
        cmd = ["swiftc", "-O"] + swift_files + ["-o", BIN_PATH]
        res = subprocess.run(cmd, capture_output=True, text=True)

        self.assertEqual(
            res.returncode,
            0,
            f"Compilation failed:\nStdout: {res.stdout}\nStderr: {res.stderr}",
        )
        self.assertTrue(os.path.isfile(BIN_PATH))
        self.assertTrue(os.access(BIN_PATH, os.X_OK))


class TestEarconPlaybackAndLatency(unittest.TestCase):
    """Verify earcon execution latency is strictly within the <2ms SLA budget."""

    @classmethod
    def setUpClass(cls):
        swift_files = sorted(glob.glob(os.path.join(SWIFT_DIR, "*.swift")))
        subprocess.run(["swiftc", "-O"] + swift_files + ["-o", BIN_PATH], check=True)

    def test_test_audio_sequence_and_latency(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"test-audio failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("Testing Pet-Talk Acoustic Earcon Engine", res.stdout)
        self.assertIn("playMicOpen()", res.stdout)
        self.assertIn("playSilenceCutoff()", res.stdout)
        self.assertIn("playBargeKill()", res.stdout)
        self.assertIn("playError()", res.stdout)
        self.assertIn("PASS: All earcon triggers executed within SLA budget", res.stdout)

        # Parse latency numbers from each line, e.g. "(0.033ms) [PASS]"
        latencies = re.findall(r"\(([0-9.]+)ms\)\s+\[PASS\]", res.stdout)
        self.assertEqual(len(latencies), 4, f"Expected 4 passed earcons, got: {latencies}")

        budget_ms = 2.0
        for lat_str in latencies:
            lat = float(lat_str)
            self.assertLess(
                lat,
                budget_ms,
                f"Earcon latency {lat}ms exceeded budget of {budget_ms}ms",
            )

    def test_sound_pack_cyberpunk_flag(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio", "--sound-pack", "cyberpunk"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Cyberpunk test failed:\n{res.stdout}")
        self.assertIn("Sound Pack: cyberpunk", res.stdout)
        self.assertIn("Submarine.aiff", res.stdout)
        self.assertIn("Ping.aiff", res.stdout)
        self.assertIn("Funk.aiff", res.stdout)
        self.assertIn("Sosumi.aiff", res.stdout)

    def test_sound_pack_haptic_flag(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio", "--sound-pack", "haptic"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Haptic test failed:\n{res.stdout}")
        self.assertIn("Sound Pack: haptic", res.stdout)

    def test_no_audio_flag(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio", "--no-audio"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Enabled: false", res.stdout)
        self.assertIn("DISABLED", res.stdout)
        self.assertIn("Audio earcons disabled", res.stdout)

    def test_volume_flag(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio", "--volume", "0.45"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Volume: 0.45", res.stdout)


class TestConfigSupport(unittest.TestCase):
    """Verify configuration reading, falling back, and environment override."""

    @classmethod
    def setUpClass(cls):
        swift_files = sorted(glob.glob(os.path.join(SWIFT_DIR, "*.swift")))
        subprocess.run(["swiftc", "-O"] + swift_files + ["-o", BIN_PATH], check=True)

    def test_config_show(self):
        res = subprocess.run(
            [BIN_PATH, "config", "show"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("audio:", res.stdout)
        self.assertIn("enabled:", res.stdout)
        self.assertIn("volume:", res.stdout)
        self.assertIn("sound_pack:", res.stdout)

    def test_custom_config_override_via_env(self):
        custom_yaml = """
version: "1.0"
audio:
  enabled: true
  volume: 0.85
  sound_pack: "cyberpunk"
  custom_sounds:
    mic_open: "/System/Library/Sounds/Glass.aiff"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(custom_yaml)
            tmp_path = f.name

        try:
            env = dict(os.environ)
            env["PET_TALK_CONFIG_PATH"] = tmp_path

            res = subprocess.run(
                [BIN_PATH, "test-audio"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, f"Failed with custom config:\n{res.stdout}")
            self.assertIn("Sound Pack: cyberpunk", res.stdout)
            self.assertIn("Volume: 0.85", res.stdout)
            self.assertIn("Glass.aiff", res.stdout)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
