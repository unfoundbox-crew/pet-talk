#!/usr/bin/env python3
"""qa/benchmarks/mic_check.py — Darwin Audio I/O Mic Capture Probe & Energy Verification.

Verifies macOS default audio input device:
1. Detects system recording tool (`sox`, `rec`, or `ffmpeg`).
2. Records exactly 2.0 seconds of linear16 mono 16kHz PCM audio.
3. Computes sample count, peak amplitude, and RMS energy.
4. Asserts that real acoustic energy was captured (peak > 0, RMS > 0.0, not digital silence).
"""
from __future__ import annotations

import argparse
import array
import math
import os
import sys
import time
from typing import Dict, Any

# Ensure repository root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cli.audio import AudioRecorder, calculate_rms, find_recorder


def probe_mic(duration_s: float = 2.0) -> Dict[str, Any]:
    """Record audio for duration_s and analyze acoustic energy."""
    cmd = find_recorder()
    tool = cmd[0] if cmd else "unknown"

    recorder = AudioRecorder(chunk_bytes=1024)
    recorder.start()

    t_start = time.perf_counter()
    chunks: list[bytes] = []

    while time.perf_counter() - t_start < duration_s:
        chunk = recorder.read_chunk()
        if chunk:
            chunks.append(chunk)
        else:
            time.sleep(0.005)

    all_pcm = recorder.stop()
    elapsed = time.perf_counter() - t_start

    # Fallback to chunks if recorder.stop() buffer was already partially read
    if not all_pcm and chunks:
        all_pcm = b"".join(chunks)

    num_samples = len(all_pcm) // 2
    shorts = array.array("h")
    if num_samples > 0:
        shorts.frombytes(all_pcm[: num_samples * 2])
        peak_amp = max(abs(s) for s in shorts)
    else:
        peak_amp = 0

    rms_val = calculate_rms(all_pcm)
    peak_pct = (peak_amp / 32768.0) * 100.0
    effective_sec = num_samples / 16000.0

    # Assertion checks
    has_audio = len(all_pcm) > 0
    has_samples = num_samples > (16000 * 1.2)  # at least 1.2s captured for a 2s window
    is_non_silent = peak_amp > 0 and rms_val > 0.0

    return {
        "tool": tool,
        "command": " ".join(cmd),
        "requested_duration_s": duration_s,
        "elapsed_s": round(elapsed, 3),
        "effective_duration_s": round(effective_sec, 3),
        "bytes_captured": len(all_pcm),
        "samples_captured": num_samples,
        "peak_amplitude": peak_amp,
        "peak_pct": round(peak_pct, 2),
        "rms_energy": round(rms_val, 2),
        "is_digital_silence": peak_amp == 0 or rms_val == 0.0,
        "has_audio": has_audio,
        "has_samples": has_samples,
        "is_non_silent": is_non_silent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mic_check",
        description="Darwin Audio Input Probe — Record 2s & verify acoustic energy.",
    )
    parser.add_argument(
        "--duration",
        "-d",
        type=float,
        default=2.0,
        help="Recording duration in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Quiet mode: only output exit code",
    )
    args = parser.parse_args(argv)

    if not args.quiet:
        print(f"+-- Darwin Mic Acoustic Probe ({args.duration:.1f}s)")
        print(f"| Initializing default audio input capture...")

    try:
        receipt = probe_mic(duration_s=args.duration)
    except Exception as e:
        if not args.quiet:
            print(f"Error: Failed to record from microphone: {e}")
        return 1

    if not args.quiet:
        print(f"| Recorder binary:     {receipt['tool']}")
        print(f"| Capture command:     {receipt['command']}")
        print(f"| Elapsed wall time:   {receipt['elapsed_s']}s")
        print(f"| Effective audio:     {receipt['effective_duration_s']}s ({receipt['samples_captured']} samples, {receipt['bytes_captured']} bytes)")
        print(f"| Peak amplitude:      {receipt['peak_amplitude']} / 32768 ({receipt['peak_pct']}%)")
        print(f"| RMS energy:          {receipt['rms_energy']}")
        print(f"| Digital silence:     {receipt['is_digital_silence']}")
        print(f"+------------------------------------------------------")

    # Assertions
    assert receipt["has_audio"], "No audio bytes captured from microphone"
    assert receipt["has_samples"], f"Too few samples captured: {receipt['samples_captured']} < expected"
    assert receipt["peak_amplitude"] > 0, "Captured peak amplitude is 0 (digital silence)"
    assert receipt["rms_energy"] > 0.0, "Captured RMS energy is 0.0 (digital silence)"

    if not args.quiet:
        print("-> [PASS] Active default microphone captured real acoustic energy.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
