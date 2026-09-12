#!/usr/bin/env python3
"""qa/test_earcons.py — QA suite for the Acoustic Earcon & Config Engine.

Fan rule: never builds Swift here. Playback tests require a prebuilt binary
at $PET_TALK_HOTKEY_BIN (default: bin/pet-talk-hotkey) and SKIP with a clear
reason when absent — never auto-built.

Silent rule: `test-audio` actually plays sound through the real earcon
engine. Every test that invokes it SKIPs when PET_TALK_SILENT=1, printing
the reason, before touching the binary.

Verifies (when applicable):
1. Native macOS sound file existence for all default and alternate sound packs.
2. Micro-acoustic playback latency meets the rigid SLA budget (< 5.0ms).
3. CLI commands & flags: test-audio, --sound-pack, --volume, --no-audio.
4. Configuration parsing and environment override via PET_TALK_CONFIG_PATH.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT_DIR = os.path.join(ROOT, "cli", "hotkey")
BIN_PATH = os.environ.get("PET_TALK_HOTKEY_BIN") or os.path.join(ROOT, "bin", "pet-talk-hotkey")
HAVE_BIN = os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK)
SILENT = os.environ.get("PET_TALK_SILENT") == "1"


def _skip_if_no_bin():
    if not HAVE_BIN:
        raise unittest.SkipTest(
            "SKIP: no prebuilt hotkey binary at %s — build it on `ssh air` "
            "via `cli/hotkey/build.sh bin/` (or `make build-hotkey`), never "
            "here (fan rule); set PET_TALK_HOTKEY_BIN to point at it" % BIN_PATH
        )


def _skip_if_silent():
    if SILENT:
        raise unittest.SkipTest(
            "SKIP: PET_TALK_SILENT=1 — this invokes `test-audio`, which "
            "plays real earcon sound through the native engine"
        )


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


class TestEarconSourcesPresent(unittest.TestCase):
    """Static check only — never compiles (fan rule)."""

    def test_sources_exist(self):
        expected = ["main.swift", "earcons.swift", "config.swift"]
        missing = [f for f in expected if not os.path.isfile(os.path.join(SWIFT_DIR, f))]
        if missing:
            raise unittest.SkipTest(f"SKIP: sources not present yet: {missing}")


class TestEarconPlaybackAndLatency(unittest.TestCase):
    """Verify earcon execution latency is strictly within the <5ms SLA budget.

    Plays real sound via `test-audio` — SKIPs under PET_TALK_SILENT=1.
    """

    @classmethod
    def setUpClass(cls):
        _skip_if_silent()
        _skip_if_no_bin()

    def test_test_audio_sequence_and_latency(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio"],
            capture_output=True,
            text=True,
            timeout=15,
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

        budget_ms = 5.0  # SLA budget from SPEC-PET-TALK-003 section 4 (<=5.0ms)
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
            timeout=15,
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
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"Haptic test failed:\n{res.stdout}")
        self.assertIn("Sound Pack: haptic", res.stdout)

    def test_no_audio_flag(self):
        res = subprocess.run(
            [BIN_PATH, "test-audio", "--no-audio"],
            capture_output=True,
            text=True,
            timeout=15,
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
            timeout=15,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Volume: 0.45", res.stdout)


class TestConfigSupport(unittest.TestCase):
    """Verify configuration reading, falling back, and environment override."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

    def test_config_show(self):
        res = subprocess.run(
            [BIN_PATH, "config", "show"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("audio:", res.stdout)
        self.assertIn("enabled:", res.stdout)
        self.assertIn("volume:", res.stdout)
        self.assertIn("sound_pack:", res.stdout)

    def test_custom_config_override_via_env(self):
        _skip_if_silent()  # invokes test-audio -> plays real sound
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
                timeout=15,
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
