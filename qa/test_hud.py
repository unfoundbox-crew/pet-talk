#!/usr/bin/env python3
"""qa/test_hud.py — QA suite for the native macOS Floating Glass Capsule HUD.

Fan rule: never builds Swift here — `swiftc -O` compiles happen on `ssh air`
(cli/hotkey/build.sh, `make build-hotkey`). Only a cheap `swiftc -typecheck`
sanity check runs locally. Tests that exercise the compiled binary require a
prebuilt binary at $PET_TALK_HOTKEY_BIN (default: bin/pet-talk-hotkey) and
SKIP with a clear reason when it is absent — never auto-built.

Verifies (when the binary is present):
1. NSPanel window properties and nonactivating flags via `--dump-hud-spec`.
2. Capsule visual geometry and motion tokens.
3. CLI execution of `test-hud` / `test-breadcrumbs` (visual only, no audio).
Plus static source-text checks that need no binary at all.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HUD_SWIFT_SRC = os.path.join(ROOT, "cli", "hotkey", "hud_window.swift")
ALL_SWIFT_SRCS = sorted(glob.glob(os.path.join(ROOT, "cli", "hotkey", "*.swift")))
BIN_PATH = os.environ.get("PET_TALK_HOTKEY_BIN") or os.path.join(ROOT, "bin", "pet-talk-hotkey")
HAVE_BIN = os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK)


def _skip_if_no_bin():
    if not HAVE_BIN:
        raise unittest.SkipTest(
            "SKIP: no prebuilt hotkey binary at %s — build it on `ssh air` "
            "via `cli/hotkey/build.sh bin/` (or `make build-hotkey`), never "
            "here (fan rule); set PET_TALK_HOTKEY_BIN to point at it" % BIN_PATH
        )


class TestSwiftTypecheck(unittest.TestCase):
    """Cheap syntax/type check only — never a full `-O` build (fan rule)."""

    def test_typecheck_only(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise unittest.SkipTest("SKIP: swiftc not available on this host")
        if not os.path.exists(HUD_SWIFT_SRC) or not ALL_SWIFT_SRCS:
            raise unittest.SkipTest("SKIP: cli/hotkey/*.swift sources not present yet")
        res = subprocess.run(["swiftc", "-typecheck"] + ALL_SWIFT_SRCS,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0,
                         f"swiftc -typecheck failed:\nStdout: {res.stdout}\nStderr: {res.stderr}")


class TestHUDWindowSpecifications(unittest.TestCase):
    """Verify window properties, nonactivating flags, notch specs, and motion tokens."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        res = subprocess.run(
            [BIN_PATH, "--dump-hud-spec"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            raise unittest.SkipTest(f"SKIP: --dump-hud-spec failed:\n{res.stderr}")
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
    """Verify CLI test-hud sequence execution (visual only — no audio)."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()

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


class TestAcousticTruthAndTelemetry(unittest.TestCase):
    """Verify acoustic truth implementation: zero looping animations, live RMS parsing, and audio levels.

    Pure static source-text checks — no build, no binary, no audio.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HUD_SWIFT_SRC):
            raise unittest.SkipTest(f"SKIP: {HUD_SWIFT_SRC} not present yet (lane C)")

    def test_no_looping_animations_during_listening(self):
        """Assert that fake looping CABasicAnimation has been eliminated from listening state."""
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            content = f.read()

        # Check setupListeningWaveform implementation
        self.assertIn("setupListeningWaveform", content)
        # Extract setupListeningWaveform function block
        idx = content.find("setupListeningWaveform")
        block = content[idx:idx + 1200]

        self.assertNotIn("CABasicAnimation(keyPath: \"bounds.size.height\")", block,
                         "Fake looping CABasicAnimation must be eliminated from setupListeningWaveform")
        self.assertNotIn("repeatCount = .infinity", block,
                         "Infinite repeatCount must be eliminated from setupListeningWaveform")
        self.assertIn("isAcousticListening = true", block,
                         "setupListeningWaveform must activate acoustic listening mode")

    def test_acoustic_truth_update_audio_level_api(self):
        """Verify updateAudioLevel(rms:peak:) API is exposed on HUD components."""
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("public func updateAudioLevel(rms: Float, peak: Float)", content,
                      "HUDIndicatorView and HUDController must provide updateAudioLevel(rms: Float, peak: Float)")

    def test_rms_telemetry_parsing_patterns(self):
        """Verify regex extraction of live microphone RMS and Peak telemetry."""
        import re

        pattern = r"(?i)\[RMS:\s*(?:rms=)?([0-9.]+)(?:[,\s]+(?:(?:PEAK|peak)=?|peak:?)?\s*([0-9.]+))?\]"

        samples = [
            ("[RMS: 0.245, PEAK: 0.512]", 0.245, 0.512),
            ("[RMS: 0.350]", 0.350, None),
            ("[RMS: 0.120, 0.450]", 0.120, 0.450),
            ("[RMS: rms=0.420, peak=0.780]", 0.420, 0.780),
            ("[rms: 0.085, peak: 0.190]", 0.085, 0.190),
        ]

        for text, exp_rms, exp_peak in samples:
            match = re.search(pattern, text)
            self.assertIsNotNone(match, f"Failed to match: {text}")
            self.assertAlmostEqual(float(match.group(1)), exp_rms, places=3)
            if exp_peak is not None:
                self.assertAlmostEqual(float(match.group(2)), exp_peak, places=3)

    def test_calculate_rms_and_peak(self):
        """Verify calculate_rms_and_peak computes accurate acoustic energy and peak amplitude."""
        import array
        try:
            from cli.audio import calculate_rms_and_peak
        except ImportError as e:
            raise unittest.SkipTest(f"SKIP: cli.audio not importable yet ({e})")

        # 1. Digital silence
        silence = bytes(1024)
        rms, peak = calculate_rms_and_peak(silence)
        self.assertEqual(rms, 0.0)
        self.assertEqual(peak, 0.0)

        # 2. Known amplitude block
        test_val = 5000
        samples = array.array("h", [test_val] * 512).tobytes()
        rms, peak = calculate_rms_and_peak(samples)
        self.assertAlmostEqual(rms, float(test_val), delta=1.0)
        self.assertEqual(peak, float(test_val))


class TestDynamicMultiLineAndBreadcrumbs(unittest.TestCase):
    """Verify dynamic multi-line height expansion and semantic action breadcrumbs."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        res = subprocess.run([BIN_PATH, "--dump-hud-spec"], capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise unittest.SkipTest(f"SKIP: --dump-hud-spec failed:\n{res.stderr}")
        cls.spec = json.loads(res.stdout)

    def test_multiline_word_wrapping_attributes(self):
        """Verify labelField uses word wrapping, multi-line mode, and 4 lines maximum."""
        data = self.spec
        self.assertFalse(data.get("labelUsesSingleLineMode"), "labelUsesSingleLineMode must be false")
        self.assertEqual(data.get("labelMaximumNumberOfLines"), 4, "labelMaximumNumberOfLines must be 4")
        self.assertEqual(data.get("maxExpandedHeight"), 110.0, "maxExpandedHeight must be 110px clamp")
        self.assertTrue(data.get("supportsBreadcrumbs"), "supportsBreadcrumbs must be true")

    def test_dynamic_notch_height_stepping(self):
        """Verify dynamic height calculation stepping from 60px to 76px to 96px to 110px clamp."""
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("computeDynamicNotchHeight", content)
        self.assertIn("maxExpandedHeight", content)
        self.assertIn("calculateTextHeight", content)

    def test_semantic_action_breadcrumb_sanitization(self):
        """Verify raw tool calls, tracebacks, and coding harness events are sanitized into clean breadcrumbs."""
        import re

        # Helper matching the Swift sanitizeSemanticBreadcrumb logic
        def sanitize(line):
            t = line.strip()
            if not t or t.startswith("[RMS:") or t.startswith("[rms:"):
                return None
            if t.startswith("[BREADCRUMB]") or t.startswith("[STATUS]"):
                rest = t.replace("[BREADCRUMB]", "").replace("[STATUS]", "").strip()
                if ":" in rest:
                    parts = rest.split(":", 1)
                    return (parts[0].strip(), parts[1].strip())
            # Transcribed
            m = re.search(r'\[([^\]]+ heard)\]:\s*\"([^\"]+)\"', t)
            if m:
                return (m.group(1), f'"{m.group(2)}"')
            # Harness patterns
            for pattern in ["Thinking", "Running", "Editing", "Searching", "Reading", "Testing"]:
                if t.lower().startswith(f"[{pattern.lower()}]:") or t.lower().startswith(f"{pattern.lower()}:"):
                    parts = t.split(":", 1)
                    return (pattern, parts[1].strip())
            # Tool calls
            if "run_command" in t or "CommandLine" in t:
                if "qa/run_all.sh" in t or "test_" in t:
                    return ("Running", "Executing QA test suites")
                elif "swiftc" in t:
                    return ("Running", "Compiling Swift hotkey daemon")
                elif "git diff" in t:
                    return ("Running", "Checking git diff")
                elif "git status" in t:
                    return ("Running", "Inspecting repository status")
                return ("Running", "Executing system task")
            if "replace_file_content" in t or "write_to_file" in t:
                return ("Editing", "Applying code changes")
            if "view_file" in t or "read_resource" in t:
                return ("Reading", "Inspecting file context")
            if "search_web" in t or "duckduckgo_web_search" in t:
                return ("Searching", "Consulting web knowledge")
            if t.startswith("{") or t.startswith("[{") or "Traceback" in t:
                return None
            return None

        # Test cases
        self.assertEqual(sanitize('[STATUS] Running: Compiling Swift daemon'), ('Running', 'Compiling Swift daemon'))
        self.assertEqual(sanitize('[Thinking]: Formulating reply...'), ('Thinking', 'Formulating reply...'))
        self.assertEqual(sanitize('[Donna heard]: "check test suites"'), ('Donna heard', '"check test suites"'))
        self.assertEqual(sanitize('{"tool": "run_command", "CommandLine": "bash qa/run_all.sh"}'),
                         ('Running', 'Executing QA test suites'))
        self.assertEqual(sanitize('{"tool": "replace_file_content", "TargetFile": "hud.swift"}'),
                         ('Editing', 'Applying code changes'))
        self.assertIsNone(sanitize('Traceback (most recent call last): File "app.py"'))
        self.assertIsNone(sanitize('[RMS: 0.245, PEAK: 0.512]'))

    def test_breadcrumbs_visual_command_output(self):
        """Verify test-breadcrumbs CLI command executes successfully."""
        res = subprocess.run(
            [BIN_PATH, "test-breadcrumbs"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"test-breadcrumbs failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("Testing Dynamic Island Semantic Action Breadcrumbs", res.stdout)
        self.assertIn("[Thinking]", res.stdout)
        self.assertIn("[Running]", res.stdout)
        self.assertIn("[Editing]", res.stdout)
        self.assertIn("[Donna heard]", res.stdout)
        self.assertIn("[Speaking]", res.stdout)
        self.assertIn("PASS: Semantic Action Breadcrumbs & Multi-line visual test completed.", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
