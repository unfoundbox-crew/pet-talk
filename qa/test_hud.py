#!/usr/bin/env python3
"""qa/test_hud.py — TDD test suite for Pet-Talk Native macOS Floating Glass Capsule HUD.

Verifies:
1. Clean Swift compilation of `hud_window.swift` alongside `main.swift`.
2. NSPanel window properties and nonactivating flags:
   - `styleMask = [.nonactivatingPanel, .borderless]` (never steal keyboard focus).
   - `level = .floating` (floats above normal windows).
   - `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`.
   - `ignoresMouseEvents = true` (transparent click-through).
   - `isOpaque = false`, `backgroundColor = .clear`.
   - `canBecomeKey = false`, `canBecomeMain = false`.
3. Capsule visual geometry: 220px width x 44px height, corner radius 22px.
4. CLI execution of `./bin/pet-talk-hotkey test-hud` cycling through states.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUD_SWIFT_SRC = os.path.join(ROOT, "cli", "hotkey", "hud_window.swift")
ALL_SWIFT_SRCS = sorted(glob.glob(os.path.join(ROOT, "cli", "hotkey", "*.swift")))
BIN_PATH = os.path.join(ROOT, "bin", "pet-talk-hotkey")


def setUpModule():
    """Ensure hotkey binary is compiled before test suite executes."""
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    cmd = ["swiftc", "-O"] + ALL_SWIFT_SRCS + ["-o", BIN_PATH]
    subprocess.run(cmd, check=True)


class TestHUDCompilation(unittest.TestCase):
    """Verify clean Swift compilation of hud_window.swift and integrated binary."""

    def test_swiftc_compiler_available(self):
        swiftc = shutil.which("swiftc")
        self.assertIsNotNone(swiftc, "swiftc compiler must be available on macOS")

    def test_standalone_hud_compilation(self):
        self.assertTrue(os.path.exists(HUD_SWIFT_SRC), f"Missing: {HUD_SWIFT_SRC}")
        cmd = ["swiftc", "-c", HUD_SWIFT_SRC, "-o", "/tmp/pet_talk_hud_test.o"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(
            res.returncode,
            0,
            f"hud_window.swift failed to compile:\nStdout: {res.stdout}\nStderr: {res.stderr}",
        )

    def test_integrated_binary_compilation(self):
        test_bin = "/tmp/pet_talk_hotkey_test_bin"
        cmd = ["swiftc", "-O"] + ALL_SWIFT_SRCS + ["-o", test_bin]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(
            res.returncode,
            0,
            f"Integrated hotkey binary failed to compile:\nStdout: {res.stdout}\nStderr: {res.stderr}",
        )
        self.assertTrue(os.path.exists(test_bin))
        self.assertTrue(os.access(test_bin, os.X_OK))


class TestHUDWindowSpecifications(unittest.TestCase):
    """Verify window properties, nonactivating flags, and frame dimensions."""

    def test_window_geometry_and_nonactivating_flags(self):
        res = subprocess.run(
            [BIN_PATH, "--dump-hud-spec"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"--dump-hud-spec failed:\n{res.stderr}")
        
        data = json.loads(res.stdout)
        
        # 1. Visual Capsule Frame Geometry
        self.assertEqual(data.get("width"), 220.0, "Capsule width must be 220px")
        self.assertEqual(data.get("height"), 44.0, "Capsule height must be 44px")
        self.assertEqual(data.get("cornerRadius"), 22.0, "Corner radius must be 22px")

        # 2. Non-activating style masks (CRUCIAL: never steal keyboard focus)
        self.assertTrue(data.get("isNonactivatingPanel"), "Must have .nonactivatingPanel styleMask")
        self.assertTrue(data.get("isBorderless"), "Must have .borderless styleMask")
        self.assertFalse(data.get("canBecomeKey"), "canBecomeKey must be false")
        self.assertFalse(data.get("canBecomeMain"), "canBecomeMain must be false")

        # 3. Floating window level & multi-space visibility
        self.assertTrue(data.get("isFloatingLevel"), "Window level must be .floating")
        self.assertTrue(data.get("canJoinAllSpaces"), "collectionBehavior must include .canJoinAllSpaces")
        self.assertTrue(data.get("fullScreenAuxiliary"), "collectionBehavior must include .fullScreenAuxiliary")

        # 4. Click-through and transparency
        self.assertTrue(data.get("ignoresMouseEvents"), "ignoresMouseEvents must be true for click-through")
        self.assertFalse(data.get("isOpaque"), "isOpaque must be false")
        self.assertTrue(data.get("isClearBackground"), "backgroundColor must be .clear")


class TestHUDInteractiveSequence(unittest.TestCase):
    """Verify CLI test-hud sequence execution."""

    def test_hud_visual_test_command_output(self):
        res = subprocess.run(
            [BIN_PATH, "test-hud"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"test-hud failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("Testing Pet-Talk Floating Glass Capsule HUD", res.stdout)
        self.assertIn("[LISTENING] Emerald True (#10b981)", res.stdout)
        self.assertIn("[THINKING] SpacePilot Gold (#c9a227)", res.stdout)
        self.assertIn("[SPEAKING] Liquid Silver (#cfd4dc)", res.stdout)
        self.assertIn("PASS: HUD visual test sequence completed.", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
