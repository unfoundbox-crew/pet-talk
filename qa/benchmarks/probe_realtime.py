#!/usr/bin/env python3
"""qa/benchmarks/probe_realtime.py — Wire-Level Realtime WebSocket Latency Probe.

Benchmarks live wire-level voice latency across:
  1. Pet-Talk Local WebSocket (default ws://127.0.0.1:8089/ws)
  2. OpenAI Realtime API (wss://api.openai.com/v1/realtime) when OPENAI_API_KEY is present

Measures:
  - Socket TTFA (Time to First Audio frame / phoneme delivery)
  - Cancellation / Barge-in Kill Latency: Sends barge frame mid-sentence and measures
    time until silence/ack and leaked audio bytes/frames.

Outputs clean monochromatic ASCII summary and exports a structured JSON report.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
    HAVE_WEBSOCKETS = True
except ImportError:
    HAVE_WEBSOCKETS = False


def generate_synthetic_pcm16(
    duration_s: float = 0.5,
    sample_rate: int = 16000,
    freq: float = 440.0,
    amplitude: float = 0.4,
) -> bytes:
    """Generate pure PCM16 mono sine audio for wire testing."""
    num_samples = int(duration_s * sample_rate)
    frames = bytearray()
    for i in range(num_samples):
        t = float(i) / sample_rate
        val = int(32767.0 * amplitude * math.sin(2.0 * math.pi * freq * t))
        val = max(-32768, min(32767, val))
        frames.extend(struct.pack("<h", val))
    return bytes(frames)


# -------------------------------------------------------- Pet-Talk Local Probe ---


async def run_pettalk_probe(
    ws_url: str,
    timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """Run TTFA and Barge-In Cancellation probe against Pet-Talk local duplex WebSocket."""
    report: Dict[str, Any] = {
        "target": "pet-talk-local",
        "ws_url": ws_url,
        "connected": False,
        "ttfa_test": {},
        "cancellation_test": {},
    }

    pcm_data = generate_synthetic_pcm16(duration_s=0.5, sample_rate=16000)
    pcm_b64 = base64.b64encode(pcm_data).decode("ascii")

    try:
        async with ws_connect(ws_url, max_size=8 * 1024 * 1024) as ws:
            report["connected"] = True

            # Handshake: receive initial state.idle
            init_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
            init_turn = init_msg.get("turn_id", "probe-init")

            # --- Test 1: Time to First Audio (TTFA) ---
            turn_1 = f"probe-ttfa-{int(time.time()*1000)}"
            await ws.send(json.dumps({"type": "user.start", "turn_id": turn_1}))
            start_ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))

            t_stop_sent = time.perf_counter()
            await ws.send(json.dumps({
                "type": "user.stop",
                "turn_id": turn_1,
                "pcm_b64": pcm_b64,
                "sample_rate": 16000,
            }))

            first_frame_ms: Optional[float] = None
            first_audio_ms: Optional[float] = None
            first_frame_type: Optional[str] = None
            audio_url: Optional[str] = None
            received_frames: List[Dict[str, Any]] = []

            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                dt = (time.perf_counter() - t_stop_sent) * 1000.0
                mtype = msg.get("type", "")
                received_frames.append({"type": mtype, "dt_ms": dt})

                if first_frame_ms is None and mtype != "state.listening":
                    first_frame_ms = dt
                    first_frame_type = mtype

                if mtype in ("agent.stall", "agent.sentence") and first_audio_ms is None:
                    first_audio_ms = dt

                if msg.get("audio_url") and audio_url is None:
                    audio_url = msg.get("audio_url")

                if mtype == "agent.done":
                    break

            report["ttfa_test"] = {
                "success": first_audio_ms is not None,
                "time_to_first_message_ms": round(first_frame_ms or 0.0, 2),
                "first_message_type": first_frame_type,
                "time_to_first_audio_ms": round(first_audio_ms or 0.0, 2),
                "audio_url": audio_url,
                "frame_sequence": [f["type"] for f in received_frames],
            }

            # --- Test 2: Barge-in Cancellation Latency & Leakage ---
            turn_2 = f"probe-barge-{int(time.time()*1000)}"
            await ws.send(json.dumps({"type": "user.start", "turn_id": turn_2}))
            await asyncio.wait_for(ws.recv(), timeout=timeout_s)  # state.listening

            # Trigger worker turn that produces speech
            await ws.send(json.dumps({
                "type": "user.stop",
                "turn_id": turn_2,
                "pcm_b64": pcm_b64,
                "sample_rate": 16000,
            }))

            # Wait for first speaking frame before firing cancellation
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if m.get("type") in ("agent.stall", "state.speaking", "agent.sentence"):
                    break

            # Mid-sentence barge-in kill
            t_barge_sent = time.perf_counter()
            await ws.send(json.dumps({"type": "barge", "turn_id": turn_2}))

            leaked_frames: List[Dict[str, Any]] = []
            cancellation_latency_ms: Optional[float] = None
            ack_msg: Optional[Dict[str, Any]] = None

            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                dt = (time.perf_counter() - t_barge_sent) * 1000.0

                if m.get("type") == "state.listening" and (
                    m.get("barged_turn") == turn_2 or "barged_turn" in m
                ):
                    cancellation_latency_ms = dt
                    ack_msg = m
                    break
                else:
                    leaked_frames.append({"type": m.get("type"), "dt_ms": round(dt, 2)})

            report["cancellation_test"] = {
                "success": cancellation_latency_ms is not None,
                "cancellation_latency_ms": round(cancellation_latency_ms or 0.0, 2),
                "leaked_frames_count": len(leaked_frames),
                "leaked_frames": leaked_frames,
                "dropped_queue_count": ack_msg.get("dropped") if ack_msg else 0,
                "barged_turn_ack": ack_msg.get("barged_turn") if ack_msg else None,
            }

    except Exception as e:
        report["error"] = str(e)

    return report


# ------------------------------------------------------- OpenAI Realtime Probe ---


async def run_openai_realtime_probe(
    api_key: str,
    model: str = "gpt-4o-realtime-preview-2024-10-01",
    timeout_s: float = 12.0,
) -> Dict[str, Any]:
    """Run TTFA and Cancellation probe against OpenAI Realtime API."""
    report: Dict[str, Any] = {
        "target": "openai-realtime",
        "model": model,
        "connected": False,
        "ttfa_test": {},
        "cancellation_test": {},
    }

    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }

    pcm_data = generate_synthetic_pcm16(duration_s=0.6, sample_rate=24000)
    pcm_b64 = base64.b64encode(pcm_data).decode("ascii")

    try:
        async with ws_connect(url, extra_headers=headers, max_size=8 * 1024 * 1024) as ws:
            report["connected"] = True

            # Handshake: session.created
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))

            # Configure session for manual turn detection (deterministic latency measurement)
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": "Reply with exactly one word: 'Ready'.",
                    "voice": "alloy",
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "turn_detection": None,
                },
            }))

            # Wait for session.updated
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if m.get("type") == "session.updated":
                    break

            # --- Test 1: Socket TTFA ---
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": pcm_b64,
            }))
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

            t_commit = time.perf_counter()
            await ws.send(json.dumps({"type": "response.create"}))

            first_text_ms: Optional[float] = None
            first_audio_ms: Optional[float] = None
            audio_bytes_received = 0

            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                dt = (time.perf_counter() - t_commit) * 1000.0
                mtype = m.get("type", "")

                if "text.delta" in mtype and first_text_ms is None:
                    first_text_ms = dt
                elif mtype == "response.audio.delta" and first_audio_ms is None:
                    first_audio_ms = dt
                    delta_b64 = m.get("delta", "")
                    audio_bytes_received += len(base64.b64decode(delta_b64))
                elif mtype == "response.done":
                    break

            report["ttfa_test"] = {
                "success": first_audio_ms is not None,
                "time_to_first_text_ms": round(first_text_ms or 0.0, 2) if first_text_ms else None,
                "time_to_first_audio_ms": round(first_audio_ms or 0.0, 2),
                "total_audio_bytes": audio_bytes_received,
            }

            # --- Test 2: Cancellation / Mid-Stream Barge ---
            # Request a longer response to allow mid-flight cancel
            await ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "instructions": "Count out loud slowly from one to twenty.",
                },
            }))

            # Await first audio delta, then immediately cancel
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if m.get("type") == "response.audio.delta":
                    break

            t_cancel = time.perf_counter()
            await ws.send(json.dumps({"type": "response.cancel"}))

            cancel_ack_ms: Optional[float] = None
            leaked_audio_bytes = 0
            leaked_deltas = 0

            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                dt = (time.perf_counter() - t_cancel) * 1000.0
                mtype = m.get("type", "")

                if mtype == "response.audio.delta":
                    leaked_deltas += 1
                    leaked_audio_bytes += len(base64.b64decode(m.get("delta", "")))
                elif mtype in ("response.cancelled", "response.done"):
                    cancel_ack_ms = dt
                    break

            report["cancellation_test"] = {
                "success": cancel_ack_ms is not None,
                "cancellation_latency_ms": round(cancel_ack_ms or 0.0, 2),
                "leaked_audio_frames": leaked_deltas,
                "leaked_audio_bytes": leaked_audio_bytes,
            }

    except Exception as e:
        report["error"] = str(e)

    return report


# ------------------------------------------------------------- Monochromatic ASCII View ---


def format_probe_report(data: Dict[str, Any]) -> str:
    """Render probe metrics in clean, monochromatic ASCII format."""
    lines: List[str] = []
    sep = "+------------------------------------------------------------------------------+"
    lines.append(sep)
    lines.append("| WIRE-LEVEL REALTIME WEBSOCKET LATENCY PROBE                                  |")
    lines.append(sep)
    lines.append(f"| Target        : {data.get('target', 'unknown'):<61} |")
    if "ws_url" in data:
        lines.append(f"| URL           : {data.get('ws_url', ''):<61} |")
    if "model" in data:
        lines.append(f"| Model         : {data.get('model', ''):<61} |")
    lines.append(f"| Connected     : {str(data.get('connected', False)):<61} |")
    lines.append(sep)
    lines.append("")

    if "error" in data:
        lines.append(f"ERROR: {data['error']}")
        lines.append(sep)
        return "\n".join(lines)

    # 1. TTFA Test
    ttfa = data.get("ttfa_test", {})
    lines.append("1. TIME TO FIRST AUDIO (TTFA)")
    if ttfa.get("success"):
        ttfa_ms = ttfa.get("time_to_first_audio_ms")
        lines.append(f"   Socket TTFA           : {ttfa_ms:6.1f} ms")
        if ttfa.get("time_to_first_message_ms"):
            lines.append(f"   Time to First Frame   : {ttfa.get('time_to_first_message_ms'):6.1f} ms ({ttfa.get('first_message_type')})")
        if ttfa.get("audio_url"):
            lines.append(f"   Audio Endpoint        : {ttfa.get('audio_url')}")
        if ttfa.get("frame_sequence"):
            lines.append(f"   Frame Flow            : {' -> '.join(ttfa.get('frame_sequence'))}")
    else:
        lines.append("   TTFA test did not receive audio response.")
    lines.append("")

    # 2. Cancellation Test
    cancel = data.get("cancellation_test", {})
    lines.append("2. CANCELLATION & BARGE-IN LATENCY")
    if cancel.get("success"):
        lat_ms = cancel.get("cancellation_latency_ms")
        lines.append(f"   Cancellation Latency  : {lat_ms:6.1f} ms")
        if "leaked_frames_count" in cancel:
            lines.append(f"   Post-Barge Frames     : {cancel.get('leaked_frames_count')} frames leaked")
        if "leaked_audio_bytes" in cancel:
            lines.append(f"   Post-Barge Audio Leak : {cancel.get('leaked_audio_bytes')} bytes")
        if "dropped_queue_count" in cancel:
            lines.append(f"   Sentences Dropped     : {cancel.get('dropped_queue_count')} from SpeakQueue")
        if "barged_turn_ack" in cancel:
            lines.append(f"   Barge Ack Turn ID     : {cancel.get('barged_turn_ack')}")
    else:
        lines.append("   Cancellation test did not complete.")

    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI ---


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire-Level Realtime WebSocket Latency Probe for Pet-Talk & OpenAI Realtime."
    )
    parser.add_argument("--url", help="Pet-Talk WebSocket URL (default: ws://127.0.0.1:8089/ws)")
    parser.add_argument("--port", type=int, default=8089, help="Local Pet-Talk server port (default: 8089)")
    parser.add_argument("--openai", action="store_true", help="Benchmark OpenAI Realtime API instead of local")
    parser.add_argument("--model", default="gpt-4o-realtime-preview-2024-10-01", help="OpenAI Realtime model")
    parser.add_argument("-o", "--output", default="qa/benchmarks/realtime_probe_report.json", help="Path to save JSON report")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")

    args = parser.parse_args(argv)

    if not HAVE_WEBSOCKETS:
        sys.stderr.write("Error: 'websockets' library is required. Install via pip install websockets\n")
        return 1

    if args.openai:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            sys.stderr.write("Error: OPENAI_API_KEY environment variable required for --openai\n")
            return 1
        data = asyncio.run(run_openai_realtime_probe(api_key=api_key, model=args.model))
    else:
        ws_url = args.url or f"ws://127.0.0.1:{args.port}/ws"
        data = asyncio.run(run_pettalk_probe(ws_url=ws_url))

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if not args.json:
            print(f"Report saved to {args.output}\n")

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(format_probe_report(data))

    return 0 if data.get("connected") else 1


if __name__ == "__main__":
    sys.exit(main())
