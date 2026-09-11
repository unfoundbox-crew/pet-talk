"""Hermetic unit tests for Smallest.ai TTS and OpenAI gpt-5-nano tyres."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.providers import (
    SmallestAITTS,
    make_tts,
    make_llm,
    OpenAICompatibleLLM,
    ProviderError,
)


class TestSmallestAITTS(unittest.TestCase):
    """Test Smallest.ai Lightning TTS tyre."""

    def test_init_defaults(self):
        tts = SmallestAITTS(api_key="test_key_123")
        self.assertEqual(tts.api_key, "test_key_123")
        self.assertEqual(tts.voice_id, "meher")
        self.assertEqual(tts.model, "lightning_v3.1_pro")
        self.assertEqual(tts.sample_rate, 24000)

    def test_empty_text_raises(self):
        tts = SmallestAITTS(api_key="test_key_123")
        with self.assertRaises(ProviderError) as ctx:
            tts.synth("")
        self.assertEqual(ctx.exception.reason, "tts_empty_text")

    def test_no_key_raises(self):
        tts = SmallestAITTS(api_key="")
        tts.api_key = ""
        with patch.dict(os.environ, {"SMALLEST_API_KEY": ""}):
            with self.assertRaises(ProviderError) as ctx:
                tts.synth("Hello world")
            self.assertEqual(ctx.exception.reason, "tts_no_key")

    @patch("urllib.request.urlopen")
    def test_synth_successful_request(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        # 1000 bytes fake WAV
        mock_resp.read.return_value = b"RIFF" + b"\x00" * 996
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        tts = SmallestAITTS(api_key="sk_test_smallest")
        audio, mark_seq = tts.synth("Hello Donna, how are you today?", voice="meher")

        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertEqual(len(audio), 1000)
        self.assertEqual(mark_seq, [])

        # Check call arguments
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.smallest.ai/waves/v1/tts")
        self.assertEqual(req.headers["Authorization"], "Bearer sk_test_smallest")
        self.assertEqual(req.headers["Content-type"], "application/json")

    def test_factory_routing(self):
        tts = make_tts("smallest", api_key="sk_test_xyz")
        self.assertIsInstance(tts, SmallestAITTS)
        self.assertEqual(tts.api_key, "sk_test_xyz")

        tts2 = make_tts("smallest-ai", api_key="sk_test_xyz2")
        self.assertIsInstance(tts2, SmallestAITTS)

        tts3 = make_tts("waves", api_key="sk_test_xyz3")
        self.assertIsInstance(tts3, SmallestAITTS)


class TestOpenAIGPT5NanoTyre(unittest.TestCase):
    """Test OpenAI gpt-5-nano reasoning tyre."""

    def test_factory_routing(self):
        llm = make_llm("openai", model="gpt-5-nano", api_key="sk-test-openai")
        self.assertIsInstance(llm, OpenAICompatibleLLM)
        self.assertEqual(llm.model, "gpt-5-nano")
        self.assertEqual(llm.api_key, "sk-test-openai")

    def test_reasoning_payload_formatting(self):
        llm = OpenAICompatibleLLM(
            base_url="https://api.openai.com/v1",
            model="gpt-5-nano",
            api_key="sk-test-openai",
        )
        payload = llm._build_payload("You are Donna.", [("user", "Hello Donna")])

        # gpt-5-nano must use max_completion_tokens (not max_tokens) and omit temperature
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertGreaterEqual(payload["max_completion_tokens"], 300)

    def test_standard_model_payload_formatting(self):
        llm = OpenAICompatibleLLM(
            base_url="https://api.groq.com/openai/v1",
            model="groq/compound-mini",
            api_key="gsk_test",
        )
        payload = llm._build_payload("You are Donna.", [("user", "Hello Donna")])

        self.assertIn("max_tokens", payload)
        self.assertIn("temperature", payload)
        self.assertNotIn("max_completion_tokens", payload)


class TestHaikuAndOpenCodeTyres(unittest.TestCase):
    """Test Claude Haiku, OpenCode Zen, and Gemini Flash LLM tyres."""

    def test_haiku_factory_routing(self):
        llm = make_llm("haiku", api_key="sk-ant-test")
        self.assertIsInstance(llm, OpenAICompatibleLLM)
        self.assertEqual(llm.model, "claude-3-5-haiku-20241022")
        self.assertEqual(llm.api_key, "sk-ant-test")

    def test_opencode_zen_factory_routing(self):
        llm = make_llm("opencode", api_key="zen-key-test")
        self.assertIsInstance(llm, OpenAICompatibleLLM)
        self.assertEqual(llm.model, "flash-3.8")
        self.assertEqual(llm.base_url, "https://api.opencode.ai/v1")
        self.assertEqual(llm.api_key, "zen-key-test")

    def test_opencode_zen_alias_routing(self):
        llm = make_llm("zen", model="meta-spark-1.3", api_key="zen-key-test")
        self.assertIsInstance(llm, OpenAICompatibleLLM)
        self.assertEqual(llm.model, "meta-spark-1.3")
        self.assertEqual(llm.base_url, "https://api.opencode.ai/v1")

    def test_gemini_flash_factory_routing(self):
        llm = make_llm("gemini", api_key="gemini-key-test")
        self.assertIsInstance(llm, OpenAICompatibleLLM)
        self.assertEqual(llm.model, "gemini-2.5-flash")
        self.assertEqual(llm.base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(llm.api_key, "gemini-key-test")


class TestSessionGrounding(unittest.TestCase):
    """Test compact session grounding doesn't leak heredocs or blow up prompts."""

    def test_grounding_bounded_and_clean(self):
        from server.app import get_session_grounding

        grounding = get_session_grounding()
        self.assertIsInstance(grounding, str)
        # Must not contain bash heredocs or full file dumps
        self.assertNotIn("cat << 'EOF'", grounding)
        self.assertNotIn("# Pet-Talk — Full-Duplex", grounding)
        self.assertNotIn("# Persona: Donna", grounding)
        # Strictly bounded length (under 500 characters)
        self.assertLess(len(grounding), 500)


if __name__ == "__main__":
    unittest.main()

