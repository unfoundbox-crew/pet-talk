#!/usr/bin/env python3
"""qa/test_providers.py — hermetic unit tests for the server.providers package.

Covers the lane-B fixes: no hardcoded secrets, the deduped sentence
splitter (and the no-httpx NameError it used to hit), fail-closed unknown
providers / missing keys, configurable timeouts, real vs. estimated
word_times, and secret-safe reprs. No network calls — every HTTP boundary
is mocked.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import providers
from server.providers import (
    DeepgramSTT,
    ElevenLabsTTS,
    GroqSTT,
    KokoroSpacePilotTTS,
    OpenAICompatibleLLM,
    ProviderError,
    StubSTT,
    StubTTS,
    estimate_word_times,
    make_llm,
    make_stt,
    make_tts,
    pcm_duration_ms,
    split_sentences,
)
from server.providers.tts import _word_times_from_char_alignment


class TestPackageShape(unittest.TestCase):
    """`server/providers.py` became a package; every public name must still
    resolve exactly as it did as a flat module."""

    def test_all_exported_names_resolve(self):
        for name in providers.__all__:
            self.assertTrue(hasattr(providers, name), f"providers.{name} missing")

    def test_from_server_import_providers_attribute_access(self):
        self.assertIs(providers.KokoroSpacePilotTTS, KokoroSpacePilotTTS)


class TestPcmDurationMs(unittest.TestCase):
    def test_known_duration(self):
        wav = providers.tts._sine_wav_bytes(duration_s=0.5, sample_rate=22050)
        self.assertEqual(pcm_duration_ms(wav), 500)

    def test_empty_bytes(self):
        self.assertEqual(pcm_duration_ms(b""), 0)

    def test_malformed_bytes(self):
        self.assertEqual(pcm_duration_ms(b"RIFF" + b"\x00" * 20), 0)


class TestEstimateWordTimes(unittest.TestCase):
    def test_proportional_by_length(self):
        times = estimate_word_times("a bb ccc", 1000)
        self.assertEqual([t["word"] for t in times], ["a", "bb", "ccc"])
        self.assertTrue(all(t["estimated"] is True for t in times))
        # Monotonic, non-overlapping, and covers the full duration.
        self.assertEqual(times[0]["start_ms"], 0)
        self.assertEqual(times[-1]["end_ms"], 1000)
        for prev, nxt in zip(times, times[1:]):
            self.assertLessEqual(prev["end_ms"], nxt["start_ms"])
        # Longer words get a longer share.
        self.assertGreater(times[2]["end_ms"] - times[2]["start_ms"], times[0]["end_ms"] - times[0]["start_ms"])

    def test_empty_text_or_zero_duration(self):
        self.assertEqual(estimate_word_times("", 1000), [])
        self.assertEqual(estimate_word_times("hello", 0), [])


class TestSplitSentences(unittest.TestCase):
    def test_basic_terminal_punctuation(self):
        sentences, remainder = split_sentences("Hello world. How are you? Great!")
        self.assertEqual(sentences, ["Hello world.", "How are you?", "Great!"])
        self.assertEqual(remainder, "")

    def test_incomplete_remainder_kept(self):
        sentences, remainder = split_sentences("Hello world. This is incompl")
        self.assertEqual(sentences, ["Hello world."])
        self.assertEqual(remainder, "This is incompl")

    def test_semicolon_split_needs_five_words(self):
        sentences, remainder = split_sentences("one two three four five; more text")
        self.assertEqual(sentences, ["one two three four five;"])
        self.assertEqual(remainder, "more text")

    def test_comma_split_needs_eight_words(self):
        text = "one two three four five six seven eight, nine ten"
        sentences, remainder = split_sentences(text)
        self.assertEqual(sentences, ["one two three four five six seven eight,"])
        self.assertEqual(remainder, "nine ten")

    def test_no_premature_split_before_threshold(self):
        sentences, remainder = split_sentences("one two, three")
        self.assertEqual(sentences, [])
        self.assertEqual(remainder, "one two, three")


class TestSecretSafeRepr(unittest.TestCase):
    def test_groq_stt_repr_hides_key(self):
        stt = GroqSTT(api_key="super-secret-value")
        self.assertNotIn("super-secret-value", repr(stt))

    def test_openai_compatible_llm_repr_hides_key(self):
        llm = OpenAICompatibleLLM(api_key="super-secret-value")
        self.assertNotIn("super-secret-value", repr(llm))

    def test_kokoro_repr_hides_token(self):
        tts = KokoroSpacePilotTTS()
        tts._token = "super-secret-token"
        self.assertNotIn("super-secret-token", repr(tts))


class TestNoHardcodedSecret(unittest.TestCase):
    def test_secret_string_absent_from_source(self):
        pkg_dir = os.path.join(ROOT, "server", "providers")
        for fname in os.listdir(pkg_dir):
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(pkg_dir, fname)) as f:
                contents = f.read()
            self.assertNotIn("sk-3340dc7a", contents, f"hardcoded secret found in {fname}")


class TestMakeLlmMissingKey(unittest.TestCase):
    def _no_keys_env(self):
        return patch.dict(os.environ, {}, clear=True)

    def test_haiku_missing_key_raises(self):
        with self._no_keys_env():
            with self.assertRaises(ProviderError) as cm:
                make_llm("haiku")
            self.assertEqual(cm.exception.reason, "missing_api_key")
            self.assertEqual(cm.exception.detail, "ANTHROPIC_API_KEY")

    def test_groq_missing_key_raises(self):
        with self._no_keys_env():
            with self.assertRaises(ProviderError) as cm:
                make_llm("groq")
            self.assertEqual(cm.exception.reason, "missing_api_key")

    def test_openai_missing_key_raises(self):
        with self._no_keys_env():
            with self.assertRaises(ProviderError) as cm:
                make_llm("openai")
            self.assertEqual(cm.exception.reason, "missing_api_key")

    def test_litellm_missing_key_raises(self):
        with self._no_keys_env():
            with self.assertRaises(ProviderError) as cm:
                make_llm("litellm")
            self.assertEqual(cm.exception.reason, "missing_api_key")
            self.assertEqual(cm.exception.detail, "LITELLM_MASTER_KEY")

    def test_key_present_constructs_fine(self):
        with self._no_keys_env():
            llm = make_llm("haiku", api_key="sk-ant-real")
            self.assertIsInstance(llm, OpenAICompatibleLLM)
            self.assertEqual(llm.api_key, "sk-ant-real")


class TestUnknownProviderFailsClosed(unittest.TestCase):
    def test_make_llm_unknown_raises(self):
        with self.assertRaises(ProviderError) as cm:
            make_llm("definitely-not-a-real-provider")
        self.assertEqual(cm.exception.reason, "llm_unknown_provider")

    def test_make_tts_unknown_raises(self):
        with self.assertRaises(ProviderError) as cm:
            make_tts("definitely-not-a-real-provider")
        self.assertEqual(cm.exception.reason, "tts_unknown_provider")

    def test_make_stt_unknown_raises(self):
        with self.assertRaises(ProviderError) as cm:
            make_stt("definitely-not-a-real-provider")
        self.assertEqual(cm.exception.reason, "stt_unknown_provider")


class TestNoSilentHostSwap(unittest.TestCase):
    def test_unreachable_host_raises_provider_unreachable(self):
        # A closed local port: httpx fails to connect, the provider reports a
        # named provider_unreachable and never rewrites the base_url.
        llm = OpenAICompatibleLLM(base_url="http://127.0.0.1:9/v1", model="x", api_key="k")

        async def run():
            async for _ in llm.stream([{"role": "user", "content": "hi"}]):
                pass

        with self.assertRaises(ProviderError) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.reason, "provider_unreachable")
        self.assertEqual(llm.base_url, "http://127.0.0.1:9/v1")

    def test_no_fleet_host_in_defaults(self):
        llm = OpenAICompatibleLLM(model="x", api_key="k")
        self.assertNotIn("100.99.", llm.base_url)


class _FakeChunk:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=content))]


class TestNoHttpxPath(unittest.TestCase):
    """Regression test for the NameError: no-httpx branch referenced an
    `is_reasoning` name that only ever existed inside `_build_payload`."""

    def test_stream_without_httpx_uses_openai_sdk_and_splits_sentences(self):
        llm = OpenAICompatibleLLM(base_url="http://127.0.0.1:9999/v1", model="test-model", api_key="k")

        async def fake_chunks():
            for piece in ["Hello world. ", "Second sentence."]:
                yield _FakeChunk(piece)

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_chunks())
        fake_openai_module = types.SimpleNamespace(AsyncOpenAI=MagicMock(return_value=mock_client))

        async def run():
            with patch.dict(sys.modules, {"httpx": None, "openai": fake_openai_module}):
                out = []
                async for sentence in llm.stream([{"role": "user", "content": "hi"}]):
                    out.append(sentence)
                return out

        sentences = asyncio.run(run())
        self.assertEqual(sentences, ["Hello world.", "Second sentence."])

    def test_reasoning_model_uses_build_payload_no_duplication(self):
        # _build_payload is the single source of truth for max_tokens /
        # max_completion_tokens selection; the no-httpx branch must reuse
        # it rather than re-deriving is_reasoning itself.
        llm = OpenAICompatibleLLM(base_url="http://x", model="o1-preview", api_key="k")
        payload = llm._build_payload([{"role": "user", "content": "hi"}])
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)


class TestMaxTokensConfigurable(unittest.TestCase):
    def test_default_is_120(self):
        with patch.dict(os.environ, {}, clear=True):
            llm = OpenAICompatibleLLM(api_key="k")
            self.assertEqual(llm.max_tokens, 120)

    def test_env_override(self):
        with patch.dict(os.environ, {"LLM_MAX_TOKENS": "40"}, clear=True):
            llm = OpenAICompatibleLLM(api_key="k")
            self.assertEqual(llm.max_tokens, 40)

    def test_constructor_arg_wins(self):
        with patch.dict(os.environ, {"LLM_MAX_TOKENS": "40"}, clear=True):
            llm = OpenAICompatibleLLM(api_key="k", max_tokens=77)
            self.assertEqual(llm.max_tokens, 77)


class TestKokoroTimeout(unittest.TestCase):
    def test_default_timeout_is_15s(self):
        with patch.dict(os.environ, {}, clear=True):
            tts = KokoroSpacePilotTTS()
            self.assertEqual(tts.timeout_s, 15.0)

    def test_env_override(self):
        with patch.dict(os.environ, {"KOKORO_TIMEOUT_S": "3"}, clear=True):
            tts = KokoroSpacePilotTTS()
            self.assertEqual(tts.timeout_s, 3.0)

    def test_job_timeout_raises_named_error(self):
        tts = KokoroSpacePilotTTS(timeout_s=0.05)

        def fake_request(method, path, payload=None):
            if method == "POST":
                return {"job_id": "job-1"}
            return {"status": "running"}

        with patch.object(tts, "_request", side_effect=fake_request), patch("time.sleep"):
            with self.assertRaises(ProviderError) as cm:
                tts.synth("hello world")
            self.assertEqual(cm.exception.reason, "tts_job_timeout")


class TestStubDelay(unittest.TestCase):
    def test_stub_stt_delay(self):
        stt = StubSTT(delay_s=0.05)
        import time

        start = time.monotonic()
        stt.transcribe(b"\x00\x00")
        self.assertGreaterEqual(time.monotonic() - start, 0.04)

    def test_stub_tts_delay(self):
        tts = StubTTS(delay_s=0.05)
        import time

        start = time.monotonic()
        wav, word_times = tts.synth("hello world")
        self.assertGreaterEqual(time.monotonic() - start, 0.04)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertTrue(all(w["estimated"] for w in word_times))


class TestElevenLabsWordTimes(unittest.TestCase):
    def test_char_alignment_grouped_into_words(self):
        text = "hi bob"
        alignment = {
            "characters": list(text),
            "character_start_times_seconds": [0.0, 0.1, 0.3, 0.4, 0.5, 0.6],
            "character_end_times_seconds": [0.1, 0.2, 0.4, 0.5, 0.6, 0.7],
        }
        words = _word_times_from_char_alignment(alignment)
        self.assertEqual([w["word"] for w in words], ["hi", "bob"])
        self.assertTrue(all(w["estimated"] is False for w in words))
        self.assertEqual(words[0]["start_ms"], 0)
        self.assertEqual(words[0]["end_ms"], 200)
        self.assertEqual(words[1]["start_ms"], 400)
        self.assertEqual(words[1]["end_ms"], 700)

    def test_mismatched_lengths_returns_empty(self):
        self.assertEqual(
            _word_times_from_char_alignment({"characters": ["a"], "character_start_times_seconds": []}), []
        )

    def test_synth_uses_with_timestamps_endpoint_and_real_word_times(self):
        text = "hi bob"
        body = json.dumps(
            {
                "audio_base64": base64.b64encode(b"RIFF" + b"\x00" * 996).decode(),
                "alignment": {
                    "characters": list(text),
                    "character_start_times_seconds": [0.0, 0.1, 0.3, 0.4, 0.5, 0.6],
                    "character_end_times_seconds": [0.1, 0.2, 0.4, 0.5, 0.6, 0.7],
                },
            }
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                tts = ElevenLabsTTS()
                audio, word_times = tts.synth(text)

        req = mock_urlopen.call_args[0][0]
        self.assertIn("/with-timestamps", req.full_url)
        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertEqual([w["word"] for w in word_times], ["hi", "bob"])
        self.assertTrue(all(w["estimated"] is False for w in word_times))


class TestDeepgramTtsEstimatesWordTimes(unittest.TestCase):
    def test_synth_returns_estimated_word_times(self):
        from server.providers import DeepgramTTS

        wav = providers.tts._sine_wav_bytes(duration_s=1.0, sample_rate=22050)
        mock_resp = MagicMock()
        mock_resp.read.return_value = wav
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            tts = DeepgramTTS(api_key="dg-test")
            audio, word_times = tts.synth("hello there friend")

        self.assertTrue(len(word_times) == 3)
        self.assertTrue(all(w["estimated"] for w in word_times))


if __name__ == "__main__":
    unittest.main()
