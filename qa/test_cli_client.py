#!/usr/bin/env python3
"""qa/test_cli_client.py — TDD test suite for Pet-Talk Terminal CLI client.

Verifies:
1. Audio encoding, Base64 conversion, and RMS calculation.
2. EnergyVAD voice onset confirmation and silence turn completion.
3. Audio recorder binary detection.
4. AudioPlayer afplay execution and barge-in kill latency (<= 50ms).
5. WS connection and full-duplex frame exchange with server/app.py.
6. Barge-in mid-turn cancellation and server ack.
"""
from __future__ import annotations

import array
import asyncio
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRATCH = os.environ.get("PET_TALK_SCRATCH") or os.path.join(ROOT, ".qa-scratch")

from cli.audio import (
    AudioPlayer,
    AudioRecorder,
    EnergyVAD,
    base64_to_pcm16,
    calculate_rms,
    find_recorder,
    pcm16_to_base64,
)
from cli import client as client_mod
from cli.client import PetTalkClient


class TestAudioProcessing(unittest.TestCase):
    """Test audio utilities, encoding, and energy calculation."""

    def test_pcm16_base64_roundtrip(self):
        # Generate 100 samples of known 16-bit PCM data
        raw_samples = array.array("h", [i * 100 for i in range(100)]).tobytes()
        encoded = pcm16_to_base64(raw_samples)
        self.assertIsInstance(encoded, str)
        decoded = base64_to_pcm16(encoded)
        self.assertEqual(raw_samples, decoded)

    def test_calculate_rms_silence(self):
        silence = bytes(1024)
        rms = calculate_rms(silence)
        self.assertEqual(rms, 0.0)

    def test_calculate_rms_sine_wave(self):
        # Synthetic 1kHz sine wave @ 16kHz sample rate, amplitude = 10000
        # Expected RMS = amplitude / sqrt(2) ≈ 7071
        samples = []
        amp = 10000
        for n in range(1600):
            val = int(amp * math.sin(2 * math.pi * 1000 * n / 16000))
            samples.append(val)
        raw_pcm = array.array("h", samples).tobytes()
        rms = calculate_rms(raw_pcm)
        self.assertAlmostEqual(rms, amp / math.sqrt(2), delta=100.0)

    def test_energy_vad_state_machine(self):
        vad = EnergyVAD(threshold=350.0, confirm_frames=2, silence_ms=200)
        silence_chunk = bytes(1024)  # RMS = 0
        speech_samples = array.array("h", [2000] * 512).tobytes()  # RMS = 2000

        # Frame 1: silence -> not speaking
        is_speech, done = vad.process_chunk(silence_chunk)
        self.assertFalse(is_speech)
        self.assertFalse(done)
        self.assertFalse(vad.speech_started)

        # Frame 2: speech onset 1 -> not confirmed yet (confirm_frames = 2)
        is_speech, done = vad.process_chunk(speech_samples)
        self.assertTrue(is_speech)
        self.assertFalse(done)
        self.assertFalse(vad.speech_started)

        # Frame 3: speech onset 2 -> confirmed!
        is_speech, done = vad.process_chunk(speech_samples)
        self.assertTrue(is_speech)
        self.assertFalse(done)
        self.assertTrue(vad.speech_started)

        # Feed silence chunks until silence_ms (200ms = ~7 chunks of 32ms) is exceeded
        turn_finished = False
        for _ in range(10):
            _, turn_finished = vad.process_chunk(silence_chunk)
            if turn_finished:
                break

        self.assertTrue(turn_finished, "VAD should declare turn finished after silence timeout")

    def test_energy_vad_initial_silence_timeout(self):
        # 100ms initial silence cutoff, chunks of 32ms
        vad = EnergyVAD(threshold=350.0, confirm_frames=2, initial_silence_ms=100)
        silence_chunk = bytes(1024)

        turn_finished = False
        for _ in range(5):
            _, turn_finished = vad.process_chunk(silence_chunk)
            if turn_finished:
                break

        self.assertTrue(turn_finished, "VAD should timeout when no speech is detected within initial_silence_ms")
        self.assertFalse(vad.speech_started, "Speech should not be marked started on silence timeout")

    def test_find_recorder_detection(self):
        cmd = find_recorder()
        self.assertIsInstance(cmd, list)
        self.assertGreater(len(cmd), 0)
        self.assertIn(cmd[0], ("sox", "rec", "ffmpeg"))


class TestAudioPlaybackAndBarge(unittest.IsolatedAsyncioTestCase):
    """Test afplay playback, queuing, and sub-50ms barge-in kill."""

    async def test_audio_player_enqueue_rejects_invalid_wav(self):
        player = AudioPlayer()
        # Invalid WAV: empty or truncated header
        await player.enqueue(audio_url="http://invalid-url-corrupt", turn_id="t1", seq=0)
        self.assertEqual(player._queue.qsize(), 0, "Corrupt or unreachable audio must not be enqueued")
        await player.stop()

    async def test_afplay_barge_kill_under_50ms(self):
        if os.environ.get("PET_TALK_SILENT") == "1":
            self.skipTest("SKIP: PET_TALK_SILENT=1 — this spawns real afplay playback")
        player = AudioPlayer()
        player.start()

        # Create a small valid WAV file for afplay testing
        wav_path = os.path.join(ROOT, "bake-deepgram_0.wav")
        if not os.path.exists(wav_path):
            self.skipTest("Reference WAV bake-deepgram_0.wav not found")

        # Directly spawn afplay through player's active proc to simulate live playback
        proc = subprocess.Popen(
            ["afplay", wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        player._active_proc = proc
        await asyncio.sleep(0.02)  # ensure afplay is actively running

        self.assertTrue(player.is_playing())

        # Barge-in kill: must execute in <= 50ms
        kill_ms = player.kill_playback()
        print(f"\n    [TEST] afplay killed via barge-in in {kill_ms:.2f}ms (budget <= 50ms)")
        self.assertLessEqual(kill_ms, 50.0, f"Barge kill took {kill_ms:.2f}ms (>50ms budget)")
        self.assertFalse(player.is_playing())

        await player.stop()


class TestStudioTokenResolution(unittest.TestCase):
    """Hermetic: cli.client resolves the studio token the same way as
    server/auth.py (env STUDIO_TOKEN -> STUDIO_TOKEN_FILE -> the file the
    server generates at <repo>/.qa-scratch/studio.token). No live server."""

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in ("STUDIO_TOKEN", "STUDIO_TOKEN_FILE")
        }
        os.environ.pop("STUDIO_TOKEN", None)
        os.environ.pop("STUDIO_TOKEN_FILE", None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_var_wins(self):
        os.environ["STUDIO_TOKEN"] = "from-env"
        self.assertEqual(client_mod.resolve_studio_token(), "from-env")

    def test_token_file_env_used_when_set_token_absent(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".token") as f:
            f.write("from-file\n")
            path = f.name
        try:
            os.environ["STUDIO_TOKEN_FILE"] = path
            self.assertEqual(client_mod.resolve_studio_token(), "from-file")
        finally:
            os.remove(path)

    def test_falls_back_to_generated_repo_file(self):
        # No env set: resolve_studio_token should read the same
        # <repo>/.qa-scratch/studio.token the server writes (server/auth.py
        # TOKEN_PATH). This asserts the two paths actually agree, not just
        # that resolution returns *something*.
        generated_path = os.path.join(ROOT, ".qa-scratch", "studio.token")
        if not os.path.exists(generated_path):
            self.skipTest("no generated token file at .qa-scratch/studio.token")
        with open(generated_path, encoding="utf-8") as f:
            expected = f.read().strip()
        self.assertEqual(client_mod.resolve_studio_token(), expected)

    def test_ws_connect_kwargs_prefers_additional_headers(self):
        # websockets >= 14 exposes additional_headers; assert we actually
        # target the parameter the installed version accepts rather than
        # guessing a name and silently sending nothing.
        kwargs = client_mod._ws_connect_kwargs("tok123")
        self.assertTrue(kwargs, "expected a header kwarg for this websockets version")
        header_dict = next(iter(kwargs.values()))
        self.assertEqual(header_dict[client_mod.STUDIO_TOKEN_HEADER], "tok123")

    def test_ws_url_with_token_appends_query_param(self):
        self.assertEqual(
            client_mod._ws_url_with_token("ws://127.0.0.1:8089/ws", "tok123"),
            "ws://127.0.0.1:8089/ws?token=tok123",
        )
        self.assertEqual(
            client_mod._ws_url_with_token("ws://127.0.0.1:8089/ws?x=1", "tok123"),
            "ws://127.0.0.1:8089/ws?x=1&token=tok123",
        )

    def test_connect_reports_clear_error_on_unauthorized_handshake(self):
        # Simulate the handshake rejection server/ws.py produces when the
        # token is missing/wrong (measured: uvicorn answers HTTP 403 for a
        # close() called before accept()) — no live server needed.
        class _FakeResponse:
            status_code = 403

        async def _raise_invalid_status(*_args, **_kwargs):
            raise client_mod.InvalidStatus(_FakeResponse())

        client = PetTalkClient(ws_url="ws://127.0.0.1:8089/ws", quiet=True)
        with mock.patch.object(client_mod, "ws_connect", side_effect=_raise_invalid_status):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(client.connect())
        self.assertIn("STUDIO_TOKEN", str(ctx.exception))


class TestLiveWsClient(unittest.IsolatedAsyncioTestCase):
    """Test PetTalkClient connection, frames, and barge against live server.

    No token handling here on purpose: PetTalkClient.connect() resolves the
    studio token itself (client_mod.resolve_studio_token, same order as
    server/auth.py), so a live server started with the token this repo's
    .qa-scratch/studio.token already carries authenticates transparently.
    """

    PORT = os.environ.get("LIVE_WS_PORT", "8089")
    WS_URL = os.environ.get("LIVE_WS_URL", f"ws://127.0.0.1:{PORT}/ws")

    async def asyncSetUp(self):
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            self.skipTest("websockets library not installed")

        # Probe port
        import socket
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", int(self.PORT)))
            s.close()
        except Exception:
            self.skipTest(f"Live server not running at 127.0.0.1:{self.PORT}")

    async def test_client_connect_and_idle_handshake(self):
        client = PetTalkClient(ws_url=self.WS_URL, quiet=True)
        await client.connect()
        try:
            first = await client.recv_frame()
            self.assertIsNotNone(first)
            self.assertEqual(first.get("type"), "state.idle")
            self.assertTrue(first.get("turn_id"))
        finally:
            await client.close()

    async def test_client_turn_frame_exchange(self):
        client = PetTalkClient(ws_url=self.WS_URL, quiet=True)
        await client.connect()
        try:
            first = await client.recv_frame()
            self.assertEqual(first.get("type"), "state.idle")

            turn_id = client.new_turn_id()
            await client.send_frame({"type": "user.start", "turn_id": turn_id, "persona": "donna"})

            # Expect state.listening
            m1 = await client.recv_frame()
            self.assertEqual(m1.get("type"), "state.listening")
            self.assertEqual(m1.get("turn_id"), turn_id)

            # Send audio chunk (use real speech fixture if available so real STT transcribes words)
            receipt_path = os.path.join(SCRATCH, "donna_ws_verified.wav")
            sample_rate = 16000
            if os.path.exists(receipt_path):
                import wave
                with wave.open(receipt_path, "rb") as w:
                    sample_rate = w.getframerate()
                    pcm_data = w.readframes(min(w.getnframes(), sample_rate * 2))
            else:
                pcm_data = bytes(320 * 2)

            await client.send_frame({
                "type": "user.chunk",
                "turn_id": turn_id,
                "chunk": pcm16_to_base64(pcm_data),
            })

            # Send user.stop
            await client.send_frame({
                "type": "user.stop",
                "turn_id": turn_id,
                "pcm_b64": pcm16_to_base64(pcm_data),
                "sample_rate": sample_rate,
            })

            # Collect response frames until agent.done or agent.error
            received_types = []
            for _ in range(15):
                frame = await asyncio.wait_for(client.recv_frame(), timeout=10.0)
                if not frame:
                    break
                received_types.append(frame.get("type"))
                if frame.get("type") in ("agent.done", "agent.error"):
                    break

            print(f"\n    [TEST] Turn frames received: {received_types}")
            self.assertTrue(
                "agent.done" in received_types or "agent.error" in received_types,
                f"Expected agent.done or agent.error in received frames, got {received_types}",
            )
        finally:
            await client.close()

    async def test_client_barge_in_mid_turn(self):
        client = PetTalkClient(ws_url=self.WS_URL, quiet=True)
        await client.connect()
        try:
            await client.recv_frame()  # state.idle
            turn_id = client.new_turn_id()

            await client.send_frame({"type": "user.start", "turn_id": turn_id, "persona": "donna"})
            await client.recv_frame()  # state.listening

            # Stop and immediately barge
            pcm_data = bytes(320 * 2)
            await client.send_frame({
                "type": "user.stop",
                "turn_id": turn_id,
                "pcm_b64": pcm16_to_base64(pcm_data),
                "sample_rate": 16000,
            })

            kill_ms = await client.barge(turn_id)
            self.assertLessEqual(kill_ms, 50.0)

            # Wait for server ack (state.listening with barged_turn)
            ack = None
            for _ in range(10):
                f = await asyncio.wait_for(client.recv_frame(), timeout=5.0)
                if f and f.get("type") == "state.listening" and "barged_turn" in f:
                    ack = f
                    break

            self.assertIsNotNone(ack, "Server should acknowledge barge frame with state.listening")
            self.assertEqual(ack.get("barged_turn"), turn_id)
            print(f"    [TEST] Barge ack received: {ack}")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
