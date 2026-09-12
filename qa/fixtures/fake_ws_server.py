#!/usr/bin/env python3
"""A tiny stand-in for `server/app.py`'s `/ws` + `/health`, for driving the
native Swift turn engine headless.

Why this exists: the native turn engine (cli/hotkey/WSClient.swift,
TurnController.swift) has to be provable without a real daemon, a real mic, a
real model, or a speaker. The real server needs all four. This replays a canned
frame sequence instead — same frame names, same keys, same order as
docs/SPEC.md §4 — and writes every frame it *receives* to a JSONL file so the
test can assert on the wire, not on the client's own story about the wire.

It is a fixture, never a fallback: nothing in the product may point at it.

Usage:
    python3 qa/fixtures/fake_ws_server.py --script normal --log /tmp/frames.jsonl

Prints ONE line of JSON on stdout once it is listening:

    {"ok": true, "port": 54321, "ws_url": "ws://127.0.0.1:54321/ws"}

then serves until SIGTERM/SIGINT.

Scripts:
  normal  — a clean two-sentence answer turn, then agent.done path=answer.
  barge   — one sentence's worth of chunks, and after the client's `barge`
            frame arrives it keeps sending LATE chunks for the killed turn
            (exactly what an in-flight socket does), then state.listening +
            agent.done path=interrupted. A client that plays a late chunk
            fails its own barge contract.
  noaudio — agent.error reason tts_unavailable instead of any audio.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import signal
import struct
import sys
import time
import wave

try:
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover - reported, never silently skipped here
    serve = None  # type: ignore

#: server/auth.py's header name, lowercased (it compares case-insensitively).
STUDIO_TOKEN_HEADER = "x-studio-token"
#: server/auth.py's WS_TOKEN_QUERY.
WS_TOKEN_QUERY = "token"


def make_wav(ms: int = 120, sample_rate: int = 16000, freq: float = 220.0) -> bytes:
    """One self-contained RIFF/WAVE file, 16-bit mono — the shape every
    `agent.chunk` carries (server/speech.py writes a whole WAV per chunk, no
    mime or sample_rate field on the frame, so the header is the contract)."""
    n = int(sample_rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        v = int(8000 * math.sin(2 * math.pi * freq * i / sample_rate))
        frames += struct.pack("<h", v)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def word_times(text: str, total_ms: int) -> list[dict]:
    """server/speak_queue.py's estimate_word_times shape, exactly:
    {"word", "start_ms", "end_ms", "estimated"}."""
    words = [w for w in text.split() if w]
    if not words or total_ms <= 0:
        return []
    per = total_ms / len(words)
    out = []
    for i, w in enumerate(words):
        out.append(
            {
                "word": w,
                "start_ms": int(i * per),
                "end_ms": int((i + 1) * per),
                "estimated": True,
            }
        )
    return out


class FakeStudio:
    def __init__(self, args):
        self.args = args
        self.log_path = args.log
        self._log_fh = open(self.log_path, "w", encoding="utf-8") if self.log_path else None
        self.wav_b64 = None
        self.barged = asyncio.Event()

    # ---- receive side -----------------------------------------------------
    def record(self, frame: dict) -> None:
        if self._log_fh is None:
            return
        # `_recv_monotonic` is fixture metadata, not part of the wire: it is the
        # server-side arrival time on this machine's monotonic clock, so a
        # measurement harness can compare "hotkey pressed" to "frame arrived"
        # across processes. Underscored so no assertion mistakes it for a field.
        out = dict(frame)
        out["_recv_monotonic"] = time.monotonic()
        self._log_fh.write(json.dumps(out, sort_keys=True) + "\n")
        self._log_fh.flush()

    # ---- send side --------------------------------------------------------
    async def send(self, ws, ftype: str, turn_id: str, **fields) -> None:
        frame = {"type": ftype, "turn_id": turn_id}
        frame.update(fields)
        await ws.send(json.dumps(frame))

    async def chunk(self, ws, turn_id: str, seq: int, chunk_no: int, final: bool) -> None:
        import base64

        if self.wav_b64 is None:
            self.wav_b64 = base64.b64encode(make_wav()).decode("ascii")
        await self.send(
            ws,
            "agent.chunk",
            turn_id,
            seq=seq,
            chunk_no=chunk_no,
            audio_b64="" if final and self.args.empty_terminator else self.wav_b64,
            url=f"/audio/{turn_id}-s{seq}-c{chunk_no}",
            final=final,
        )

    async def sentence(self, ws, turn_id: str, seq: int, text: str) -> None:
        await self.send(
            ws,
            "agent.sentence",
            turn_id,
            seq=seq,
            text=text,
            audio_url=f"/audio/{turn_id}-s{seq}",
            word_times=word_times(text, 600),
            estimated=True,
            stream_url=f"/audio/{turn_id}-s{seq}-c0",
            chunked=True,
        )

    # ---- the scripts ------------------------------------------------------
    async def run_answer(self, ws, turn_id: str) -> None:
        await self.send(ws, "transcript.user", turn_id, text="what is the weather", handover=False)
        await self.send(ws, "agent.stall", turn_id, phrase_id="one_sec", text="One second.")
        await self.send(ws, "state.thinking", turn_id)
        await self.send(ws, "state.speaking", turn_id)
        for seq, text in ((1, "It is bright and cold today."), (2, "Take the warm coat.")):
            for chunk_no in range(2):
                await self.chunk(ws, turn_id, seq, chunk_no, final=(chunk_no == 1))
                await asyncio.sleep(0.02)
            await self.sentence(ws, turn_id, seq, text)
        await self.send(ws, "agent.done", turn_id, path="answer", sentences=2)

    async def run_barge(self, ws, turn_id: str) -> None:
        await self.send(ws, "transcript.user", turn_id, text="tell me a long story", handover=False)
        await self.send(ws, "state.thinking", turn_id)
        await self.send(ws, "state.speaking", turn_id)
        # Chunks keep flowing while the client decides to barge. The LATE ones
        # after the barge frame are the point of this script.
        sent_after_barge = 0
        for chunk_no in range(12):
            final = chunk_no == 11
            await self.chunk(ws, turn_id, 1, chunk_no, final=final)
            if self.barged.is_set():
                sent_after_barge += 1
                if sent_after_barge >= 3:
                    break
            await asyncio.sleep(0.03)
        if not self.barged.is_set():
            # No barge arrived: say so loudly rather than passing quietly.
            await self.send(ws, "agent.error", turn_id, reason="fixture_no_barge_received", detail="")
            await self.send(ws, "agent.done", turn_id, path="error", sentences=0, reason="fixture_no_barge_received")
            return
        new_turn = turn_id + "-post-barge"
        await self.send(ws, "state.listening", new_turn, barged_turn=turn_id, dropped=2)
        await self.send(ws, "agent.done", turn_id, path="interrupted", sentences=0)

    async def run_noaudio(self, ws, turn_id: str) -> None:
        await self.send(ws, "transcript.user", turn_id, text="speak up", handover=False)
        await self.send(ws, "state.thinking", turn_id)
        await self.send(ws, "agent.error", turn_id, reason="tts_unavailable", detail="fixture")
        await self.send(ws, "agent.done", turn_id, path="error", sentences=0, reason="tts_unavailable")

    # ---- connection handler ----------------------------------------------
    async def handler(self, ws) -> None:
        turn_id = "fake-t1"
        await self.send(ws, "state.idle", turn_id)
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                self.record({"type": "__undecodable__", "raw": str(raw)[:200]})
                continue
            if not isinstance(frame, dict):
                self.record({"type": "__not_an_object__"})
                continue
            self.record(frame)
            ftype = frame.get("type")
            tid = frame.get("turn_id") or turn_id
            if ftype == "user.start":
                turn_id = tid
                await self.send(ws, "state.listening", tid)
            elif ftype == "user.handover":
                turn_id = tid
                await self.send(ws, "handover.received", tid, source=frame.get("source", "unknown"))
                await self.send(ws, "state.listening", tid)
            elif ftype == "user.stop":
                turn_id = tid
                if self.args.script == "normal":
                    asyncio.create_task(self.run_answer(ws, tid))
                elif self.args.script == "barge":
                    asyncio.create_task(self.run_barge(ws, tid))
                else:
                    asyncio.create_task(self.run_noaudio(ws, tid))
            elif ftype == "barge":
                self.barged.set()


async def amain(args) -> int:
    if serve is None:
        print(json.dumps({"ok": False, "reason": "websockets_not_installed"}))
        return 2

    studio = FakeStudio(args)

    def process_request(connection, request):
        path = request.path or "/"
        if path.startswith("/health"):
            status = args.health_status
            body = json.dumps(
                {"ok": status == 200, "service": "fake-studio", "version": "0", "providers": {}, "degraded": []}
            )
            return connection.respond(status, body + "\n")
        if not path.startswith("/ws"):
            return connection.respond(404, "not found\n")
        if args.require_token:
            supplied = request.headers.get(STUDIO_TOKEN_HEADER)
            if not supplied:
                q = path.split("?", 1)[1] if "?" in path else ""
                for pair in q.split("&"):
                    if pair.startswith(WS_TOKEN_QUERY + "="):
                        supplied = pair.split("=", 1)[1]
                        break
            if supplied != args.require_token:
                studio.record({"type": "__unauthorized__", "turn_id": "-"})
                return connection.respond(403, "unauthorized\n")
        return None

    async with serve(
        studio.handler,
        "127.0.0.1",
        args.port,
        process_request=process_request,
        max_size=8 * 1024 * 1024,
    ) as server:
        port = list(server.sockets)[0].getsockname()[1]
        print(json.dumps({"ok": True, "port": port, "ws_url": f"ws://127.0.0.1:{port}/ws"}), flush=True)
        stop = asyncio.get_running_loop().create_future()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
            except (NotImplementedError, RuntimeError):
                pass
        await stop
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fake_ws_server")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port and print it")
    p.add_argument("--log", default="", help="JSONL path for every frame RECEIVED")
    p.add_argument("--script", default="normal", choices=["normal", "barge", "noaudio"])
    p.add_argument("--health-status", type=int, default=200)
    p.add_argument("--require-token", default="")
    p.add_argument(
        "--empty-terminator",
        action="store_true",
        help="send the final chunk with audio_b64='' (server/speech.py's synthetic terminator)",
    )
    args = p.parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
