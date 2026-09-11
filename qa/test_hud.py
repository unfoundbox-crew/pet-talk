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
    """Verify window properties, nonactivating flags, notch specs, and motion tokens."""

    @classmethod
    def setUpClass(cls):
        res = subprocess.run(
            [BIN_PATH, "--dump-hud-spec"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"--dump-hud-spec failed:\n{res.stderr}"
        cls.spec = json.loads(res.stdout)

    def test_nonactivating_window_properties(self):
        """CRUCIAL: Ensure window never steals keyboard focus or activates."""
        data = self.spec
        self.assertTrue(data.get("isNonactivatingPanel"), "Must have .nonactivatingPanel styleMask")
        self.assertTrue(data.get("isBorderless"), "Must have .borderless styleMask")
        self.assertFalse(data.get("canBecomeKey"), "canBecomeKey must be false")
        self.assertFalse(data.get("canBecomeMain"), "canBecomeMain must be false")

        # Floating window level & multi-space visibility
        self.assertTrue(data.get("isFloatingLevel"), "Window level must be .floating")
        self.assertTrue(data.get("canJoinAllSpaces"), "collectionBehavior must include .canJoinAllSpaces")
        self.assertTrue(data.get("fullScreenAuxiliary"), "collectionBehavior must include .fullScreenAuxiliary")

        # Click-through and transparency
        self.assertTrue(data.get("ignoresMouseEvents"), "ignoresMouseEvents must be true for click-through")
        self.assertFalse(data.get("isOpaque"), "isOpaque must be false")
        self.assertTrue(data.get("isClearBackground"), "backgroundColor must be .clear")

    def test_hardware_notch_specifications(self):
        """Verify dynamic island notch dimensions, ear fillets, and squircle curvature."""
        data = self.spec
        notch_specs = data.get("notchSpecs")
        self.assertIsInstance(notch_specs, dict, "Must export notchSpecs dictionary")

        self.assertEqual(notch_specs.get("restingHeight"), 38.0, "Hardware notch resting height is 38px")
        self.assertEqual(notch_specs.get("listeningHeight"), 52.0, "Listening drip height is 52px")
        self.assertEqual(notch_specs.get("expandedHeight"), 60.0, "Expanded blossom height is 60px")

        self.assertEqual(notch_specs.get("restingWidth"), 220.0, "Hardware notch resting width is 220px")
        self.assertEqual(notch_specs.get("expandedWidth"), 440.0, "Blossom expanded width is 440px")

        self.assertEqual(notch_specs.get("earFilletRadius"), 10.0, "Top concave ear fillets radius is 10px")
        self.assertEqual(notch_specs.get("bottomCornerRadius"), 20.0, "Bottom continuous squircle radius is 20px")
        self.assertEqual(notch_specs.get("fallbackCornerRadius"), 22.0, "External monitor pill radius is 22px")
        self.assertEqual(notch_specs.get("hoverPeekHeight"), 6.0, "Hover peek shelf height is 6px")

        self.assertIn("hasNotch", data, "Must detect whether active screen has hardware notch")
        self.assertGreaterEqual(data.get("notchWidth", 0), 220.0, "Notch width must be >= 220px")

    def test_apple_motion_tokens(self):
        """Verify Apple fluid spring physics and timing tokens (SPEC-PET-TALK-004 Sec 3)."""
        data = self.spec
        motion = data.get("motionTokens")
        self.assertIsInstance(motion, dict, "Must export motionTokens dictionary")

        # Apple Fluid Spring Parameters
        self.assertEqual(motion.get("springStiffness"), 220.0, "Apple spring stiffness must be 220.0")
        self.assertEqual(motion.get("springDamping"), 21.0, "Apple spring damping must be 21.0")
        self.assertEqual(motion.get("springMass"), 1.0, "Apple spring mass must be 1.0")

        # Duration Tokens
        self.assertAlmostEqual(motion.get("dripDuration"), 0.22, places=2, msg="Drip entrance must be 220ms")
        self.assertAlmostEqual(motion.get("blossomDuration"), 0.24, places=2, msg="Island blossom must be 240ms")
        self.assertAlmostEqual(motion.get("stateTransitionDuration"), 0.16, places=2, msg="State transition must be 160ms")
        self.assertAlmostEqual(motion.get("suctionRetractionDuration"), 0.18, places=2, msg="Suction retraction must be 180ms")
        self.assertAlmostEqual(motion.get("reduceMotionDuration"), 0.08, places=2, msg="Reduce motion crossfade must be 80ms")

        # Error Shake Tokens
        self.assertAlmostEqual(motion.get("errorShakeDuration"), 0.12, places=2, msg="Error shake duration must be 120ms")
        self.assertEqual(motion.get("errorShakeAmplitude"), 3.0, "Error shake amplitude must be ±3px")
        self.assertEqual(motion.get("errorShakeCycles"), 3, "Error shake must be 3 cycles")

    def test_error_shake_capability(self):
        """Verify dynamic island error shake feedback capability is enabled."""
        data = self.spec
        self.assertTrue(data.get("errorShakeSupported"), "errorShakeSupported must be true")


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
        self.assertIn("[EXPANDED DICTATION]", res.stdout)
        self.assertIn("[SPEAKING] Liquid Silver (#cfd4dc)", res.stdout)
        self.assertIn("[ERROR SHAKE]", res.stdout)
        self.assertIn("PASS: HUD visual test sequence completed.", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
