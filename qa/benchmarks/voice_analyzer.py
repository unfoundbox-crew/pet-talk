#!/usr/bin/env python3
"""qa/benchmarks/voice_analyzer.py — Voice Forensics and Conversational Latency Analyzer.

Offline Audio Waveform & Conversation Forensics tool for pet-talk and voice agents.
Analyzes stereo WAV (ch0=User, ch1=Agent) or separate user/agent WAV files.

Computes exact conversational metrics using standard wave + numpy:
  a) Turn-taking latency: User speech cutoff to Agent first audio phoneme.
  b) Interruption / Barge-in Kill Latency: Time from user interrupt onset to agent waveform silence.
  c) Cutoff fade characteristics: Hard-clip (DC pop/click risk) vs smooth fade-out (cosine/linear).
  d) Backchannel detector: Short vocal affirmations (<1.2s, e.g. 'mm-hmm', 'yeah') during long turns.

Outputs monochromatic ASCII tables and distributions (min, p50, p95, max).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import wave
from typing import Any, List, Optional, Tuple

import numpy as np


@dataclasses.dataclass
class AudioTrack:
    name: str
    samples: np.ndarray  # float32 [-1.0, 1.0]
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate > 0 else 0.0

    @property
    def duration_ms(self) -> float:
        return self.duration_s * 1000.0


@dataclasses.dataclass
class SpeechSegment:
    start_ms: float
    end_ms: float
    speaker: str
    peak_dbfs: float

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


@dataclasses.dataclass
class FadeReport:
    classification: str  # "hard_clip", "linear_fade", "cosine_fade", "natural_decay"
    fade_duration_ms: float
    step_discontinuity: float
    click_risk: str  # "HIGH", "MEDIUM", "LOW"
    linear_corr: float
    cosine_corr: float
    detail: str


@dataclasses.dataclass
class TurnTakingEvent:
    user_turn_index: int
    agent_turn_index: int
    user_end_ms: float
    agent_start_ms: float
    latency_ms: float


@dataclasses.dataclass
class BargeInEvent:
    agent_turn_index: int
    agent_start_ms: float
    agent_end_ms: float
    user_start_ms: float
    kill_latency_ms: float
    fade: FadeReport


@dataclasses.dataclass
class BackchannelEvent:
    speaker: str
    start_ms: float
    end_ms: float
    duration_ms: float
    context_speaker: str
    context_turn_ms: float
    peak_dbfs: float


@dataclasses.dataclass
class LatencyStats:
    count: int
    min_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float

    @classmethod
    def from_values(cls, values: List[float]) -> LatencyStats:
        if not values:
            return cls(count=0, min_ms=0.0, p50_ms=0.0, p95_ms=0.0, max_ms=0.0, mean_ms=0.0)
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        p50 = float(np.percentile(sorted_vals, 50))
        p95 = float(np.percentile(sorted_vals, 95))
        return cls(
            count=n,
            min_ms=float(sorted_vals[0]),
            p50_ms=p50,
            p95_ms=p95,
            max_ms=float(sorted_vals[-1]),
            mean_ms=float(sum(sorted_vals) / n),
        )


# ---------------------------------------------------------------- Audio IO ---


def read_wav_file(path: str) -> Tuple[np.ndarray, int, int]:
    """Read WAV file into float32 array [-1.0, 1.0]. Returns (data, sample_rate, channels)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"WAV file not found: {path}")

    with wave.open(path, "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        raw_bytes = wf.readframes(nframes)

    if sampwidth == 1:
        # 8-bit unsigned PCM
        data = (np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        # 16-bit signed PCM
        data = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 3:
        # 24-bit signed PCM
        raw_arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        reshaped = raw_arr.reshape(-1, 3)
        padded = np.pad(reshaped, ((0, 0), (1, 0)), mode="constant", constant_values=0)
        # Shift to sign-extend 24-bit into 32-bit int
        int32_arr = padded.view(dtype="<i4").flatten() >> 8
        data = int32_arr.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        # 32-bit int or float
        try:
            data = np.frombuffer(raw_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
        except Exception:
            data = np.frombuffer(raw_bytes, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth} bytes")

    if nchannels > 1:
        data = data.reshape(-1, nchannels)

    return data, framerate, nchannels


def load_conversation_audio(
    stereo_path: Optional[str] = None,
    user_path: Optional[str] = None,
    agent_path: Optional[str] = None,
) -> Tuple[AudioTrack, AudioTrack]:
    """Load conversation audio either from stereo WAV or two separate mono WAVs."""
    if stereo_path:
        data, rate, channels = read_wav_file(stereo_path)
        if channels < 2:
            raise ValueError(f"Stereo WAV expected at least 2 channels, found {channels}")
        user_samples = data[:, 0].copy()
        agent_samples = data[:, 1].copy()
        return (
            AudioTrack(name="User (ch0)", samples=user_samples, sample_rate=rate),
            AudioTrack(name="Agent (ch1)", samples=agent_samples, sample_rate=rate),
        )

    if user_path and agent_path:
        u_data, u_rate, u_ch = read_wav_file(user_path)
        a_data, a_rate, a_ch = read_wav_file(agent_path)

        u_samples = u_data[:, 0] if u_ch > 1 else u_data
        a_samples = a_data[:, 0] if a_ch > 1 else a_data

        target_rate = u_rate
        if a_rate != target_rate:
            # Resample agent to user rate using linear interpolation
            orig_times = np.linspace(0, len(a_samples) / a_rate, len(a_samples), endpoint=False)
            new_len = int(len(a_samples) * target_rate / a_rate)
            new_times = np.linspace(0, len(a_samples) / a_rate, new_len, endpoint=False)
            a_samples = np.interp(new_times, orig_times, a_samples).astype(np.float32)

        # Pad shorter to match longer
        max_len = max(len(u_samples), len(a_samples))
        if len(u_samples) < max_len:
            u_samples = np.pad(u_samples, (0, max_len - len(u_samples)))
        if len(a_samples) < max_len:
            a_samples = np.pad(a_samples, (0, max_len - len(a_samples)))

        return (
            AudioTrack(name=f"User ({os.path.basename(user_path)})", samples=u_samples, sample_rate=target_rate),
            AudioTrack(name=f"Agent ({os.path.basename(agent_path)})", samples=a_samples, sample_rate=target_rate),
        )

    raise ValueError("Must provide either stereo_path OR both user_path and agent_path")


# ---------------------------------------------------------------- VAD & Energy ---


def compute_rms_envelope(
    samples: np.ndarray,
    sample_rate: int,
    frame_ms: float = 10.0,
    hop_ms: float = 5.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute short-time RMS envelope and time axis in milliseconds."""
    frame_len = max(1, int(frame_ms * sample_rate / 1000.0))
    hop_len = max(1, int(hop_ms * sample_rate / 1000.0))

    if len(samples) < frame_len:
        return np.array([0.0]), np.array([-100.0])

    num_frames = 1 + (len(samples) - frame_len) // hop_len
    # Vectorized sliding window
    shape = (num_frames, frame_len)
    strides = (samples.strides[0] * hop_len, samples.strides[0])
    frames = np.lib.stride_tricks.as_strided(samples, shape=shape, strides=strides)

    mean_sq = np.mean(frames ** 2, axis=1)
    rms = np.sqrt(np.maximum(mean_sq, 1e-12))
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-6))

    times_ms = (np.arange(num_frames) * hop_len + frame_len / 2.0) * (1000.0 / sample_rate)
    return times_ms, rms_db


def detect_speech_segments(
    samples: np.ndarray,
    sample_rate: int,
    speaker: str,
    threshold_db: float = -38.0,
    min_speech_ms: float = 60.0,
    hangover_ms: float = 180.0,
) -> List[SpeechSegment]:
    """Extract speech intervals with hangover smoothing and minimum duration filtering."""
    times_ms, rms_db = compute_rms_envelope(samples, sample_rate, frame_ms=10.0, hop_ms=5.0)
    is_speech = rms_db >= threshold_db

    segments: List[SpeechSegment] = []
    in_speech = False
    start_ms = 0.0
    last_speech_ms = 0.0

    for t, active, db in zip(times_ms, is_speech, rms_db):
        if active:
            if not in_speech:
                in_speech = True
                start_ms = t
            last_speech_ms = t
        else:
            if in_speech and (t - last_speech_ms >= hangover_ms):
                end_ms = last_speech_ms
                if (end_ms - start_ms) >= min_speech_ms:
                    s_idx = max(0, int(start_ms * sample_rate / 1000.0))
                    e_idx = min(len(samples), int(end_ms * sample_rate / 1000.0))
                    peak = float(np.max(np.abs(samples[s_idx:e_idx]))) if s_idx < e_idx else 1e-6
                    peak_db = 20.0 * math.log10(max(peak, 1e-6))
                    segments.append(
                        SpeechSegment(
                            start_ms=start_ms,
                            end_ms=end_ms,
                            speaker=speaker,
                            peak_dbfs=peak_db,
                        )
                    )
                in_speech = False

    if in_speech and (last_speech_ms - start_ms) >= min_speech_ms:
        s_idx = max(0, int(start_ms * sample_rate / 1000.0))
        e_idx = min(len(samples), int(last_speech_ms * sample_rate / 1000.0))
        peak = float(np.max(np.abs(samples[s_idx:e_idx]))) if s_idx < e_idx else 1e-6
        peak_db = 20.0 * math.log10(max(peak, 1e-6))
        segments.append(
            SpeechSegment(
                start_ms=start_ms,
                end_ms=last_speech_ms,
                speaker=speaker,
                peak_dbfs=peak_db,
            )
        )

    return segments


# ---------------------------------------------------------------- Forensics ---


def analyze_cutoff_fade(
    samples: np.ndarray,
    sample_rate: int,
    cutoff_ms: float,
    window_before_ms: float = 30.0,
    window_after_ms: float = 10.0,
) -> FadeReport:
    """Analyze the audio envelope right before cutoff for DC pop/clicks vs smooth decay.

    Distinguishes:
      - 'hard_clip': Signal abruptly stops at high amplitude (step discontinuity, DC click/pop risk).
      - 'cosine_fade': Signal smoothly decays following a raised-cosine/Hann profile.
      - 'linear_fade': Signal linearly tapers down to zero.
      - 'natural_decay': Long acoustic decay tail without sharp intervention.
    """
    cutoff_idx = int(cutoff_ms * sample_rate / 1000.0)
    w_before = int(window_before_ms * sample_rate / 1000.0)
    w_after = int(window_after_ms * sample_rate / 1000.0)

    start_idx = max(0, cutoff_idx - w_before)
    end_idx = min(len(samples), cutoff_idx + w_after)

    if end_idx - start_idx < 10:
        return FadeReport(
            classification="unknown",
            fade_duration_ms=0.0,
            step_discontinuity=0.0,
            click_risk="LOW",
            linear_corr=0.0,
            cosine_corr=0.0,
            detail="Buffer around cutoff too short",
        )

    pre_cutoff = samples[start_idx:cutoff_idx]
    post_cutoff = samples[cutoff_idx:end_idx]

    steady_peak = float(np.max(np.abs(pre_cutoff))) if len(pre_cutoff) > 0 else 0.0
    if steady_peak < 1e-3:
        return FadeReport(
            classification="silence",
            fade_duration_ms=0.0,
            step_discontinuity=0.0,
            click_risk="LOW",
            linear_corr=1.0,
            cosine_corr=1.0,
            detail="Signal already silent at boundary",
        )

    # Measure boundary zone metrics (2ms pre-cutoff vs 2ms post-cutoff)
    boundary_len = max(2, int(0.002 * sample_rate))
    pre_end_zone = pre_cutoff[-boundary_len:] if len(pre_cutoff) >= boundary_len else pre_cutoff
    post_start_zone = post_cutoff[:boundary_len] if len(post_cutoff) >= boundary_len else post_cutoff

    pre_end_peak = float(np.max(np.abs(pre_end_zone))) if len(pre_end_zone) > 0 else 0.0
    post_start_peak = float(np.max(np.abs(post_start_zone))) if len(post_start_zone) > 0 else 0.0

    last_sample = pre_cutoff[-1] if len(pre_cutoff) > 0 else 0.0
    first_post_sample = post_cutoff[0] if len(post_cutoff) > 0 else 0.0
    direct_step = float(abs(last_sample - first_post_sample))
    rel_step = max(direct_step / steady_peak, pre_end_peak / steady_peak)

    # 1. Hard Clip Detection:
    # If the signal in the final 2ms before cutoff is still strong (>= 25% of steady peak)
    # and drops directly to silence (<= 8% of steady peak), audio abruptly halted mid-wave.
    if (pre_end_peak >= 0.25 * steady_peak) and (post_start_peak <= 0.08 * steady_peak):
        click_risk = "HIGH" if rel_step >= 0.40 else "MEDIUM"
        return FadeReport(
            classification="hard_clip",
            fade_duration_ms=0.0,
            step_discontinuity=float(rel_step),
            click_risk=click_risk,
            linear_corr=0.0,
            cosine_corr=0.0,
            detail=f"Abrupt truncation into silence (step {rel_step:.2f}, click risk {click_risk})",
        )

    # 2. Fade Profile Analysis across candidate fade lengths (6ms to 60ms)
    period = max(2, int(0.0025 * sample_rate))
    half_p = period // 2

    best_lin_err = float("inf")
    best_cos_err = float("inf")
    best_L_lin = 0.0
    best_L_cos = 0.0

    max_L_ms = min(60, int((cutoff_idx - start_idx) * 1000.0 / sample_rate))
    for L_ms in range(6, max_L_ms + 1, 2):
        L_samp = int(L_ms * sample_rate / 1000.0)
        chunk = samples[cutoff_idx - L_samp : cutoff_idx]
        n_c = len(chunk)
        env = np.array([
            float(np.max(np.abs(chunk[max(0, i - half_p) : min(n_c, i + half_p)])))
            for i in range(n_c)
        ], dtype=np.float32)

        t = np.linspace(0.0, np.pi, len(env))
        ideal_cos = steady_peak * 0.5 * (1.0 + np.cos(t))
        ideal_lin = steady_peak * (1.0 - t / np.pi)

        err_lin = float(np.mean((env - ideal_lin) ** 2))
        err_cos = float(np.mean((env - ideal_cos) ** 2))

        if err_lin < best_lin_err:
            best_lin_err = err_lin
            best_L_lin = float(L_ms)
        if err_cos < best_cos_err:
            best_cos_err = err_cos
            best_L_cos = float(L_ms)

    if best_cos_err < best_lin_err:
        classification = "cosine_fade"
        best_L = best_L_cos
        detail = f"Smooth raised-cosine fade-out over {best_L:.1f}ms (MSE {best_cos_err:.5f})"
    else:
        classification = "linear_fade"
        best_L = best_L_lin
        detail = f"Linear ramp fade-out over {best_L:.1f}ms (MSE {best_lin_err:.5f})"

    click_risk = "LOW" if best_L >= 5.0 else "MEDIUM"

    return FadeReport(
        classification=classification,
        fade_duration_ms=best_L,
        step_discontinuity=float(rel_step),
        click_risk=click_risk,
        linear_corr=float(1.0 - min(1.0, best_lin_err)),
        cosine_corr=float(1.0 - min(1.0, best_cos_err)),
        detail=detail,
    )


def compute_conversation_metrics(
    user_segments: List[SpeechSegment],
    agent_segments: List[SpeechSegment],
    agent_samples: np.ndarray,
    sample_rate: int,
) -> Tuple[List[TurnTakingEvent], List[BargeInEvent], List[BackchannelEvent]]:
    """Compute Turn-Taking, Barge-in Kill, and Backchannels across conversation turns."""
    turn_taking_events: List[TurnTakingEvent] = []
    barge_in_events: List[BargeInEvent] = []
    backchannels: List[BackchannelEvent] = []

    # 1. Identify Backchannels:
    # Short agent affirmations (<1.2s, e.g. "mm-hmm", "yeah") during long user turns
    # without agent taking the conversational floor.
    agent_is_backchannel = [False] * len(agent_segments)

    for i, a in enumerate(agent_segments):
        if a.duration_ms <= 1200.0:
            # Check if this occurs during a user speech turn or between closely linked user turns
            overlapping_user = [
                u for u in user_segments
                if (u.start_ms <= a.start_ms and u.end_ms >= a.end_ms)
                or (u.start_ms <= a.start_ms and abs(u.end_ms - a.start_ms) < 400.0)
                or (abs(a.end_ms - u.start_ms) < 400.0)
            ]
            # Long user context: total user speech surrounding this is >= 1500ms
            user_speech_context = sum(u.duration_ms for u in overlapping_user)
            # Verify agent does NOT follow up with immediate long speech (didn't take floor)
            next_agent_turn = agent_segments[i + 1] if i + 1 < len(agent_segments) else None
            takes_floor = next_agent_turn and (next_agent_turn.start_ms - a.end_ms < 600.0)

            if user_speech_context >= 1200.0 and not takes_floor:
                agent_is_backchannel[i] = True
                backchannels.append(
                    BackchannelEvent(
                        speaker="Agent",
                        start_ms=a.start_ms,
                        end_ms=a.end_ms,
                        duration_ms=a.duration_ms,
                        context_speaker="User",
                        context_turn_ms=user_speech_context,
                        peak_dbfs=a.peak_dbfs,
                    )
                )

    # 2. Turn-Taking Latency:
    # Milliseconds between user speech cutoff (RMS drops below threshold) and agent first audio phoneme.
    # Exclude backchannels from taking the turn floor if user continues.
    for u_idx, u in enumerate(user_segments):
        # Find the next non-backchannel agent turn starting at or shortly after user finishes
        candidate_agents = [
            (a_idx, a) for a_idx, a in enumerate(agent_segments)
            if not agent_is_backchannel[a_idx]
            and (a.start_ms >= u.end_ms - 200.0)  # Allow slight conversational latching/overlap
            and (a.start_ms - u.end_ms <= 8000.0)  # Conversational response window
        ]
        if not candidate_agents:
            continue

        a_idx, first_agent = candidate_agents[0]
        # Verify no intermediate user turn intervened
        intervening_users = [
            u_other for u_other in user_segments
            if u_other.start_ms > u.end_ms and u_other.start_ms < first_agent.start_ms
        ]
        if intervening_users:
            continue

        latency_ms = max(0.0, first_agent.start_ms - u.end_ms)
        turn_taking_events.append(
            TurnTakingEvent(
                user_turn_index=u_idx,
                agent_turn_index=a_idx,
                user_end_ms=u.end_ms,
                agent_start_ms=first_agent.start_ms,
                latency_ms=latency_ms,
            )
        )

    # 3. Interruption / Barge-in Kill Latency:
    # When user starts speaking during agent speech, how many ms until agent waveform drops to silence?
    for a_idx, a in enumerate(agent_segments):
        if agent_is_backchannel[a_idx]:
            continue

        # Look for user starting to speak while agent is active
        interrupting_users = [
            u for u in user_segments
            if (u.start_ms > a.start_ms + 100.0) and (u.start_ms < a.end_ms - 20.0)
        ]
        if not interrupting_users:
            continue

        u_intr = interrupting_users[0]
        kill_latency_ms = max(0.0, a.end_ms - u_intr.start_ms)

        # Analyze fade-out characteristics at agent cutoff
        fade_report = analyze_cutoff_fade(agent_samples, sample_rate, a.end_ms)

        barge_in_events.append(
            BargeInEvent(
                agent_turn_index=a_idx,
                agent_start_ms=a.start_ms,
                agent_end_ms=a.end_ms,
                user_start_ms=u_intr.start_ms,
                kill_latency_ms=kill_latency_ms,
                fade=fade_report,
            )
        )

    return turn_taking_events, barge_in_events, backchannels


# ---------------------------------------------------------------- Reporter ---


def format_report(
    track_user: AudioTrack,
    track_agent: AudioTrack,
    user_segments: List[SpeechSegment],
    agent_segments: List[SpeechSegment],
    turn_taking: List[TurnTakingEvent],
    barge_ins: List[BargeInEvent],
    backchannels: List[BackchannelEvent],
) -> str:
    """Format analysis into clean, monochromatic ASCII summary tables."""
    tt_vals = [e.latency_ms for e in turn_taking]
    barge_vals = [e.kill_latency_ms for e in barge_ins]

    tt_stats = LatencyStats.from_values(tt_vals)
    barge_stats = LatencyStats.from_values(barge_vals)

    lines: List[str] = []
    sep = "+------------------------------------------------------------------------------+"

    lines.append(sep)
    lines.append("| VOICE FORENSICS & CONVERSATION LATENCY REPORT                                |")
    lines.append(sep)
    lines.append(f"| User Track  : {track_user.name:<63} |")
    lines.append(f"| Agent Track : {track_agent.name:<63} |")
    lines.append(f"| Sample Rate : {track_user.sample_rate} Hz | Duration: {track_user.duration_s:.2f}s | Active Turns: U={len(user_segments)} / A={len(agent_segments)} |")
    lines.append(sep)
    lines.append("")

    # Section 1: Turn-Taking Latency
    lines.append("1. TURN-TAKING LATENCY (User speech cutoff -> Agent first audio phoneme)")
    lines.append(f"   Events Detected: {tt_stats.count}")
    if tt_stats.count > 0:
        lines.append(
            f"   Distribution   : Min: {tt_stats.min_ms:6.1f}ms | p50: {tt_stats.p50_ms:6.1f}ms | "
            f"p95: {tt_stats.p95_ms:6.1f}ms | Max: {tt_stats.max_ms:6.1f}ms | Mean: {tt_stats.mean_ms:6.1f}ms"
        )
        lines.append("   Event Log:")
        for idx, e in enumerate(turn_taking, 1):
            lines.append(
                f"   +-- [Turn {idx:2d}] User end: {e.user_end_ms/1000.0:6.2f}s -> "
                f"Agent start: {e.agent_start_ms/1000.0:6.2f}s | Latency: {e.latency_ms:6.1f}ms"
            )
    else:
        lines.append("   No qualifying sequential turns detected.")
    lines.append("")

    # Section 2: Barge-in / Interruption Kill Latency
    lines.append("2. INTERRUPTION & BARGE-IN KILL LATENCY (User barge onset -> Agent audio silence)")
    lines.append(f"   Interruptions Detected: {barge_stats.count}")
    if barge_stats.count > 0:
        lines.append(
            f"   Distribution          : Min: {barge_stats.min_ms:6.1f}ms | p50: {barge_stats.p50_ms:6.1f}ms | "
            f"p95: {barge_stats.p95_ms:6.1f}ms | Max: {barge_stats.max_ms:6.1f}ms | Mean: {barge_stats.mean_ms:6.1f}ms"
        )
        lines.append("   Event Log & Cutoff Characteristics:")
        for idx, b in enumerate(barge_ins, 1):
            f = b.fade
            lines.append(
                f"   +-- [Barge {idx:2d}] User speech @ {b.user_start_ms/1000.0:6.2f}s -> "
                f"Agent silence @ {b.agent_end_ms/1000.0:6.2f}s | Kill: {b.kill_latency_ms:6.1f}ms"
            )
            lines.append(
                f"       |   Fade Profile: {f.classification} ({f.detail}) | Click Risk: {f.click_risk}"
            )
    else:
        lines.append("   No user interruptions during agent speech detected.")
    lines.append("")

    # Section 3: Cutoff Fade Characteristics Summary
    lines.append("3. CUTOFF FADE & CLICK AUDIT")
    if barge_ins:
        hard_clips = sum(1 for b in barge_ins if b.fade.classification == "hard_clip")
        smooth_fades = len(barge_ins) - hard_clips
        lines.append(f"   Total Cutoffs Evaluated : {len(barge_ins)}")
        lines.append(f"   Smooth Decays / Fades   : {smooth_fades}")
        lines.append(f"   Hard Clips (Click Risk) : {hard_clips}")
        if hard_clips > 0:
            lines.append("   WARNING: Hard audio truncation detected. Apply Hann/cosine fade-out to kill buffer.")
        else:
            lines.append("   PASS: Audio terminations exhibit smooth decay with low DC click risk.")
    else:
        lines.append("   No cutoff events to evaluate.")
    lines.append("")

    # Section 4: Backchannels
    lines.append("4. BACKCHANNEL DETECTIONS (<1.2s vocal affirmations without taking floor)")
    lines.append(f"   Backchannels Detected   : {len(backchannels)}")
    if backchannels:
        for idx, bc in enumerate(backchannels, 1):
            lines.append(
                f"   +-- [BC {idx:2d}] {bc.speaker} affirmed @ {bc.start_ms/1000.0:6.2f}s "
                f"(dur: {bc.duration_ms:5.1f}ms, peak: {bc.peak_dbfs:5.1f} dBFS) during {bc.context_speaker} turn"
            )
    else:
        lines.append("   No short backchannel affirmations detected.")

    lines.append(sep)
    return "\n".join(lines)


def export_json_dict(
    track_user: AudioTrack,
    track_agent: AudioTrack,
    turn_taking: List[TurnTakingEvent],
    barge_ins: List[BargeInEvent],
    backchannels: List[BackchannelEvent],
) -> dict[str, Any]:
    """Return dictionary representation for JSON export."""
    tt_stats = LatencyStats.from_values([e.latency_ms for e in turn_taking])
    barge_stats = LatencyStats.from_values([e.kill_latency_ms for e in barge_ins])

    return {
        "metadata": {
            "duration_s": track_user.duration_s,
            "sample_rate": track_user.sample_rate,
            "user_track": track_user.name,
            "agent_track": track_agent.name,
        },
        "turn_taking": {
            "stats": dataclasses.asdict(tt_stats),
            "events": [dataclasses.asdict(e) for e in turn_taking],
        },
        "barge_in": {
            "stats": dataclasses.asdict(barge_stats),
            "events": [
                {
                    "agent_turn_index": b.agent_turn_index,
                    "agent_start_ms": b.agent_start_ms,
                    "agent_end_ms": b.agent_end_ms,
                    "user_start_ms": b.user_start_ms,
                    "kill_latency_ms": b.kill_latency_ms,
                    "fade": dataclasses.asdict(b.fade),
                }
                for b in barge_ins
            ],
        },
        "backchannels": [dataclasses.asdict(bc) for bc in backchannels],
    }


# ---------------------------------------------------------------- CLI ---


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline Voice Forensics and Latency Analyzer for Pet-Talk & Voice Agents."
    )
    parser.add_argument("stereo_wav", nargs="?", help="Path to stereo WAV (ch0=User, ch1=Agent)")
    parser.add_argument("--stereo", dest="opt_stereo", help="Explicit path to stereo WAV")
    parser.add_argument("--user", help="Path to User mono WAV")
    parser.add_argument("--agent", help="Path to Agent mono WAV")
    parser.add_argument("--threshold-db", type=float, default=-38.0, help="VAD energy threshold in dBFS (default: -38)")
    parser.add_argument("--hangover-ms", type=float, default=180.0, help="Speech hangover smoothing ms (default: 180)")
    parser.add_argument("--json", action="store_true", help="Output JSON report instead of ASCII table")
    parser.add_argument("-o", "--output", help="Save report to specified output path")

    args = parser.parse_args(argv)

    stereo = args.opt_stereo or args.stereo_wav
    if not stereo and not (args.user and args.agent):
        parser.error("Must specify a stereo WAV or both --user and --agent WAV files.")

    try:
        track_user, track_agent = load_conversation_audio(
            stereo_path=stereo,
            user_path=args.user,
            agent_path=args.agent,
        )
    except Exception as e:
        sys.stderr.write(f"Error loading audio: {e}\n")
        return 1

    user_segments = detect_speech_segments(
        track_user.samples,
        track_user.sample_rate,
        speaker="User",
        threshold_db=args.threshold_db,
        hangover_ms=args.hangover_ms,
    )
    agent_segments = detect_speech_segments(
        track_agent.samples,
        track_agent.sample_rate,
        speaker="Agent",
        threshold_db=args.threshold_db,
        hangover_ms=args.hangover_ms,
    )

    turn_taking, barge_ins, backchannels = compute_conversation_metrics(
        user_segments=user_segments,
        agent_segments=agent_segments,
        agent_samples=track_agent.samples,
        sample_rate=track_agent.sample_rate,
    )

    if args.json:
        result = export_json_dict(track_user, track_agent, turn_taking, barge_ins, backchannels)
        text = json.dumps(result, indent=2)
    else:
        text = format_report(
            track_user,
            track_agent,
            user_segments,
            agent_segments,
            turn_taking,
            barge_ins,
            backchannels,
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Report saved to {args.output}")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
