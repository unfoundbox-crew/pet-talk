#!/usr/bin/env python3
"""qa/test_voice_analyzer.py — Unit tests for Voice Forensics & Latency Analyzer.

Uses pure synthetic math waveforms (sine/silence buffers) with zero external downloads.
Proves:
  1. Turn-taking latency calculation accuracy (+-15ms hop tolerance)
  2. Barge-in kill latency detection (e.g. 100ms kill gate)
  3. Fade-out cutoff classification (hard_clip vs linear_fade vs cosine_fade)
  4. Backchannel detection (<1.2s vocal affirmation during long user speech)
  5. Stereo interleaved vs dual-mono file equivalence

Run: `python3 -m unittest qa/test_voice_analyzer.py`
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import unittest
import wave

import numpy as np

from qa.benchmarks.voice_analyzer import (
    AudioTrack,
    analyze_cutoff_fade,
    compute_conversation_metrics,
    detect_speech_segments,
    format_report,
    load_conversation_audio,
)


def make_sine(
    duration_ms: float,
    sample_rate: int = 16000,
    freq: float = 440.0,
    amplitude: float = 0.5,
    fade_out_type: str = "none",  # "none", "hard_clip", "linear", "cosine"
    fade_ms: float = 25.0,
) -> np.ndarray:
    """Generate synthetic sine wave with optional envelope shaping."""
    n_samples = max(1, int(duration_ms * sample_rate / 1000.0))
    t = np.arange(n_samples) / sample_rate
    wave_arr = (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)

    # Initial 5ms fade-in to avoid start click
    fin_len = min(n_samples // 4, int(0.005 * sample_rate))
    if fin_len > 0:
        wave_arr[:fin_len] *= np.linspace(0.0, 1.0, fin_len, dtype=np.float32)

    fade_len = min(n_samples, int(fade_ms * sample_rate / 1000.0))
    if fade_len > 0 and fade_out_type != "none":
        if fade_out_type == "linear":
            ramp = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
            wave_arr[-fade_len:] *= ramp
        elif fade_out_type == "cosine":
            t_cos = np.linspace(0.0, np.pi, fade_len, dtype=np.float32)
            hann_half = 0.5 * (1.0 + np.cos(t_cos))
            wave_arr[-fade_len:] *= hann_half
        elif fade_out_type == "hard_clip":
            # Force high non-zero sample right up to truncation
            pass

    return wave_arr


def make_silence(duration_ms: float, sample_rate: int = 16000) -> np.ndarray:
    n_samples = max(0, int(duration_ms * sample_rate / 1000.0))
    return np.zeros(n_samples, dtype=np.float32)


def save_test_wav(
    path: str,
    samples: np.ndarray,
    sample_rate: int = 16000,
    channels: int = 1,
) -> None:
    """Save float32 array as 16-bit PCM WAV."""
    int16_samples = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16_samples.tobytes())


class TestVoiceAnalyzer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pet_talk_voice_qa_")
        self.sr = 16000

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_turn_taking_latency_accuracy(self):
        """User speaks for 1000ms -> 350ms silence -> Agent answers for 800ms."""
        user_audio = np.concatenate([
            make_sine(1000.0, self.sr, freq=300.0, amplitude=0.6, fade_out_type="linear", fade_ms=10.0),
            make_silence(1500.0, self.sr),
        ])

        target_latency_ms = 350.0
        agent_audio = np.concatenate([
            make_silence(1000.0 + target_latency_ms, self.sr),
            make_sine(800.0, self.sr, freq=600.0, amplitude=0.5, fade_out_type="linear", fade_ms=10.0),
            make_silence(350.0, self.sr),
        ])

        user_segs = detect_speech_segments(user_audio, self.sr, speaker="User")
        agent_segs = detect_speech_segments(agent_audio, self.sr, speaker="Agent")

        self.assertEqual(len(user_segs), 1)
        self.assertEqual(len(agent_segs), 1)

        turns, barges, backchannels = compute_conversation_metrics(
            user_segs, agent_segs, agent_audio, self.sr
        )

        self.assertEqual(len(turns), 1)
        self.assertAlmostEqual(turns[0].latency_ms, target_latency_ms, delta=15.0)
        self.assertEqual(len(barges), 0)
        self.assertEqual(len(backchannels), 0)

    def test_barge_in_kill_latency_detection(self):
        """Agent speaks from 0-1600ms. User interrupts at 800ms. Agent dies at 900ms (100ms kill)."""
        target_kill_ms = 100.0
        interrupt_time_ms = 800.0
        agent_die_time_ms = interrupt_time_ms + target_kill_ms

        agent_audio = np.concatenate([
            make_sine(agent_die_time_ms, self.sr, freq=500.0, amplitude=0.5, fade_out_type="linear", fade_ms=15.0),
            make_silence(1000.0, self.sr),
        ])

        user_audio = np.concatenate([
            make_silence(interrupt_time_ms, self.sr),
            make_sine(700.0, self.sr, freq=350.0, amplitude=0.6, fade_out_type="linear", fade_ms=15.0),
            make_silence(400.0, self.sr),
        ])

        user_segs = detect_speech_segments(user_audio, self.sr, speaker="User")
        agent_segs = detect_speech_segments(agent_audio, self.sr, speaker="Agent")

        turns, barges, backchannels = compute_conversation_metrics(
            user_segs, agent_segs, agent_audio, self.sr
        )

        self.assertEqual(len(barges), 1)
        barge = barges[0]
        self.assertAlmostEqual(barge.kill_latency_ms, target_kill_ms, delta=15.0)
        self.assertIn(barge.fade.classification, ("linear_fade", "cosine_fade"))

    def test_cutoff_fade_out_classification(self):
        """Verify distinction between hard-clip (pop risk) and smooth fades (cosine/linear)."""
        sr = self.sr

        # 1. Hard clip: high sine amplitude abruptly set to 0.0
        sine_hard = make_sine(500.0, sr, freq=440.0, amplitude=0.8, fade_out_type="hard_clip")
        audio_hard = np.concatenate([sine_hard, np.zeros(int(0.05 * sr), dtype=np.float32)])
        report_hard = analyze_cutoff_fade(audio_hard, sr, cutoff_ms=500.0)
        self.assertEqual(report_hard.classification, "hard_clip")
        self.assertIn(report_hard.click_risk, ("HIGH", "MEDIUM"))
        self.assertGreater(report_hard.step_discontinuity, 0.3)

        # 2. Linear fade over 25ms
        sine_lin = make_sine(500.0, sr, freq=440.0, amplitude=0.8, fade_out_type="linear", fade_ms=25.0)
        audio_lin = np.concatenate([sine_lin, np.zeros(int(0.05 * sr), dtype=np.float32)])
        report_lin = analyze_cutoff_fade(audio_lin, sr, cutoff_ms=500.0)
        self.assertEqual(report_lin.classification, "linear_fade")
        self.assertEqual(report_lin.click_risk, "LOW")
        self.assertGreaterEqual(report_lin.linear_corr, 0.75)

        # 3. Cosine fade (raised cosine / Hann) over 25ms
        sine_cos = make_sine(500.0, sr, freq=440.0, amplitude=0.8, fade_out_type="cosine", fade_ms=25.0)
        audio_cos = np.concatenate([sine_cos, np.zeros(int(0.05 * sr), dtype=np.float32)])
        report_cos = analyze_cutoff_fade(audio_cos, sr, cutoff_ms=500.0)
        self.assertEqual(report_cos.classification, "cosine_fade")
        self.assertEqual(report_cos.click_risk, "LOW")
        self.assertGreaterEqual(report_cos.cosine_corr, 0.85)

    def test_backchannel_detection(self):
        """User holds long conversational turn (3.5s). Agent emits 300ms 'mm-hmm' affirmation."""
        user_audio = np.concatenate([
            make_sine(3500.0, self.sr, freq=280.0, amplitude=0.5, fade_out_type="linear", fade_ms=20.0),
            make_silence(500.0, self.sr),
        ])

        agent_audio = np.concatenate([
            make_silence(1200.0, self.sr),
            make_sine(300.0, self.sr, freq=550.0, amplitude=0.3, fade_out_type="cosine", fade_ms=20.0),
            make_silence(2500.0, self.sr),
        ])

        user_segs = detect_speech_segments(user_audio, self.sr, speaker="User")
        agent_segs = detect_speech_segments(agent_audio, self.sr, speaker="Agent")

        turns, barges, backchannels = compute_conversation_metrics(
            user_segs, agent_segs, agent_audio, self.sr
        )

        self.assertEqual(len(backchannels), 1)
        bc = backchannels[0]
        self.assertEqual(bc.speaker, "Agent")
        self.assertAlmostEqual(bc.duration_ms, 300.0, delta=20.0)
        # Verify the backchannel was NOT counted as agent stealing the turn
        self.assertEqual(len(turns), 0)
        self.assertEqual(len(barges), 0)

    def test_stereo_interleaved_and_dual_mono_equivalence(self):
        """Verify identical results whether loaded as interleaved stereo or dual mono WAVs."""
        user_data = make_sine(800.0, self.sr, freq=300.0, amplitude=0.5)
        agent_data = np.concatenate([
            make_silence(1100.0, self.sr),
            make_sine(600.0, self.sr, freq=600.0, amplitude=0.5),
        ])
        user_padded = np.pad(user_data, (0, len(agent_data) - len(user_data)))

        stereo_path = os.path.join(self.temp_dir, "conversation_stereo.wav")
        user_mono_path = os.path.join(self.temp_dir, "user.wav")
        agent_mono_path = os.path.join(self.temp_dir, "agent.wav")

        # Save stereo
        stereo_interleaved = np.empty((len(user_padded), 2), dtype=np.float32)
        stereo_interleaved[:, 0] = user_padded
        stereo_interleaved[:, 1] = agent_data
        save_test_wav(stereo_path, stereo_interleaved.flatten(), self.sr, channels=2)

        # Save dual mono
        save_test_wav(user_mono_path, user_padded, self.sr, channels=1)
        save_test_wav(agent_mono_path, agent_data, self.sr, channels=1)

        # Load both
        track_u_st, track_a_st = load_conversation_audio(stereo_path=stereo_path)
        track_u_dm, track_a_dm = load_conversation_audio(user_path=user_mono_path, agent_path=agent_mono_path)

        u_segs_st = detect_speech_segments(track_u_st.samples, track_u_st.sample_rate, speaker="User")
        a_segs_st = detect_speech_segments(track_a_st.samples, track_a_st.sample_rate, speaker="Agent")
        turns_st, _, _ = compute_conversation_metrics(u_segs_st, a_segs_st, track_a_st.samples, self.sr)

        u_segs_dm = detect_speech_segments(track_u_dm.samples, track_u_dm.sample_rate, speaker="User")
        a_segs_dm = detect_speech_segments(track_a_dm.samples, track_a_dm.sample_rate, speaker="Agent")
        turns_dm, _, _ = compute_conversation_metrics(u_segs_dm, a_segs_dm, track_a_dm.samples, self.sr)

        self.assertEqual(len(turns_st), len(turns_dm))
        self.assertAlmostEqual(turns_st[0].latency_ms, turns_dm[0].latency_ms, places=2)

        # Verify formatting output renders cleanly without exception
        report = format_report(track_u_st, track_a_st, u_segs_st, a_segs_st, turns_st, [], [])
        self.assertIn("VOICE FORENSICS & CONVERSATION LATENCY REPORT", report)
        self.assertIn("TURN-TAKING LATENCY", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
