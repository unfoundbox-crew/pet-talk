#!/usr/bin/env python3
"""qa/test_real_engine_e2e.py — End-to-end verification of real engine endpoints.

Only runs when PET_TALK_REAL_ENGINE=1 — this hits the real Kokoro/STT/LLM
stack (network calls, real synthesis), never the default QA gate. `make
qa-real` sets the flag; plain `make qa`/`qa/run_all.sh` SKIP this file.

Verifies:
1. The configured TTS tyre really synthesizes (>10KB valid RIFF WAV).
2. STT via /transcribe (real transcription of audio input).
3. LLM via /ws (LiteLLM proxy with Donna persona, returning playable audio).

Provider-agnostic by design. It used to pin `KokoroSpacePilotTTS` by class and
so failed the moment the default TTS tyre changed to the in-process
`kokoro-local` — a test that asserts *which* tyre is fitted cannot survive a
tyre swap, which is the one thing the provider contract promises (gate 4,
config-only swap). What it asserts now is that whatever tyre is fitted is a
real one and that it works.
"""
import base64
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRATCH = os.environ.get("PET_TALK_SCRATCH") or os.path.join(ROOT, ".qa-scratch")

REAL_ENGINE = os.environ.get("PET_TALK_REAL_ENGINE") == "1"

if REAL_ENGINE:
    from fastapi.testclient import TestClient
    from server import app as server_module
    from server import providers


class TestRealEngineEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REAL_ENGINE:
            raise unittest.SkipTest(
                "SKIP: PET_TALK_REAL_ENGINE!=1 — this test hits the real "
                "Kokoro/STT/LLM stack; run `make qa-real` to exercise it"
            )
        os.makedirs(SCRATCH, exist_ok=True)
        # Every /transcribe and /ws call needs the studio token — the server
        # is 401/4401 without it (see server/auth.py). Resolved the same way
        # the server resolves it, so an in-process TestClient and the live
        # server agree.
        from server.auth import studio_token

        token = studio_token()
        if not token:
            raise unittest.SkipTest(
                "SKIP: no studio token resolvable (STUDIO_TOKEN, "
                "STUDIO_TOKEN_FILE, or .qa-scratch/studio.token) — every "
                "/transcribe and /ws call would 401"
            )
        cls.client = TestClient(server_module.app, headers={"X-Studio-Token": token})

    def test_01_kokoro_tts_synthesis(self):
        """Verify Kokoro TTS is reachable and produces >10KB valid RIFF WAV."""
        tts = server_module.tts
        # Whatever tyre is configured, as long as it is a real one: a stub
        # would pass the byte-count assertion below on a sine wave, and an
        # Unavailable* placeholder must surface its build failure by name.
        self.assertNotIsInstance(tts, providers.StubTTS,
                                 "real-engine test ran against StubTTS")
        self.assertIsInstance(tts, providers.TTSProvider)
        print(f"\n[TYRE] TTS = {type(tts).__name__}")
        test_text = "Good morning. Donna Paulsen here, executive secretary mode is fully operational."
        wav_bytes, _ = tts.synth(test_text, voice="af_heart", speed=1.05)
        self.assertTrue(len(wav_bytes) > 10000, f"Audio too small: {len(wav_bytes)} bytes")
        self.assertTrue(wav_bytes.startswith(b"RIFF"), "Missing RIFF WAV header")

        receipt_path = os.path.join(SCRATCH, "kokoro_tts_receipt.wav")
        with open(receipt_path, "wb") as f:
            f.write(wav_bytes)
        print(f"\n[RECEIPT 1] Kokoro TTS Synthesized {len(wav_bytes)} bytes -> {receipt_path}")

    def test_02_stt_transcribe_endpoint(self):
        """Verify /transcribe endpoint transcribes real audio input."""
        receipt_path = os.path.join(SCRATCH, "kokoro_tts_receipt.wav")
        if not os.path.exists(receipt_path):
            wav_bytes, _ = server_module.tts.synth("Hello world, testing speech recognition.", voice="af_heart")
            with open(receipt_path, "wb") as f:
                f.write(wav_bytes)
        else:
            with open(receipt_path, "rb") as f:
                wav_bytes = f.read()

        b64 = base64.b64encode(wav_bytes).decode("ascii")
        resp = self.client.post(
            "/transcribe",
            json={"pcm_b64": b64, "sample_rate": 24000, "clean_prose": True},
        )
        self.assertEqual(resp.status_code, 200, f"Transcribe failed: {resp.text}")
        data = resp.json()
        self.assertTrue(data.get("ok"), f"Transcribe not ok: {data}")
        text = data.get("text", "")
        self.assertTrue(len(text) > 0, "Empty transcription returned")
        print(f"\n[RECEIPT 2] STT ({type(server_module.stt).__name__}) Transcribed: {text!r}")

    def test_03_ws_turn_with_donna_and_kokoro(self):
        """Verify WS turn drives Donna LLM streaming and delivers playable Kokoro WAV."""
        turn_id = "turn-verify-real-001"
        prompt_text = "Donna, are the local engines operational?"

        with self.client.websocket_connect("/ws") as ws:
            idle = ws.receive_json()
            self.assertEqual(idle.get("type"), "state.idle")

            # Submit prompt with Donna persona
            ws.send_json({
                "type": "user.text",
                "turn_id": turn_id,
                "text": prompt_text,
                "persona": "donna",
            })

            received_frames = []
            audio_urls = []
            agent_sentences = []

            # Read frames until agent.done
            while True:
                f = ws.receive_json()
                ftype = f.get("type")
                received_frames.append(ftype)

                if ftype == "agent.sentence":
                    agent_sentences.append(f.get("text"))
                    if f.get("audio_url"):
                        audio_urls.append(f.get("audio_url"))

                if ftype in ("agent.done", "agent.error"):
                    break

            self.assertIn("transcript.user", received_frames)
            self.assertIn("agent.done", received_frames)
            self.assertTrue(len(agent_sentences) > 0, "No sentences synthesized by LLM")
            self.assertTrue(len(audio_urls) > 0, "No audio URLs returned in agent.sentence")

            # Download synthesized audio from /audio/<audio_id>
            first_url = audio_urls[0]
            audio_resp = self.client.get(first_url)
            self.assertEqual(audio_resp.status_code, 200)
            self.assertEqual(audio_resp.headers.get("content-type"), "audio/wav")

            audio_data = audio_resp.content
            self.assertTrue(len(audio_data) > 10000, f"Synthesized audio too small: {len(audio_data)} bytes")
            self.assertTrue(audio_data.startswith(b"RIFF"), "Audio does not have RIFF header")

            ws_receipt_path = os.path.join(SCRATCH, "donna_ws_verified.wav")
            with open(ws_receipt_path, "wb") as f:
                f.write(audio_data)

            print(f"\n[RECEIPT 3] WS Donna Turn Completed:")
            print(f"  User Prompt: {prompt_text}")
            print(f"  Donna Spoke: {' '.join(agent_sentences)}")
            print(f"  Audio Output: {len(audio_data)} bytes -> {ws_receipt_path}")


if __name__ == "__main__":
    unittest.main()
