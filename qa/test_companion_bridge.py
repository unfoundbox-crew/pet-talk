#!/usr/bin/env python3
"""qa/test_companion_bridge.py — the companion state bridge, hermetically.

What this proves (no network, no daemon, no audio device, no sound):

1. ``server.companion.audio_level``: RMS and peak of int16 PCM (raw or RIFF)
   normalised to 0.0..1.0 — silence is 0, a full-scale square is 1, a
   full-scale sine is ~0.71, garbage is 0 and never raises. The strided
   estimator stays within 2% of the exact RMS, so its cost can be bounded
   without lying about the level.
2. ``server.companion.packets``: the wire-frame -> companion-state mapping,
   one packet shape, and the stable field set the orb skin reads.
3. ``server.companion.bridge``: idle -> thinking -> speaking -> idle through
   the frames a real turn sends; partial transcripts do not flip state;
   identical consecutive states are not re-sent; ``state.speaking`` keeps the
   last sentence's level instead of blinking to zero.
4. ``server.companion.broadcast``: ``publish`` is synchronous and returns
   without touching the socket (zero cost on the TTS send path); a slow
   subscriber's queue drops its oldest packet, never blocks the publisher;
   a dead subscriber is dropped, named.
5. Integration through the real app: ``/events`` refuses without the studio
   token (4401); with it, a ``user.text`` turn on ``/ws`` is mirrored on
   ``/events`` as idle -> thinking -> speaking (level > 0, from the stub
   TTS sine) -> idle, and a late subscriber receives the current state
   immediately.

Run: ``python3 qa/test_companion_bridge.py -v``
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import struct
import sys
import unittest
import wave
from unittest.mock import AsyncMock, MagicMock

from starlette.websockets import WebSocketState

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("STT_PROVIDER", "stub")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("TTS_PROVIDER", "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")
os.environ.pop("PET_TALK_AX", None)

from fastapi.testclient import TestClient

from server.app import app
from server.auth import STUDIO_TOKEN_HEADER, studio_token
from server.audio_store import store_audio
from server.companion import bridge
from server.companion.audio_level import (
    MAX_SAMPLES,
    pcm16_samples,
    peak_level,
    rms_level,
)
from server.companion.bridge import CompanionBridge
from server.companion.broadcast import QUEUE_DEPTH, Broadcaster
from server.companion.packets import (
    ORB_ID,
    PACKET_TYPE,
    STATE_IDLE,
    STATE_SPEAKING,
    STATE_THINKING,
    STATES,
    companion_state_packet,
    transition_for_frame,
)
from server.frames import frame, safe_send_json

AUTH = {STUDIO_TOKEN_HEADER: studio_token()}


# ------------------------------------------------------------ fixtures ---


def pcm_square(amplitude: int = 32767, n: int = 2000) -> bytes:
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


def pcm_sine(amplitude: float = 1.0, n: int = 4410, freq_hz: float = 441.0, sr: int = 44100) -> bytes:
    return struct.pack(
        f"<{n}h",
        *(int(32767 * amplitude * math.sin(2 * math.pi * freq_hz * i / sr)) for i in range(n)),
    )


def wav_of(pcm: bytes, sr: int = 22050, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def exact_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n) / 32768.0


def fake_ws(fail: bool = False) -> MagicMock:
    ws = MagicMock(name="ws")
    ws.client_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock(side_effect=RuntimeError("closed") if fail else None)
    return ws


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- 1. audio level ---


class AudioLevelTests(unittest.TestCase):
    def test_silence_is_zero(self):
        self.assertEqual(rms_level(b"\x00" * 4000), 0.0)
        self.assertEqual(peak_level(b"\x00" * 4000), 0.0)

    def test_empty_and_garbage_are_zero_never_raise(self):
        self.assertEqual(rms_level(b""), 0.0)
        self.assertEqual(rms_level(b"RIFFxxxx not a wav"), 0.0)
        self.assertEqual(peak_level(b"\x01"), 0.0)  # one odd byte

    def test_full_scale_square_is_one(self):
        self.assertAlmostEqual(rms_level(pcm_square()), 1.0, places=3)
        self.assertAlmostEqual(peak_level(pcm_square()), 1.0, places=3)

    def test_full_scale_sine_is_root_half(self):
        self.assertAlmostEqual(rms_level(pcm_sine(1.0)), 1 / math.sqrt(2), delta=0.01)
        self.assertAlmostEqual(peak_level(pcm_sine(1.0)), 1.0, delta=0.01)

    def test_level_scales_with_amplitude(self):
        self.assertAlmostEqual(rms_level(pcm_sine(0.5)), 0.5 / math.sqrt(2), delta=0.01)
        self.assertAlmostEqual(rms_level(pcm_square(16384)), 0.5, places=3)

    def test_wav_and_raw_pcm_agree(self):
        pcm = pcm_sine(0.4)
        self.assertAlmostEqual(rms_level(wav_of(pcm)), rms_level(pcm), places=6)

    def test_stereo_wav_is_measured(self):
        pcm = pcm_square(16384)
        self.assertAlmostEqual(rms_level(wav_of(pcm, channels=2)), 0.5, places=3)

    def test_eight_bit_wav_is_silence_not_a_crash(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(8000)
            w.writeframes(b"\x80\xff" * 100)
        self.assertEqual(rms_level(buf.getvalue()), 0.0)

    def test_estimator_is_strided_and_bounded(self):
        # A 10 s clip at 24 kHz (Kokoro's rate) is 240k samples; the
        # estimator reads at most MAX_SAMPLES of them, so its cost on the
        # send path is constant — and it still lands within 2% of exact.
        pcm = pcm_sine(0.6, n=240_000, freq_hz=220.0, sr=24000)
        self.assertLessEqual(len(pcm16_samples(pcm)), MAX_SAMPLES)
        self.assertAlmostEqual(rms_level(pcm), exact_rms(pcm), delta=0.02 * exact_rms(pcm))

    def test_level_is_clamped(self):
        floor = struct.pack("<4h", -32768, -32768, -32768, -32768)
        self.assertEqual(rms_level(floor), 1.0)
        self.assertEqual(peak_level(floor), 1.0)


# ----------------------------------------------------------- 2. packets ---


class PacketTests(unittest.TestCase):
    def test_packet_shape(self):
        p = companion_state_packet(STATE_SPEAKING, 0.42, "t1")
        self.assertEqual(p["type"], PACKET_TYPE)
        self.assertEqual(p["type"], "companion_state")
        self.assertEqual(p["state"], "speaking")
        self.assertEqual(p["audio_level"], 0.42)
        self.assertEqual(p["orb_id"], ORB_ID)
        self.assertEqual(p["turn_id"], "t1")
        self.assertIsInstance(p["timestamp"], float)
        self.assertGreater(p["timestamp"], 1_700_000_000)

    def test_states_are_exactly_three(self):
        self.assertEqual(set(STATES), {"idle", "thinking", "speaking"})

    def test_packet_rejects_unknown_state_and_bad_level(self):
        with self.assertRaises(ValueError):
            companion_state_packet("listening", 0.0, "t1")
        self.assertEqual(companion_state_packet(STATE_IDLE, 7.0, "t1")["audio_level"], 1.0)
        self.assertEqual(companion_state_packet(STATE_IDLE, -1.0, "t1")["audio_level"], 0.0)

    def test_frame_mapping(self):
        cur = (STATE_IDLE, 0.0)
        self.assertEqual(transition_for_frame({"type": "state.listening"}, *cur), (STATE_IDLE, 0.0))
        self.assertEqual(transition_for_frame({"type": "state.thinking"}, *cur), (STATE_THINKING, 0.0))
        self.assertEqual(transition_for_frame({"type": "transcript.user", "text": "hi"}, *cur), (STATE_THINKING, 0.0))
        self.assertIsNone(transition_for_frame({"type": "transcript.user", "partial": True}, *cur))
        self.assertEqual(transition_for_frame({"type": "state.speaking"}, *cur), (STATE_SPEAKING, 0.0))
        self.assertEqual(transition_for_frame({"type": "agent.done"}, STATE_SPEAKING, 0.5), (STATE_IDLE, 0.0))
        self.assertIsNone(transition_for_frame({"type": "agent.stall"}, *cur))
        self.assertIsNone(transition_for_frame({"type": "companion_state"}, *cur))

    def test_speaking_keeps_level_while_speaking(self):
        self.assertEqual(transition_for_frame({"type": "state.speaking"}, STATE_SPEAKING, 0.3), (STATE_SPEAKING, 0.3))

    def test_sentence_level_from_audio_store_and_chunk_from_b64(self):
        store_audio("t9-s1", wav_of(pcm_square(16384)))
        state, level = transition_for_frame(
            {"type": "agent.sentence", "audio_url": "/audio/t9-s1"}, STATE_SPEAKING, 0.0
        )
        self.assertEqual(state, STATE_SPEAKING)
        self.assertAlmostEqual(level, 0.5, places=3)
        chunk = {"type": "agent.chunk", "audio_b64": base64.b64encode(pcm_square()).decode()}
        self.assertAlmostEqual(transition_for_frame(chunk, STATE_SPEAKING, 0.0)[1], 1.0, places=3)
        missing = {"type": "agent.sentence", "audio_url": "/audio/nope"}
        self.assertEqual(transition_for_frame(missing, STATE_SPEAKING, 0.0), (STATE_SPEAKING, 0.0))


# ------------------------------------------------------------ 3. bridge ---


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.b = CompanionBridge(broadcaster=Broadcaster())

    def states(self):
        return [p["state"] for p in self.b.history]

    def test_full_turn_transitions(self):
        store_audio("t1-s1", wav_of(pcm_sine(0.5)))
        for f in (
            frame("state.idle", "t0"),
            frame("state.listening", "t1"),
            frame("transcript.user", "t1", text="hello", partial=True, final=False),
            frame("transcript.user", "t1", text="hello"),
            frame("state.thinking", "t1"),
            frame("state.speaking", "t1"),
            frame("agent.sentence", "t1", seq=1, audio_url="/audio/t1-s1"),
            frame("agent.done", "t1", path="direct", sentences=1),
        ):
            self.b.observe_frame(f)
        self.assertEqual(self.states(), ["idle", "idle", "thinking", "speaking", "speaking", "idle"])
        levels = [p["audio_level"] for p in self.b.history]
        self.assertEqual(levels[0:4], [0.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(levels[4], 0.5 / math.sqrt(2), delta=0.01)
        self.assertEqual(levels[5], 0.0)
        self.assertEqual(self.b.state, STATE_IDLE)
        self.assertEqual(self.b.last["turn_id"], "t1")

    def test_duplicate_state_is_not_resent(self):
        self.assertIsNotNone(self.b.observe_frame(frame("state.thinking", "t1")))
        self.assertIsNone(self.b.observe_frame(frame("state.thinking", "t1")))
        self.assertIsNone(self.b.observe_frame(frame("transcript.user", "t1", text="x")))
        self.assertEqual(len(self.b.history), 1)

    def test_new_turn_same_state_is_sent(self):
        self.b.observe_frame(frame("state.idle", "t1"))
        self.assertIsNotNone(self.b.observe_frame(frame("state.listening", "t2")))

    def test_unrelated_frames_are_ignored(self):
        self.assertIsNone(self.b.observe_frame(frame("agent.error", "t1", reason="x")))
        self.assertIsNone(self.b.observe_frame({"type": "companion_state"}))
        self.assertIsNone(self.b.observe_frame({}))
        self.assertEqual(len(self.b.history), 0)

    def test_speaking_keeps_level_across_repeated_state_speaking(self):
        store_audio("t3-s1", wav_of(pcm_square(16384)))
        self.b.observe_frame(frame("state.speaking", "t3"))
        self.b.observe_frame(frame("agent.sentence", "t3", seq=1, audio_url="/audio/t3-s1"))
        self.assertIsNone(self.b.observe_frame(frame("state.speaking", "t3")))
        self.assertAlmostEqual(self.b.audio_level, 0.5, places=3)

    def test_observe_publishes_to_broadcaster(self):
        seen = []
        self.b.broadcaster.publish = lambda packet: seen.append(packet) or 1
        self.b.observe_frame(frame("state.thinking", "t4"))
        self.assertEqual([p["state"] for p in seen], ["thinking"])

    def test_reset(self):
        self.b.observe_frame(frame("state.thinking", "t5"))
        self.b.reset()
        self.assertEqual((self.b.state, self.b.audio_level, self.b.turn_id, self.b.last), ("idle", 0.0, "", None))
        self.assertEqual(len(self.b.history), 0)


# --------------------------------------------------------- 4. broadcast ---


class BroadcastTests(unittest.TestCase):
    def test_publish_is_sync_and_does_not_touch_sockets(self):
        async def go():
            bc = Broadcaster()
            ws = fake_ws()
            await bc.subscribe(ws)
            ws.send_json.reset_mock()
            n = bc.publish({"type": "companion_state", "state": "thinking"})  # no await
            self.assertEqual(n, 1)
            ws.send_json.assert_not_called()  # nothing sent inline
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            ws.send_json.assert_called_once()
            await bc.unsubscribe(ws)
            self.assertEqual(bc.count, 0)

        run(go())

    def test_publish_with_no_subscribers_is_free(self):
        self.assertEqual(Broadcaster().publish({"state": "idle"}), 0)

    def test_slow_subscriber_drops_oldest_never_blocks(self):
        async def go():
            bc = Broadcaster()
            gate = asyncio.Event()
            ws = fake_ws()

            async def slow(_packet):
                await gate.wait()

            ws.send_json = AsyncMock(side_effect=slow)
            await bc.subscribe(ws)
            total = QUEUE_DEPTH + 5
            for i in range(total):
                bc.publish({"seq": i})  # returns immediately every time
            await asyncio.sleep(0)
            sub = bc.subscription(ws)
            # All publishes landed before the sender task first ran, so the
            # queue holds the newest QUEUE_DEPTH and the rest were dropped
            # oldest-first; the sender is now blocked on the first survivor.
            self.assertEqual(sub.dropped, total - QUEUE_DEPTH)
            gate.set()
            for _ in range(total + 5):
                await asyncio.sleep(0)
            sent = [c.args[0]["seq"] for c in ws.send_json.call_args_list]
            self.assertEqual(sent[-1], total - 1)  # the newest always arrives
            self.assertEqual(sent, sorted(sent))
            await bc.unsubscribe(ws)

        run(go())

    def test_dead_subscriber_is_dropped(self):
        async def go():
            bc = Broadcaster()
            good, bad = fake_ws(), fake_ws(fail=True)
            bad.send_json = AsyncMock(return_value=None)
            await bc.subscribe(good)
            await bc.subscribe(bad)
            bad.send_json = AsyncMock(side_effect=RuntimeError("closed"))
            self.assertEqual(bc.publish({"seq": 1}), 2)
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(bc.count, 1)
            self.assertIs(bc.subscription(good).ws, good)
            self.assertIsNone(bc.subscription(bad))
            await bc.unsubscribe(good)

        run(go())

    def test_subscribe_sends_hello_packet(self):
        async def go():
            bc = Broadcaster()
            ws = fake_ws()
            await bc.subscribe(ws, hello={"state": "speaking"})
            ws.send_json.assert_awaited_once_with({"state": "speaking"})
            await bc.unsubscribe(ws)

        run(go())


# ------------------------------------------------------- 5. integration ---


class EventsEndpointTests(unittest.TestCase):
    def setUp(self):
        bridge.reset()
        self.client = TestClient(app, headers=AUTH)

    def test_events_without_token_is_refused(self):
        anon = TestClient(app)
        with self.assertRaises(Exception) as ctx:
            with anon.websocket_connect("/events"):
                pass
        self.assertEqual(getattr(ctx.exception, "code", None), 4401)

    def test_hello_packet_is_current_state(self):
        with self.client.websocket_connect("/events") as ev:
            hello = ev.receive_json()
        self.assertEqual(hello["type"], "companion_state")
        self.assertEqual(hello["state"], "idle")
        self.assertEqual(hello["audio_level"], 0.0)
        self.assertEqual(hello["orb_id"], ORB_ID)

    def test_safe_send_json_feeds_the_bridge(self):
        run(safe_send_json(fake_ws(), frame("state.thinking", "tz")))
        self.assertEqual(bridge.state, STATE_THINKING)
        self.assertEqual(bridge.last["turn_id"], "tz")

    def test_user_text_turn_is_mirrored_on_events(self):
        with self.client.websocket_connect("/events") as ev:
            self.assertEqual(ev.receive_json()["state"], "idle")
            with self.client.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["type"], "state.idle")
                ws.send_json({"type": "user.text", "turn_id": "orb-turn-1",
                              "text": "What is the status of the project?"})
                for _ in range(20):
                    if ws.receive_json().get("type") == "agent.done":
                        break
            packets = []
            for _ in range(20):
                p = ev.receive_json()
                packets.append(p)
                if p["state"] == "idle" and p["turn_id"] == "orb-turn-1":
                    break
        # A fresh /ws connect sends state.idle on its own new turn id, so the
        # first packet is idle for THAT turn; the user's turn follows.
        self.assertEqual(packets[0]["state"], "idle")
        self.assertNotEqual(packets[0]["turn_id"], "orb-turn-1")
        packets = [p for p in packets if p["turn_id"] == "orb-turn-1"]
        for p in packets:
            self.assertEqual(p["type"], "companion_state")
            self.assertEqual(p["orb_id"], ORB_ID)
            self.assertIn(p["state"], STATES)
            self.assertTrue(0.0 <= p["audio_level"] <= 1.0)
        states = [p["state"] for p in packets]
        # Collapse runs: speaking repeats once per sentence with a new level.
        collapsed = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
        self.assertEqual(collapsed, ["thinking", "speaking", "idle"])
        speaking_levels = [p["audio_level"] for p in packets if p["state"] == "speaking"]
        # StubTTS writes a 0.3-amplitude sine: RMS 0.3/sqrt(2) ~= 0.21.
        self.assertTrue(any(abs(l - 0.212) < 0.03 for l in speaking_levels), speaking_levels)
        self.assertEqual(packets[-1]["audio_level"], 0.0)
        self.assertEqual([p["timestamp"] for p in packets], sorted(p["timestamp"] for p in packets))


if __name__ == "__main__":
    unittest.main()
