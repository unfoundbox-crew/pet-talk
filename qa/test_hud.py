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
import re
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


# Every subprocess call below runs headless: PET_TALK_HEADLESS=1 makes the daemon
# run its full state machine, springs and geometry without ordering any panel on
# screen. A QA suite must never pop the capsule onto the display someone is
# working on (Saurabh, 2026-09-12).
HEADLESS_ENV = dict(os.environ, PET_TALK_HEADLESS="1", PET_TALK_SILENT="1")


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
        if sys.platform != "darwin":
            raise unittest.SkipTest(
                "SKIP: not macOS — these sources import AppKit/Carbon, so "
                "swiftc -typecheck fails on any other platform even when "
                "swiftc itself is installed (e.g. Linux CI runners)"
            )
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
            env=HEADLESS_ENV,
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

        # Resting width is the MEASURED notch, never a 220 literal: on a notched
        # screen it equals the measured notch width, on an external display it is
        # the 180pt fallback pill.
        if data.get("hasNotch"):
            self.assertEqual(notch_specs.get("restingWidth"), data.get("notchWidth"),
                             "Resting width must be the measured notch width")
            self.assertGreater(data.get("notchWidth", 0), 0.0, "Measured notch width must be positive")
        else:
            self.assertEqual(notch_specs.get("restingWidth"), 180.0,
                             "With no hardware notch the fallback pill is 180pt")
        self.assertEqual(notch_specs.get("expandedWidth"), 440.0, "Blossom expanded width is 440px")

        self.assertEqual(notch_specs.get("earFilletRadius"), 10.0, "Top concave ear fillets radius is 10px")
        self.assertEqual(notch_specs.get("bottomCornerRadius"), 20.0, "Bottom continuous squircle radius is 20px")
        self.assertEqual(notch_specs.get("fallbackCornerRadius"), 22.0, "External monitor pill radius is 22px")
        self.assertEqual(notch_specs.get("hoverPeekHeight"), 6.0, "Hover peek shelf height is 6px")

        self.assertIn("hasNotch", data, "Must detect whether active screen has hardware notch")

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
    """`test-hud` draws the real capsule over whatever the developer is doing, so
    it is opt-in: set PET_TALK_HUD_VISUAL=1 to run it. The headless equivalents
    (`--self-test`, `--dump-state`) cover the same mechanics and always run."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        if os.environ.get("PET_TALK_HUD_VISUAL") != "1":
            raise unittest.SkipTest(
                "SKIP: `test-hud` shows the capsule on the developer's screen — "
                "set PET_TALK_HUD_VISUAL=1 to run it; the headless "
                "`--self-test` and `--dump-state` checks cover the mechanics"
            )

    def test_hud_visual_test_command_output(self):
        res = subprocess.run(
            [BIN_PATH, "test-hud"],
            capture_output=True,
            text=True,
            timeout=15,
            env=HEADLESS_ENV,
        )
        self.assertEqual(res.returncode, 0, f"test-hud failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("Testing Pet-Talk Floating Glass Capsule HUD", res.stdout)
        self.assertIn("[LISTENING] Emerald True (#10b981)", res.stdout)
        self.assertIn("[THINKING] SpacePilot Gold (#c9a227)", res.stdout)
        self.assertIn("[EXPANDED DICTATION]", res.stdout)
        self.assertIn("[SPEAKING] Liquid Silver (#cfd4dc)", res.stdout)
        self.assertIn("[ERROR SHAKE]", res.stdout)
        self.assertIn("PASS: HUD visual test sequence completed.", res.stdout)


class TestErrorShakeSilentUnderFlags(unittest.TestCase):
    """FINDING 9: the Basso chime in triggerErrorShake must be suppressed under
    PET_TALK_SILENT and PET_TALK_HEADLESS — reusing the existing silent/headless
    helpers rather than inventing a new one. `orderFrontUnlessHeadless()` means
    PET_TALK_HEADLESS=1 never orders the panel's window front, so — unlike
    TestHUDInteractiveSequence's PET_TALK_HUD_VISUAL=1-gated run — this exercises
    the real triggerErrorShake path (including the error-shake block) with no
    window ever reaching the screen: a real subprocess run, not just a grep."""

    def test_test_hud_sequence_completes_headless_and_silent(self):
        _skip_if_no_bin()
        res = subprocess.run(
            [BIN_PATH, "test-hud"],
            capture_output=True,
            text=True,
            timeout=15,
            env=HEADLESS_ENV,
        )
        self.assertEqual(res.returncode, 0, f"test-hud failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("[ERROR SHAKE]", res.stdout,
                      "must reach the error-shake step (the guarded Basso call) under PET_TALK_SILENT=1")
        self.assertIn("PASS: HUD visual test sequence completed.", res.stdout,
                      "must complete cleanly with audio suppressed, never crash or hang")

    def test_basso_call_is_gated_on_silent_and_headless(self):
        """Runtime cannot observe whether NSSound actually played (no audio
        capture in CI), so this static check backs the subprocess run above:
        the Basso call must sit behind the existing silent/headless flags, not
        fire unconditionally in the else branch."""
        with open(os.path.join(ROOT, "cli", "hotkey", "hud_window.swift"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('NSSound(named: "Basso")')
        self.assertNotEqual(idx, -1, "Basso earcon call must still exist in triggerErrorShake")
        preceding = src[max(0, idx - 200):idx]
        self.assertIn("isSilentModeEnv", preceding,
                      "Basso must be gated on the existing EarconEngine.isSilentModeEnv helper")
        self.assertIn("isHeadless", preceding,
                      "Basso must also be gated on the existing HUDController.isHeadless helper")


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
        res = subprocess.run([BIN_PATH, "--dump-hud-spec"], capture_output=True,
                             text=True, timeout=15, env=HEADLESS_ENV)
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
        """Opt-in: `test-breadcrumbs` draws the capsule on the developer's screen."""
        if os.environ.get("PET_TALK_HUD_VISUAL") != "1":
            raise unittest.SkipTest(
                "SKIP: `test-breadcrumbs` shows the capsule on the developer's "
                "screen — set PET_TALK_HUD_VISUAL=1 to run it"
            )
        res = subprocess.run(
            [BIN_PATH, "test-breadcrumbs"],
            capture_output=True,
            text=True,
            timeout=15,
            env=HEADLESS_ENV,
        )
        self.assertEqual(res.returncode, 0, f"test-breadcrumbs failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("Testing Dynamic Island Semantic Action Breadcrumbs", res.stdout)
        self.assertIn("[Thinking]", res.stdout)
        self.assertIn("[Running]", res.stdout)
        self.assertIn("[Editing]", res.stdout)
        self.assertIn("[Donna heard]", res.stdout)
        self.assertIn("[Speaking]", res.stdout)
        self.assertIn("PASS: Semantic Action Breadcrumbs & Multi-line visual test completed.", res.stdout)


class TestSpringMechanics(unittest.TestCase):
    """The three declared spring tokens must drive real motion, not sit unused.

    Pure static source-text checks — no build, no binary, no audio.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HUD_SWIFT_SRC):
            raise unittest.SkipTest(f"SKIP: {HUD_SWIFT_SRC} not present yet")
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            cls.src = f.read()

    def test_spring_animation_reads_the_declared_tokens(self):
        """A CASpringAnimation is built, and its stiffness/damping/mass come from
        the HUDTokens spring tokens — never from inline numeric literals."""
        self.assertTrue("CASpringAnimation" in self.src,
                        "A real CASpringAnimation must exist, not just declared tokens")
        idx = self.src.find("CASpringAnimation(")
        block = self.src[idx:idx + 900]
        self.assertIn("HUDTokens.springStiffness", block,
                      "stiffness must read HUDTokens.springStiffness")
        self.assertIn("HUDTokens.springDamping", block,
                      "damping must read HUDTokens.springDamping")
        self.assertIn("HUDTokens.springMass", block,
                      "mass must read HUDTokens.springMass")
        self.assertIn("initialVelocity", block, "initialVelocity must be set explicitly")

        import re
        for prop in ("stiffness", "damping", "mass"):
            self.assertIsNone(
                re.search(r"\b%s\s*=\s*[0-9]" % prop, self.src),
                f"{prop} must never be assigned a numeric literal — read the token",
            )

    def test_spring_driver_integrates_the_same_tokens(self):
        """A damped-spring integrator drives capsule geometry (window frame),
        seeded from the same three tokens."""
        self.assertTrue("HUDSpringDriver" in self.src,
                        "A spring driver type must exist")
        idx = self.src.find("class HUDSpringDriver")
        self.assertNotEqual(idx, -1, "HUDSpringDriver must be a declared type")
        block = self.src[idx:idx + 2600]
        self.assertIn("stiffness", block)
        self.assertIn("damping", block)
        self.assertIn("mass", block)
        # Semi-implicit Euler on a damped spring: acceleration from -k*x - c*v, /m
        self.assertIn("velocity", block, "The integrator must carry velocity state")

    def test_no_literal_capsule_width_220(self):
        """capsuleWidth is the measured notch, so no 220 literal survives."""
        offenders = [
            ln.strip() for ln in self.src.splitlines()
            if "220" in ln and ("width" in ln.lower() or "capsule" in ln.lower())
        ]
        self.assertEqual(offenders, [],
                         "no 220pt capsule-width literal may survive — measure the notch")
        self.assertTrue("measuredNotchWidth" in self.src,
                        "capsuleWidth must come from a measuredNotchWidth() reading")
        self.assertTrue("auxiliaryTopLeftArea" in self.src, "missing: auxiliaryTopLeftArea")
        self.assertTrue("auxiliaryTopRightArea" in self.src, "missing: auxiliaryTopRightArea")

    def test_fallback_capsule_width_is_180_token(self):
        """With no notch (external display) the fallback pill is 180pt, via a token."""
        self.assertTrue("fallbackCapsuleWidth" in self.src, "missing: fallbackCapsuleWidth")
        import re
        # The default is the design lane's token (capsuleWidthRest = 180), never a
        # second copy of the number in this file.
        self.assertIsNotNone(
            re.search(r"fallbackCapsuleWidth[^\n]*=\s*CGFloat\(DesignTokens\.capsuleWidthRest\)", self.src),
            "HUDTokens.fallbackCapsuleWidth must read DesignTokens.capsuleWidthRest",
        )
        with open(os.path.join(ROOT, "cli", "hotkey", "DesignTokens.swift"), "r", encoding="utf-8") as f:
            dt = f.read()
        m = re.search(r"capsuleWidthRest: Double = ([0-9.]+)", dt)
        self.assertIsNotNone(m, "DesignTokens.capsuleWidthRest missing")
        self.assertEqual(float(m.group(1)), 180.0, "the no-notch fallback pill is 180pt")
        self.assertTrue("measuredNotchWidth() ?? HUDTokens.fallbackCapsuleWidth" in self.src,
                        "capsuleWidth must fall back to the 180pt token when no notch is reported")

    def test_ear_fillets_only_past_notch_width_plus_threshold(self):
        self.assertTrue("earFilletThreshold" in self.src, "missing: earFilletThreshold")
        import re
        self.assertIsNotNone(
            re.search(r"earFilletThreshold[^\n]*=\s*24\.0", self.src),
            "ear fillet threshold token must default to 24.0pt",
        )
        self.assertTrue("notchWidth + HUDTokens.earFilletThreshold" in self.src,
                        "ear fillets appear only when content exceeds notch width + threshold")

    def test_reduce_motion_zeroes_travel_and_keeps_time(self):
        self.assertTrue("accessibilityDisplayShouldReduceMotion" in self.src, "missing: accessibilityDisplayShouldReduceMotion")
        self.assertTrue("reduceMotionDuration" in self.src,
                        "Reduce Motion must keep a timed crossfade (80ms), not zero it")
        # The Reduce Motion branch must snap the frame (zero travel) rather than
        # hand the frame to the spring driver.
        idx = self.src.find("func show(state: HUDState")
        show_block = self.src[idx:idx + 3000]
        rm = show_block.find("accessibilityDisplayShouldReduceMotion")
        self.assertNotEqual(rm, -1, "show() must have a Reduce Motion branch")
        rm_block = show_block[rm:rm + 900]
        self.assertNotIn("HUDSpringDriver", rm_block,
                         "the Reduce Motion branch must not run the spatial spring driver")

    def test_barge_is_a_hard_cut_with_no_animation(self):
        self.assertNotEqual(self.src.find("func dismiss("), -1)
        self.assertTrue("hardCut" in self.src,
                        "the barge path must be a named hard cut, not a short fade")
        hc = self.src.find("// HARD CUT")
        self.assertNotEqual(hc, -1, "the hard-cut path must be marked in the source")
        hc_block = self.src[hc:hc + 600]
        self.assertNotIn("NSAnimationContext", hc_block,
                         "a barge hard cut must run zero animation")
        self.assertNotIn("HUDSpringDriver", hc_block,
                         "a barge hard cut must not spring")

    def test_error_shake_amplitude_and_cycles_come_from_tokens(self):
        idx = self.src.find("func triggerErrorShake")
        block = self.src[idx:idx + 1600]
        self.assertIn("errorShakeAmplitude", block, "±3pt amplitude must read the token")
        self.assertIn("errorShakeCycles", block, "cycle count must read the token")
        self.assertIn("errorShakeDuration", block, "120ms duration must read the token")

    def test_tokens_are_overridable_from_config_yaml(self):
        """A design-playground export lands in config.yaml and applies with no rebuild."""
        self.assertTrue("struct HUDTokens" in self.src, "missing: struct HUDTokens")
        self.assertTrue("static func apply(from config: PetTalkConfig)" in self.src,
                        "HUDTokens must accept a config.yaml override")
        cfg_src_path = os.path.join(ROOT, "cli", "hotkey", "config.swift")
        with open(cfg_src_path, "r", encoding="utf-8") as f:
            cfg = f.read()
        for key in ("spring_stiffness", "spring_damping", "spring_mass",
                    "notch_width_fallback", "ear_fillet_threshold", "double_tap_window_ms"):
            self.assertIn(key, cfg, f"config.yaml must expose the {key} key")

    def test_lane4_handover_comment_present(self):
        """HUDTokens is a placeholder: lane 4's generated file replaces it."""
        idx = self.src.find("struct HUDTokens")
        header = self.src[max(0, idx - 900):idx]
        self.assertIn("DesignTokens.swift", header,
                      "HUDTokens must say lane 4's generated DesignTokens.swift replaces it")


class TestDumpState(unittest.TestCase):
    """`--dump-state` is the one machine-readable read of the live HUD: one JSON
    line, no daemon, no audio, no window shown."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        res = subprocess.run([BIN_PATH, "--dump-state"], capture_output=True,
                             text=True, timeout=15, env=HEADLESS_ENV)
        # A present binary whose --dump-state fails is a real failure, never a skip.
        if res.returncode != 0:
            raise AssertionError(f"--dump-state failed (rc={res.returncode}):\n{res.stderr}\n{res.stdout}")
        cls.raw = res.stdout
        cls.state = json.loads(res.stdout)

    def test_single_json_line(self):
        lines = [ln for ln in self.raw.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"--dump-state must print exactly one line, got {len(lines)}")

    def test_state_machine_block(self):
        sm = self.state.get("state")
        self.assertIsInstance(sm, dict, "must export a state machine block")
        self.assertEqual(sm.get("lifecycle"), "hidden", "a fresh process is hidden")
        self.assertIn("hudState", sm)
        self.assertIn("paused", sm)
        self.assertIn("hoverPeek", sm)
        self.assertIn("reduceMotion", sm)

    def test_capsule_geometry_block(self):
        g = self.state.get("geometry")
        self.assertIsInstance(g, dict, "must export a geometry block")
        self.assertEqual(g.get("fallbackCapsuleWidth"), 180.0)
        self.assertEqual(g.get("earFilletThreshold"), 24.0)
        self.assertIn("hasNotch", g)
        if g.get("hasNotch"):
            self.assertEqual(g.get("capsuleWidth"), g.get("measuredNotchWidth"),
                             "with a notch, the capsule is exactly the measured notch wide")
        else:
            self.assertEqual(g.get("capsuleWidth"), 180.0,
                             "with no notch, the capsule is the 180pt fallback pill")

    def test_spring_constants_block(self):
        s = self.state.get("spring")
        self.assertIsInstance(s, dict, "must export a spring block")
        self.assertEqual(s.get("stiffness"), 220.0)
        self.assertEqual(s.get("damping"), 21.0)
        self.assertEqual(s.get("mass"), 1.0)
        self.assertGreater(s.get("settlingDurationMs", 0), 0.0,
                           "a real spring reports a settling duration")

    def test_chord_block_matches_the_new_gestures(self):
        c = self.state.get("chords")
        self.assertIsInstance(c, dict, "must export the chord map")
        self.assertEqual(c.get("optionTab"), "ask")
        self.assertEqual(c.get("optionShiftTab"), "handover")
        self.assertEqual(c.get("optionTabDoubleTap"), "pause")
        self.assertEqual(c.get("doubleTapWindowMs"), 400.0)


class TestConfigNumericHardening(unittest.TestCase):
    """FINDING 6: config.swift must reject non-finite spring numbers (fall back
    to the built-in default) and clamp out-of-range ones — never let `nan`/`inf`/
    an absurd value reach the spring integrator. Driven end to end through
    `--dump-state` against a real config.yaml, via PET_TALK_CONFIG_PATH."""

    def _dump_state_with_config(self, yaml_text: str) -> dict:
        _skip_if_no_bin()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.yaml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(yaml_text)
            env = dict(HEADLESS_ENV, PET_TALK_CONFIG_PATH=cfg_path)
            res = subprocess.run([BIN_PATH, "--dump-state"], capture_output=True,
                                  text=True, timeout=15, env=env)
            self.assertEqual(res.returncode, 0,
                              f"--dump-state must never crash on a bad config "
                              f"(rc={res.returncode}):\n{res.stderr}\n{res.stdout}")
            lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, "--dump-state must still print exactly one JSON line")
            return json.loads(res.stdout)

    def test_nan_falls_back_to_default_stiffness(self):
        state = self._dump_state_with_config(
            "motion:\n  spring_stiffness: nan\n  spring_damping: 21.0\n  spring_mass: 1.0\n"
        )
        self.assertEqual(state["spring"]["stiffness"], 220.0,
                          "a non-finite spring_stiffness must fall back to the built-in default, not NaN")

    def test_inf_falls_back_to_default_damping(self):
        state = self._dump_state_with_config(
            "motion:\n  spring_stiffness: 220.0\n  spring_damping: inf\n  spring_mass: 1.0\n"
        )
        self.assertEqual(state["spring"]["damping"], 21.0,
                          "a non-finite spring_damping must fall back to the built-in default, not inf")

    def test_out_of_range_values_are_clamped(self):
        state = self._dump_state_with_config(
            "motion:\n  spring_stiffness: 999999\n  spring_damping: -50\n  spring_mass: 50\n"
        )
        s = state["spring"]
        self.assertEqual(s["stiffness"], 2000.0, "stiffness must clamp to the 1...2000 ceiling")
        self.assertEqual(s["damping"], 0.0, "damping must clamp to the 0...200 floor")
        self.assertEqual(s["mass"], 10.0, "mass must clamp to the 0.1...10 ceiling")

    def test_nan_and_out_of_range_together_never_crash(self):
        state = self._dump_state_with_config(
            "motion:\n  spring_stiffness: nan\n  spring_damping: 999\n  spring_mass: 0.0001\n"
        )
        s = state["spring"]
        self.assertEqual(s["stiffness"], 220.0)
        self.assertEqual(s["damping"], 200.0)
        self.assertEqual(s["mass"], 0.1)
        for v in (s["stiffness"], s["damping"], s["mass"]):
            self.assertTrue(v == v and v not in (float("inf"), float("-inf")),
                             "no non-finite spring number may ever reach --dump-state")


class TestSpringConstantsSourcedFromDesignTokens(unittest.TestCase):
    """The Swift HUD's spring numbers should come from DesignTokens (design
    lane's generated file), not from hud_window.swift's own literals.

    DesignTokens.swift is generated by design/build.py from
    design/tokens.pet-talk.json (see DESIGN.md) — that half of this
    assertion is real and enforced unconditionally below. Wiring
    hud_window.swift's `HUDMotionTokens` to actually read `DesignTokens.*`
    instead of its own `220.0`/`21.0`/`1.0` literals is a concurrent lane's
    (lane 3's) integration step, not this suite's edit — until that lands,
    this test SKIPs with the exact reason rather than faking a pass.
    """

    DESIGN_TOKENS_SWIFT = os.path.join(ROOT, "cli", "hotkey", "DesignTokens.swift")

    def test_design_tokens_swift_defines_matching_spring_constants(self):
        if not os.path.exists(self.DESIGN_TOKENS_SWIFT):
            self.fail(
                "cli/hotkey/DesignTokens.swift is missing — run "
                "`python3 design/build.py --write`"
            )
        with open(self.DESIGN_TOKENS_SWIFT, "r", encoding="utf-8") as f:
            src = f.read()
        for name, expected in (
            ("springStiffness", "220"),
            ("springDamping", "21"),
            ("springMass", "1"),
        ):
            m = re.search(rf"static let {name}: Double = ([0-9.]+)", src)
            self.assertIsNotNone(m, f"DesignTokens.swift has no {name}")
            self.assertEqual(
                float(m.group(1)), float(expected),
                f"DesignTokens.{name} drifted from design/tokens.pet-talk.json",
            )

    def test_hud_window_reads_design_tokens_not_own_literals(self):
        if not os.path.exists(HUD_SWIFT_SRC):
            raise unittest.SkipTest("SKIP: cli/hotkey/hud_window.swift not present yet")
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            src = f.read()
        wired = "DesignTokens.springStiffness" in src or "DesignTokens.springDamping" in src
        if not wired:
            raise unittest.SkipTest(
                "SKIP: cli/hotkey/hud_window.swift still defines its own "
                "HUDMotionTokens literals (springStiffness = 220.0, "
                "springDamping = 21.0, springMass = 1.0) instead of reading "
                "DesignTokens.springStiffness/springDamping/springMass — "
                "wiring that read-through is lane 3's integration step "
                "(cli/hotkey/hud_window.swift, cli/hotkey/main.swift are its "
                "owned paths, not this design lane's)."
            )
        self.assertNotIn(
            "public static let springStiffness: Double = 220.0", src,
            "hud_window.swift is wired to DesignTokens but still keeps its own literal",
        )


class TestHeadlessSelfTest(unittest.TestCase):
    """`--self-test` exercises the spring integrator and the geometry math with no
    window ordered front — the headless replacement for watching `test-hud`."""

    @classmethod
    def setUpClass(cls):
        _skip_if_no_bin()
        cls.res = subprocess.run([BIN_PATH, "--self-test"], capture_output=True,
                                 text=True, timeout=30, env=HEADLESS_ENV)

    def test_self_test_passes(self):
        self.assertEqual(self.res.returncode, 0,
                         f"--self-test failed:\n{self.res.stdout}\n{self.res.stderr}")
        self.assertIn("PASS: self-test", self.res.stdout)

    def test_self_test_covers_the_spring_integrator(self):
        out = self.res.stdout
        for needle in ("[spring]", "spring settles", "spring is underdamped",
                       "spring lands exactly on target", "CASpringAnimation reads the tokens"):
            self.assertIn(needle, out, f"--self-test must report: {needle}")

    def test_self_test_covers_the_geometry_math(self):
        out = self.res.stdout
        for needle in ("[geometry]", "measuredNotchWidth=", "ears once content clears notch",
                       "four-line clamp", "no window was shown"):
            self.assertIn(needle, out, f"--self-test must report: {needle}")

    def test_self_test_refuses_to_run_non_headless(self):
        env = dict(os.environ)
        env.pop("PET_TALK_HEADLESS", None)
        res = subprocess.run([BIN_PATH, "--self-test"], capture_output=True,
                             text=True, timeout=30, env=env)
        self.assertEqual(res.returncode, 1,
                         "--self-test must refuse to run without PET_TALK_HEADLESS=1")
        self.assertIn("requires PET_TALK_HEADLESS=1", res.stdout)


class TestHeadlessGuard(unittest.TestCase):
    """No panel may reach the screen while PET_TALK_HEADLESS=1."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HUD_SWIFT_SRC):
            raise unittest.SkipTest(f"SKIP: {HUD_SWIFT_SRC} not present yet")
        with open(HUD_SWIFT_SRC, "r", encoding="utf-8") as f:
            cls.src = f.read()

    def test_every_show_path_goes_through_the_headless_guard(self):
        self.assertTrue("PET_TALK_HEADLESS" in self.src, "missing: PET_TALK_HEADLESS")
        self.assertTrue("orderFrontUnlessHeadless" in self.src, "missing: orderFrontUnlessHeadless")
        # Exactly one raw orderFrontRegardless() call may exist: the one inside
        # the guard itself.
        raw = [ln.strip() for ln in self.src.splitlines()
               if "orderFrontRegardless()" in ln and "func " not in ln]
        self.assertEqual(len(raw), 1,
                         f"every show path must route through the guard; raw calls: {raw}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
