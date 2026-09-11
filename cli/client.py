"""Pet-Talk Terminal Client — Interactive TUI & One-Shot Hotkey Client.

Connects to the Pet-Talk WebSocket daemon (default ws://127.0.0.1:8089/ws).
Supports:
1. Interactive TUI Mode: Full terminal loop with Push/Toggle-to-talk and instant barge-in.
2. One-Shot Hotkey Mode: Run from Raycast/Shortcuts/Alfred/skhd with Energy VAD & afplay.
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import sys
import termios
import time
import tty
import uuid
from typing import Optional
from urllib.parse import urlparse

from .audio import AudioPlayer, AudioRecorder, EnergyVAD, calculate_rms_and_peak, pcm16_to_base64

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    # websockets may also use older path in some environments
    try:
        from websockets import connect as ws_connect  # type: ignore
        from websockets.exceptions import ConnectionClosed  # type: ignore
    except ImportError:
        ws_connect = None  # type: ignore
        ConnectionClosed = Exception  # type: ignore


class TerminalInput:
    """Non-blocking single-key terminal input handler (macOS / Linux)."""

    def __init__(self) -> None:
        self._old_settings = None
        self._is_tty = sys.stdin.isatty()

    def enable(self) -> None:
        if self._is_tty:
            try:
                self._old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self._old_settings = None

    def restore(self) -> None:
        if self._old_settings is not None and self._is_tty:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None

    def read_key(self) -> Optional[str]:
        if not self._is_tty:
            return None
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return sys.stdin.read(1)
        except (ValueError, IOError):
            pass
        return None


class PetTalkClient:
    """High-performance terminal client for Pet-Talk full-duplex voice loop."""

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:8089/ws",
        persona: str = "donna",
        vad_silence_ms: int = 800,
        vad_threshold: float = 350.0,
        quiet: bool = False,
    ) -> None:
        self.ws_url = ws_url
        self.persona = persona
        self.vad_silence_ms = vad_silence_ms
        self.vad_threshold = vad_threshold
        self.quiet = quiet

        parsed = urlparse(ws_url)
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        self.http_base = f"{http_scheme}://{parsed.netloc}"

        self.recorder = AudioRecorder(chunk_bytes=1024)
        self.player = AudioPlayer(
            base_url=self.http_base,
            on_sentence_start=self._on_speech_sentence,
        )
        self.vad = EnergyVAD(
            threshold=vad_threshold,
            silence_ms=vad_silence_ms,
            sample_rate=16000,
            chunk_samples=512,
        )
        self.input_handler = TerminalInput()

        self.state: str = "idle"  # idle | listening | thinking | speaking
        self.current_turn_id: Optional[str] = None
        self._running: bool = False
        self._ws = None
        self._turn_counter: int = 1

    def _log(self, text: str) -> None:
        if not self.quiet:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _on_speech_sentence(self, turn_id: str, seq: int, text: str) -> None:
        if not self.quiet and text:
            persona_name = self.persona.capitalize()
            self._log(f"-> [SPEAKING] {persona_name}: \"{text}\"")

    def new_turn_id(self) -> str:
        tid = f"cli-t{self._turn_counter}-{uuid.uuid4().hex[:6]}"
        self._turn_counter += 1
        return tid

    async def connect(self) -> None:
        """Establish WebSocket connection to daemon."""
        if ws_connect is None:
            raise RuntimeError("websockets library is required: pip install websockets")
        self._ws = await ws_connect(self.ws_url, max_size=8 * 1024 * 1024)
        self.player.start()

    async def close(self) -> None:
        """Gracefully disconnect and tear down processes."""
        self._running = False
        self.recorder.stop()
        await self.player.stop()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.input_handler.restore()

    async def send_frame(self, frame_dict: dict) -> None:
        """Send JSON frame over WebSocket."""
        if self._ws:
            await self._ws.send(json.dumps(frame_dict))

    async def recv_frame(self) -> Optional[dict]:
        """Receive and decode single JSON frame."""
        if not self._ws:
            return None
        try:
            msg = await self._ws.recv()
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8")
            return json.loads(msg)
        except (ConnectionClosed, asyncio.CancelledError):
            return None

    async def barge(self, turn_id: Optional[str] = None) -> float:
        """Execute instant barge-in interruption.

        Kills afplay in <= 50ms, flushes playback queue, and notifies server.
        """
        kill_ms = self.player.kill_playback()
        tid = turn_id or self.current_turn_id or self.new_turn_id()
        self.current_turn_id = tid
        await self.send_frame({"type": "barge", "turn_id": tid})
        self._log(f"-> [BARGE] Audio killed in {kill_ms:.1f}ms (queue flushed).")
        self.state = "listening"
        return kill_ms

    async def start_recording_turn(self) -> str:
        """Begin audio capture and notify server of turn start."""
        self.recorder.stop()
        self.vad.reset()
        self.current_turn_id = self.new_turn_id()
        self.state = "listening"

        await self.send_frame({
            "type": "user.start",
            "turn_id": self.current_turn_id,
            "persona": self.persona,
        })
        self.recorder.start()
        self._log(f"-> [LISTENING] Speak now... (Press Space/Enter to finish, 'b' to barge)")
        return self.current_turn_id

    async def stop_recording_turn(self) -> bytes:
        """Stop audio capture and send complete PCM payload to server."""
        pcm_bytes = self.recorder.stop()
        self.state = "thinking"
        self._log(f"-> [THINKING] Transcribing & waiting for {self.persona.capitalize()}...")

        b64_pcm = pcm16_to_base64(pcm_bytes)
        await self.send_frame({
            "type": "user.stop",
            "turn_id": self.current_turn_id,
            "pcm_b64": b64_pcm,
            "sample_rate": 16000,
        })
        return pcm_bytes

    async def run_once(self) -> int:
        """Execute a single hotkey / one-shot turn with energy VAD and exit."""
        self._log(f"+-- pet-talk (one-shot hotkey mode) | Persona: {self.persona.capitalize()}")
        self._log(f"| connecting: {self.ws_url}")

        try:
            await self.connect()
        except Exception as e:
            self._log(f"Error: failed to connect to Pet-Talk server at {self.ws_url}: {e}")
            return 1

        try:
            # Wait for initial state.idle frame
            first = await self.recv_frame()
            if not first or first.get("type") != "state.idle":
                self._log(f"Warning: unexpected initial frame: {first}")

            # Start recording
            await self.start_recording_turn()

            # Stream audio chunks and monitor VAD
            t_start = time.monotonic()
            max_duration_s = 20.0  # safety timeout

            while self.recorder.is_active():
                chunk = self.recorder.read_chunk()
                if chunk:
                    # Stream chunk to server
                    await self.send_frame({
                        "type": "user.chunk",
                        "turn_id": self.current_turn_id,
                        "chunk": pcm16_to_base64(chunk),
                    })

                    # Real-time acoustic energy telemetry for Dynamic Island HUD
                    rms, peak = calculate_rms_and_peak(chunk)
                    norm_rms = min(1.0, max(0.0, rms / 4000.0))
                    norm_peak = min(1.0, max(0.0, peak / 16000.0))
                    self._log(f"[RMS: {norm_rms:.3f}, PEAK: {norm_peak:.3f}]")

                    # VAD analysis
                    _is_speech, turn_done = self.vad.process_chunk(chunk)
                    if turn_done:
                        break

                if (time.monotonic() - t_start) > max_duration_s:
                    break
                await asyncio.sleep(0.01)

            if not self.vad.speech_started:
                self.recorder.stop()
                self._log("-> [VAD] No speech detected (ambient silence).")
                await self.send_frame({"type": "barge", "turn_id": self.current_turn_id})
                return 0

            # Stop recording and send user.stop
            await self.stop_recording_turn()

            # Consume server frames until agent.done
            while True:
                frame = await self.recv_frame()
                if not frame:
                    break

                ftype = frame.get("type")
                if ftype == "transcript.user":
                    user_text = frame.get("text", "")
                    if user_text:
                        persona_name = self.persona.capitalize()
                        self._log(f"-> [HEARD] [{persona_name} heard]: \"{user_text}\"")
                elif ftype == "agent.stall":
                    stall_text = frame.get("text", "")
                    if stall_text and not self.quiet:
                        self._log(f"-> [STALL] {stall_text}")
                elif ftype == "agent.sentence":
                    audio_url = frame.get("audio_url", "")
                    seq = frame.get("seq", 0)
                    text = frame.get("text", "")
                    if audio_url:
                        await self.player.enqueue(
                            audio_url=audio_url,
                            turn_id=self.current_turn_id or "",
                            seq=seq,
                            text=text,
                        )
                elif ftype == "agent.error":
                    self._log(f"-> [ERROR] {frame.get('reason')}")
                    break
                elif ftype == "agent.done":
                    break

            # Wait until Donna finishes speaking audio via afplay
            await self.player.wait_idle()
            self._log("+-- [DONE] Turn completed.")
            return 0

        finally:
            await self.close()

    async def run_interactive(self, enable_vad: bool = False) -> int:
        """Run full-duplex interactive TUI in the terminal pane."""
        self._log("+-- pet-talk v0.2 | Persona: " + self.persona.capitalize())
        self._log("| host: " + self.ws_url)
        self._log("| keys: Space/Enter = talk/stop | 'b' = barge-in | 'q' = quit")
        self._log("+------------------------------------------------------")

        try:
            await self.connect()
        except Exception as e:
            self._log(f"Error: failed to connect to {self.ws_url}: {e}")
            return 1

        self.input_handler.enable()
        self._running = True

        # Frame receiver task
        async def _receiver():
            while self._running:
                try:
                    frame = await self.recv_frame()
                    if not frame:
                        break

                    ftype = frame.get("type")
                    if ftype == "state.idle":
                        if self.state != "listening":
                            self.state = "idle"
                            self._log("-> [IDLE] Donna is ready. Press Space or Enter to talk.")
                    elif ftype == "state.listening":
                        self.state = "listening"
                    elif ftype == "state.thinking":
                        self.state = "thinking"
                    elif ftype == "state.speaking":
                        self.state = "speaking"
                    elif ftype == "transcript.user":
                        user_text = frame.get("text", "")
                        if user_text:
                            persona_name = self.persona.capitalize()
                            self._log(f"-> [HEARD] [{persona_name} heard]: \"{user_text}\"")
                    elif ftype == "agent.stall":
                        stall_text = frame.get("text", "")
                        if stall_text and not self.quiet:
                            self._log(f"-> [STALL] {stall_text}")
                    elif ftype == "agent.sentence":
                        audio_url = frame.get("audio_url", "")
                        seq = frame.get("seq", 0)
                        text = frame.get("text", "")
                        if audio_url:
                            await self.player.enqueue(
                                audio_url=audio_url,
                                turn_id=self.current_turn_id or "",
                                seq=seq,
                                text=text,
                            )
                    elif ftype == "agent.done":
                        # Wait for playback to finish before returning to idle
                        await self.player.wait_idle()
                        self.state = "idle"
                        self._log("-> [IDLE] Turn complete. Ready for next input.")
                    elif ftype == "agent.error":
                        self._log(f"-> [ERROR] {frame.get('reason')}")
                        self.state = "idle"

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self._log(f"Receiver error: {e}")
                    break

        receiver_task = asyncio.create_task(_receiver())

        try:
            while self._running:
                # 1. Check keyboard input
                key = self.input_handler.read_key()
                if key:
                    if key in ("q", "Q", "\x03"):  # q or Ctrl+C
                        self._log("\nExiting pet-talk...")
                        break
                    elif key in (" ", "\r", "\n"):
                        if self.state == "idle":
                            await self.start_recording_turn()
                        elif self.state == "listening":
                            await self.stop_recording_turn()
                        elif self.state in ("thinking", "speaking") or self.player.is_playing():
                            # Barge-in!
                            await self.barge()
                            await self.start_recording_turn()
                    elif key in ("b", "B"):
                        # Explicit barge key
                        if self.state in ("thinking", "speaking") or self.player.is_playing():
                            await self.barge()
                            await self.start_recording_turn()

                # 2. If recording, stream chunk to server
                if self.state == "listening" and self.recorder.is_active():
                    chunk = self.recorder.read_chunk()
                    if chunk:
                        await self.send_frame({
                            "type": "user.chunk",
                            "turn_id": self.current_turn_id,
                            "chunk": pcm16_to_base64(chunk),
                        })
                        if enable_vad:
                            _speech, turn_done = self.vad.process_chunk(chunk)
                            if turn_done:
                                if self.vad.speech_started:
                                    await self.stop_recording_turn()
                                else:
                                    self.recorder.stop()
                                    self._log("-> [VAD] No speech detected (silence cutoff). Returning to idle.")
                                    await self.send_frame({"type": "barge", "turn_id": self.current_turn_id})
                                    self.state = "idle"

                await asyncio.sleep(0.01)

        finally:
            self._running = False
            receiver_task.cancel()
            await self.close()

        return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI argument parser and dispatcher."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="pet-talk-cli",
        description="Low-latency Terminal CLI & Hotkey Client for Pet-Talk.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="interactive",
        choices=["interactive", "once", "tui"],
        help="Execution mode: 'interactive' (default TUI) or 'once' (hotkey one-shot)",
    )
    parser.add_argument(
        "--hotkey",
        "-1",
        dest="hotkey_flag",
        action="store_true",
        help="Run in one-shot hotkey mode (records mic via VAD, plays reply, exits)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("PET_TALK_WS_URL", "ws://127.0.0.1:8089/ws"),
        help="WebSocket daemon URL (default: ws://127.0.0.1:8089/ws)",
    )
    parser.add_argument(
        "--persona",
        default=os.environ.get("PET_TALK_PERSONA", "donna"),
        help="Persona to converse with (default: donna)",
    )
    parser.add_argument(
        "--vad",
        action="store_true",
        help="Enable continuous energy VAD in interactive mode",
    )
    parser.add_argument(
        "--silence",
        type=int,
        default=int(os.environ.get("PET_TALK_VAD_SILENCE_MS", "800")),
        help="VAD silence duration cutoff in ms (default: 800)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("PET_TALK_VAD_THRESHOLD", "350.0")),
        help="VAD speech RMS threshold (default: 350.0)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Quiet mode with minimal console output (ideal for hotkey bindings)",
    )

    args = parser.parse_args(argv)

    client = PetTalkClient(
        ws_url=args.url,
        persona=args.persona,
        vad_silence_ms=args.silence,
        vad_threshold=args.threshold,
        quiet=args.quiet,
    )

    is_once = args.mode == "once" or args.hotkey_flag

    def _sig_handler(sig, frame):
        client.player.kill_playback()
        client.input_handler.restore()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    if is_once:
        return asyncio.run(client.run_once())
    else:
        return asyncio.run(client.run_interactive(enable_vad=args.vad))


if __name__ == "__main__":
    sys.exit(main())
