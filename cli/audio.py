"""Audio capture, VAD, resampling, playback, and barge-in for Pet-Talk CLI.

TECH-SPEC:
- 16kHz linear PCM mono (linear16, signed 16-bit little-endian)
- Barge-in audio kill <= 50ms via native macOS afplay
- Energy VAD with speech confirmation and silence duration cutoff
"""
from __future__ import annotations

import array
import asyncio
import base64
import math
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Callable, Optional


def pcm16_to_base64(pcm_data: bytes) -> str:
    """Encode linear16 PCM bytes to Base64 string."""
    return base64.b64encode(pcm_data).decode("ascii")


def base64_to_pcm16(b64_str: str) -> bytes:
    """Decode Base64 string to linear16 PCM bytes."""
    return base64.b64decode(b64_str)


def calculate_rms(pcm_bytes: bytes) -> float:
    """Calculate Root Mean Square (RMS) energy of 16-bit linear PCM audio.

    Returns:
        float: RMS energy in range [0.0, 32768.0].
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0
    count = len(pcm_bytes) // 2
    shorts = array.array("h")
    shorts.frombytes(pcm_bytes[: count * 2])
    sum_sq = sum(s * s for s in shorts)
    return math.sqrt(sum_sq / count)


def calculate_rms_and_peak(pcm_bytes: bytes) -> tuple[float, float]:
    """Calculate Root Mean Square (RMS) energy and peak amplitude of 16-bit linear PCM audio.

    Returns:
        (rms, peak): raw RMS and peak amplitude in range [0.0, 32768.0].
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0, 0.0
    count = len(pcm_bytes) // 2
    shorts = array.array("h")
    shorts.frombytes(pcm_bytes[: count * 2])
    sum_sq = 0
    max_peak = 0
    for s in shorts:
        abs_s = abs(s)
        if abs_s > max_peak:
            max_peak = abs_s
        sum_sq += s * s
    rms = math.sqrt(sum_sq / count)
    return rms, float(max_peak)


class EnergyVAD:
    """Lightweight Voice Activity Detector based on energy (RMS) thresholds.

    - Detects voice onset by requiring N consecutive speech frames.
    - Measures silence duration after speech has started (default 800ms).
    - Prevents premature cutoff during natural mid-sentence pauses.
    - Prevents capturing unending ambient silence via initial silence timeout (default 4000ms).
    - Declares turn finished when speech silence exceeds ``silence_ms`` or initial silence exceeds ``initial_silence_ms``.
    """

    def __init__(
        self,
        threshold: float = 350.0,
        confirm_frames: int = 2,
        silence_ms: int = 800,
        sample_rate: int = 16000,
        chunk_samples: int = 512,
        initial_silence_ms: int = 4000,
        min_speech_ms: int = 0,
    ) -> None:
        self.threshold = threshold
        self.confirm_frames = confirm_frames
        self.silence_ms = silence_ms
        self.sample_rate = sample_rate
        self.chunk_samples = chunk_samples
        self.chunk_duration_ms = (chunk_samples / sample_rate) * 1000.0
        self.initial_silence_ms = initial_silence_ms
        self.min_speech_ms = min_speech_ms

        self.speech_started: bool = False
        self._consecutive_speech: int = 0
        self._speech_duration_ms: float = 0.0
        self.silence_duration_ms: float = 0.0
        self.initial_silence_duration_ms: float = 0.0

    def reset(self) -> None:
        """Reset VAD state for a fresh turn."""
        self.speech_started = False
        self._consecutive_speech = 0
        self._speech_duration_ms = 0.0
        self.silence_duration_ms = 0.0
        self.initial_silence_duration_ms = 0.0

    def process_chunk(self, chunk: bytes) -> tuple[bool, bool]:
        """Process a PCM chunk.

        Returns:
            (is_speech_chunk, turn_finished)
        """
        rms = calculate_rms(chunk)
        is_speech = rms >= self.threshold

        if is_speech:
            self._consecutive_speech += 1
            self._speech_duration_ms += self.chunk_duration_ms
            if not self.speech_started and self._consecutive_speech >= self.confirm_frames:
                self.speech_started = True
            if self.speech_started:
                self.silence_duration_ms = 0.0
        else:
            self._consecutive_speech = 0
            if self.speech_started:
                # Active speech finished, accumulating trailing silence
                self.silence_duration_ms += self.chunk_duration_ms
                if (
                    self.silence_duration_ms >= self.silence_ms
                    and self._speech_duration_ms >= self.min_speech_ms
                ):
                    return False, True
            else:
                # Speech has not started yet: accumulate initial silence
                self.initial_silence_duration_ms += self.chunk_duration_ms
                if self.initial_silence_duration_ms >= self.initial_silence_ms:
                    return False, True

        return is_speech, False


def find_recorder() -> list[str]:
    """Find the best available 16kHz audio recorder binary on macOS.

    Prefers `sox` or `rec`, then falls back to `ffmpeg`.
    Returns command argument list that outputs raw s16le mono 16kHz to stdout.
    """
    if shutil.which("sox"):
        return [
            "sox",
            "-q",
            "--buffer",
            "1024",
            "-d",
            "-b",
            "16",
            "-e",
            "signed-integer",
            "-c",
            "1",
            "-r",
            "16000",
            "-t",
            "raw",
            "-",
            "rate",
            "16000",
        ]
    if shutil.which("rec"):
        return [
            "rec",
            "-q",
            "--buffer",
            "1024",
            "-b",
            "16",
            "-c",
            "1",
            "-r",
            "16000",
            "-t",
            "raw",
            "-",
            "rate",
            "16000",
        ]
    if shutil.which("ffmpeg"):
        return [
            "ffmpeg",
            "-loglevel",
            "quiet",
            "-f",
            "avfoundation",
            "-i",
            ":default",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ]
    raise RuntimeError("No supported audio recording binary found (install sox or ffmpeg)")


class AudioRecorder:
    """Low-latency audio recorder streaming 16kHz mono linear16 PCM chunks."""

    def __init__(self, chunk_bytes: int = 1024) -> None:
        self.chunk_bytes = chunk_bytes
        self._proc: Optional[subprocess.Popen] = None
        self._accumulated: list[bytes] = []

    def start(self) -> None:
        """Launch recorder subprocess piping raw PCM to stdout."""
        self.stop()
        cmd = find_recorder()
        self._accumulated = []
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def is_active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def read_chunk(self) -> Optional[bytes]:
        """Read a single PCM chunk. Returns None if process terminated."""
        if not self.is_active() or self._proc is None or self._proc.stdout is None:
            return None
        try:
            chunk = self._proc.stdout.read(self.chunk_bytes)
            if chunk:
                self._accumulated.append(chunk)
                return chunk
        except (ValueError, IOError):
            pass
        return None

    def stop(self) -> bytes:
        """Stop recording, terminate subprocess, and return complete PCM buffer."""
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    self._proc.wait(timeout=0.1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            finally:
                if self._proc.stdout:
                    try:
                        rest = self._proc.stdout.read()
                        if rest:
                            self._accumulated.append(rest)
                    except Exception:
                        pass
                self._proc = None

        all_pcm = b"".join(self._accumulated)
        self._accumulated = []
        return all_pcm


class AudioPlayer:
    """Gapless audio playback worker via native macOS `afplay` with <=50ms barge-in."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8089",
        on_sentence_start: Optional[Callable[[str, int, str], None]] = None,
        on_sentence_end: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.on_sentence_start = on_sentence_start
        self.on_sentence_end = on_sentence_end

        self._queue: asyncio.Queue[tuple[str, str, int, str]] = asyncio.Queue()
        self._active_proc: Optional[subprocess.Popen] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._temp_files: list[str] = []
        self._playing_turn_id: Optional[str] = None
        self._playing_seq: Optional[int] = None
        self._barge_in_progress: bool = False

    def start(self) -> None:
        """Start the background playback loop."""
        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._worker_task = asyncio.create_task(self._playback_loop())

    async def stop(self) -> None:
        """Stop the player and kill any active playback."""
        self._running = False
        self.kill_playback()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._cleanup_temp_files()

    def is_playing(self) -> bool:
        """Check if audio is actively playing or queued to play."""
        return (self._active_proc is not None and self._active_proc.poll() is None) or not self._queue.empty()

    async def enqueue(self, audio_url: str, turn_id: str, seq: int, text: str = "") -> None:
        """Fetch audio payload and enqueue it for immediate playback."""
        if self._barge_in_progress:
            return

        full_url = audio_url if audio_url.startswith("http") else f"{self.base_url}{audio_url}"

        # Fetch in thread to keep loop non-blocking
        def _fetch() -> bytes:
            req = urllib.request.Request(full_url, headers={"User-Agent": "pet-talk-cli/0.2"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.read()

        try:
            wav_bytes = await asyncio.to_thread(_fetch)
        except Exception as e:
            return

        if self._barge_in_progress:
            return

        # Ensure payload is a valid non-empty WAV (RIFF/WAVE header, min 44 bytes)
        if len(wav_bytes) < 44 or not (wav_bytes.startswith(b"RIFF") and b"WAVE" in wav_bytes[:16]):
            return

        # Write to temporary WAV file
        fd, path = tempfile.mkstemp(prefix=f"pet_talk_{turn_id}_{seq}_", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes)
            self._temp_files.append(path)
            await self._queue.put((path, turn_id, seq, text))
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass

    def kill_playback(self) -> float:
        """Kill currently playing audio process in <= 50ms and drop queued items.

        Returns:
            float: Elapsed kill time in milliseconds.
        """
        t0 = time.perf_counter()
        self._barge_in_progress = True

        # 1. Drain the playback queue
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                path = item[0]
                if path in self._temp_files:
                    self._temp_files.remove(path)
                try:
                    os.remove(path)
                except OSError:
                    pass
                self._queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break

        # 2. Terminate active afplay process
        proc = self._active_proc
        self._active_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=0.035)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.01)
            except Exception:
                pass

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._playing_turn_id = None
        self._playing_seq = None
        self._barge_in_progress = False
        return elapsed_ms

    async def wait_idle(self) -> None:
        """Wait until all queued audio finishes playing."""
        while not self._queue.empty() or (self._active_proc is not None and self._active_proc.poll() is None):
            await asyncio.sleep(0.02)

    async def _playback_loop(self) -> None:
        """Continuous worker playing audio items sequentially."""
        while self._running:
            try:
                path, turn_id, seq, text = await self._queue.get()
            except asyncio.CancelledError:
                break

            if not os.path.exists(path):
                self._queue.task_done()
                continue

            self._playing_turn_id = turn_id
            self._playing_seq = seq
            if self.on_sentence_start:
                self.on_sentence_start(turn_id, seq, text)

            # Spawn native afplay
            try:
                proc = subprocess.Popen(
                    ["afplay", path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._active_proc = proc

                # Wait for afplay to finish or be killed
                while proc.poll() is None:
                    await asyncio.sleep(0.01)

            except Exception:
                pass
            finally:
                self._active_proc = None
                self._playing_turn_id = None
                self._playing_seq = None
                if path in self._temp_files:
                    self._temp_files.remove(path)
                try:
                    os.remove(path)
                except OSError:
                    pass
                if self.on_sentence_end:
                    self.on_sentence_end(turn_id, seq)
                self._queue.task_done()

    def _cleanup_temp_files(self) -> None:
        for p in list(self._temp_files):
            try:
                os.remove(p)
            except OSError:
                pass
        self._temp_files.clear()
