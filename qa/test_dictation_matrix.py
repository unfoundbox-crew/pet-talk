#!/usr/bin/env python3
"""qa/test_dictation_matrix.py — TDD test suite for Universal Dictation Matrix & Clean Prose Router.

Tests all STT provider adapters using pure-math synthetic PCM16 fixtures:
- DeepgramSTT (Cloud flagship Nova-3)
- GroqSTT (Ultra-fast Groq LPU Whisper)
- OpenAIWhisperSTT (OpenAI Whisper API)
- WhisperKitSTT (Apple Neural Engine CoreML via CLI/process)
- MLXWhisperSTT (Apple Silicon GPU via MLX)
- SenseVoiceSTT (Sovereign Fleet HTTP adapter :8086)
- StubSTT (Zero-cost mock)

Tests CleanProseFormatter:
- Strips filler tokens: "uh", "um", "like", "you know", "er", "ah"
- Cleans repeated stutter words ("the the" -> "the")
- Preserves and formats technical syntax (app.py, git commit -m, CamelCase, snake_case)
- Auto-capitalizes and fixes sentence punctuation

Tests make_stt() factory and fail-closed error handling.
"""
from __future__ import annotations

import io
import json
import math
import os
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.dictation import CleanProseFormatter
from server.providers import (
    DeepgramSTT,
    GroqSTT,
    MLXWhisperSTT,
    OpenAIWhisperSTT,
    ProviderError,
    SenseVoiceSTT,
    StubSTT,
    WhisperKitSTT,
    make_stt,
    pcm16_to_wav_bytes,
)


def make_synthetic_pcm16(
    duration_s: float = 0.5, freq_hz: float = 440.0, sample_rate: int = 16000
) -> bytes:
    """Deterministic synthetic PCM16 mono fixture — pure math, zero dependencies."""
    n = int(duration_s * sample_rate)
    buf = bytearray()
    for i in range(n):
        sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        buf.extend(struct.pack("<h", sample))
    return bytes(buf)


class TestCleanProseFormatter(unittest.TestCase):
    """Unit tests for the Wispr Flow style Clean Prose engine."""

    def test_strip_basic_fillers(self):
        cases = [
            ("uh um I think we should start", "I think we should start."),
            ("er ah Donna is ready", "Donna is ready."),
            ("we need you know to deploy now", "We need to deploy now."),
            ("uh, um, er, ah, you know", ""),
        ]
        for raw, expected in cases:
            self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_filler_like_vs_grammatical_like(self):
        # Hesitation "like" should be stripped
        self.assertEqual(
            CleanProseFormatter.format("like we need to build this feature"),
            "We need to build this feature.",
        )
        self.assertEqual(
            CleanProseFormatter.format("I was like thinking about the plan"),
            "I was thinking about the plan.",
        )
        # Valid grammatical usages of "like" must be preserved!
        self.assertEqual(
            CleanProseFormatter.format("I like python and Rust"),
            "I like python and Rust.",
        )
        self.assertEqual(
            CleanProseFormatter.format("I would like to update app.py"),
            "I would like to update app.py.",
        )

    def test_clean_repeated_stutter_words(self):
        cases = [
            ("the the bug is fixed", "The bug is fixed."),
            ("we we we are ready", "We are ready."),
            ("in in the the codebase", "In the codebase."),
            ("The the system is live", "The system is live."),
        ]
        for raw, expected in cases:
            self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_format_technical_file_extensions(self):
        cases = [
            ("check app dot py for errors", "Check app.py for errors."),
            ("update main dot ts and index dot html", "Update main.ts and index.html."),
            ("edit package dot json", "Edit package.json."),
            ("app.py is ready", "app.py is ready."),  # preserve filename casing at start
        ]
        for raw, expected in cases:
            self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_format_technical_git_syntax(self):
        cases = [
            ("run git commit dash m and push", "Run git commit -m and push."),
            ("execute git checkout dash b new-feature", "Execute git checkout -b new-feature."),
            ("git commit -m is preserved", "Git commit -m is preserved."),
        ]
        for raw, expected in cases:
            self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_format_spoken_snake_case_and_camel_case(self):
        cases = [
            ("call make underscore stt immediately", "Call make_stt immediately."),
            ("configure stt underscore provider in settings", "Configure stt_provider in settings."),
            ("SettingsModal and PromptComposer are intact", "SettingsModal and PromptComposer are intact."),
        ]
        for raw, expected in cases:
            self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_auto_capitalization_and_punctuation(self):
        # Statement -> period
        self.assertEqual(
            CleanProseFormatter.format("this is a high performance audio pipeline"),
            "This is a high performance audio pipeline.",
        )
        # Question -> question mark
        self.assertEqual(
            CleanProseFormatter.format("what is the latency budget for stt"),
            "What is the latency budget for stt?",
        )
        self.assertEqual(
            CleanProseFormatter.format("can you run the test suite"),
            "Can you run the test suite?",
        )
        self.assertEqual(
            CleanProseFormatter.format("how does failover work"),
            "How does failover work?",
        )

    def test_combined_complex_prose(self):
        raw = "uh um like we need to fix the the bug in app dot py and run git commit dash m you know"
        expected = "We need to fix the bug in app.py and run git commit -m."
        self.assertEqual(CleanProseFormatter.format(raw), expected)

    def test_empty_and_whitespace_input(self):
        self.assertEqual(CleanProseFormatter.format(""), "")
        self.assertEqual(CleanProseFormatter.format("    \n\t  "), "")


class TestSTTProviders(unittest.TestCase):
    """Unit tests for all swappable STT provider adapters."""

    def setUp(self):
        self.pcm16 = make_synthetic_pcm16(duration_s=0.2, freq_hz=440.0)

    # 1. StubSTT
    def test_stub_stt(self):
        stt = StubSTT(fixed_text="custom stub output")
        self.assertEqual(stt.transcribe(self.pcm16), "custom stub output")

    def test_stub_stt_empty_audio(self):
        stt = StubSTT()
        with self.assertRaises(ProviderError) as cm:
            stt.transcribe(b"")
        self.assertEqual(cm.exception.reason, "stt_empty_audio")

    # 2. DeepgramSTT
    def test_deepgram_stt_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            stt = DeepgramSTT(api_key="")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_no_key")

    def test_deepgram_stt_success(self):
        fake_resp = json.dumps({
            "results": {
                "channels": [
                    {"alternatives": [{"transcript": "hello from deepgram nova 3"}]}
                ]
            }
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            stt = DeepgramSTT(api_key="dg_test_key_123")
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "hello from deepgram nova 3")

    def test_deepgram_stt_empty_result(self):
        fake_resp = json.dumps({
            "results": {"channels": [{"alternatives": [{"transcript": ""}]}]}
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            stt = DeepgramSTT(api_key="dg_test_key_123")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_empty_result")

    # 3. GroqSTT
    def test_groq_stt_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            stt = GroqSTT(api_key="")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_no_key")

    def test_groq_stt_success(self):
        fake_resp = json.dumps({"text": "ultra fast groq whisper transcription"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            stt = GroqSTT(api_key="gsk_test_key_123")
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "ultra fast groq whisper transcription")

    def test_groq_stt_network_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            stt = GroqSTT(api_key="gsk_test_key_123")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_request_failed")

    # 4. OpenAIWhisperSTT
    def test_openai_whisper_stt_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            stt = OpenAIWhisperSTT(api_key="")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_no_key")

    def test_openai_whisper_stt_success(self):
        fake_resp = json.dumps({"text": "openai cloud whisper output"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            stt = OpenAIWhisperSTT(api_key="sk_test_key_123")
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "openai cloud whisper output")

    # 5. WhisperKitSTT (Apple Neural Engine)
    def test_whisperkit_not_found(self):
        with patch("shutil.which", return_value=None), patch("os.path.exists", return_value=False):
            stt = WhisperKitSTT(cli_path="/nonexistent/whisperkit-cli")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_whisperkit_not_found")

    def test_whisperkit_success(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"text": "apple neural engine whisperkit transcription"})
        mock_proc.stderr = ""

        with patch("shutil.which", return_value="/usr/local/bin/whisperkit-cli"), \
             patch("subprocess.run", return_value=mock_proc):
            stt = WhisperKitSTT()
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "apple neural engine whisperkit transcription")

    def test_whisperkit_process_failure(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "CoreML runtime error"

        with patch("shutil.which", return_value="/usr/local/bin/whisperkit-cli"), \
             patch("subprocess.run", return_value=mock_proc):
            stt = WhisperKitSTT()
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_whisperkit_failed")

    # 6. MLXWhisperSTT (Apple Silicon GPU)
    def test_mlx_not_installed(self):
        with patch.dict(sys.modules, {"mlx_whisper": None}):
            stt = MLXWhisperSTT()
            # If mlx_whisper is not installed or import fails
            with patch("builtins.__import__", side_effect=ImportError("No module named 'mlx_whisper'")):
                with self.assertRaises(ProviderError) as cm:
                    stt.transcribe(self.pcm16)
                self.assertEqual(cm.exception.reason, "stt_mlx_not_installed")

    def test_mlx_success(self):
        mock_mlx = MagicMock()
        mock_mlx.transcribe.return_value = {"text": "apple silicon gpu mlx transcription"}

        with patch.dict(sys.modules, {"mlx_whisper": mock_mlx}):
            stt = MLXWhisperSTT()
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "apple silicon gpu mlx transcription")

    # 7. SenseVoiceSTT (Sovereign Fleet)
    def test_sensevoice_success(self):
        fake_resp = json.dumps({"text": "sovereign fleet sensevoice transcription"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            stt = SenseVoiceSTT(base_url="http://127.0.0.1:8086")
            text = stt.transcribe(self.pcm16)
            self.assertEqual(text, "sovereign fleet sensevoice transcription")

    def test_sensevoice_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            stt = SenseVoiceSTT(base_url="http://127.0.0.1:8086")
            with self.assertRaises(ProviderError) as cm:
                stt.transcribe(self.pcm16)
            self.assertEqual(cm.exception.reason, "stt_request_failed")


class TestSTTFactoryAndResolution(unittest.TestCase):
    """Unit tests for make_stt() factory and provider fail-closed resolution."""

    def test_factory_stub(self):
        stt = make_stt("stub")
        self.assertIsInstance(stt, StubSTT)

    def test_factory_deepgram(self):
        stt = make_stt("deepgram", api_key="dg_test")
        self.assertIsInstance(stt, DeepgramSTT)

    def test_factory_groq(self):
        stt = make_stt("groq", api_key="gsk_test")
        self.assertIsInstance(stt, GroqSTT)

    def test_factory_openai(self):
        stt = make_stt("openai", api_key="sk_test")
        self.assertIsInstance(stt, OpenAIWhisperSTT)
        stt_alias = make_stt("openai-whisper", api_key="sk_test")
        self.assertIsInstance(stt_alias, OpenAIWhisperSTT)

    def test_factory_whisperkit(self):
        stt = make_stt("whisperkit")
        self.assertIsInstance(stt, WhisperKitSTT)
        stt_alias = make_stt("coreml")
        self.assertIsInstance(stt_alias, WhisperKitSTT)

    def test_factory_mlx(self):
        stt = make_stt("mlx")
        self.assertIsInstance(stt, MLXWhisperSTT)
        stt_alias = make_stt("mlx-whisper")
        self.assertIsInstance(stt_alias, MLXWhisperSTT)

    def test_factory_sensevoice(self):
        stt = make_stt("sensevoice")
        self.assertIsInstance(stt, SenseVoiceSTT)
        stt_alias = make_stt("fleet")
        self.assertIsInstance(stt_alias, SenseVoiceSTT)

    def test_factory_unknown_provider_raises(self):
        with self.assertRaises(ProviderError) as cm:
            make_stt("nonexistent_unknown_provider")
        self.assertEqual(cm.exception.reason, "stt_unknown_provider")

    def test_pcm16_to_wav_bytes(self):
        pcm = make_synthetic_pcm16(duration_s=0.1, sample_rate=16000)
        wav = pcm16_to_wav_bytes(pcm, sample_rate=16000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])


if __name__ == "__main__":
    unittest.main()
