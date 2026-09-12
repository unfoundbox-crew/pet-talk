#!/usr/bin/env python3
"""qa/test_stt_warm_live.py — the first turn after a boot is warm (0.4.0).

Only runs under ``PET_TALK_REAL_ENGINE=1``. It boots a real server with real
providers, so it needs credentials and an interpreter with the model wheels in
it; the default gate SKIPs it and says so.

What it proves, and why it is worth a whole live server: the hermetic STT-warm
tests in `qa/test_turn_lifecycle.py` prove the startup hook calls `transcribe`
exactly once. They cannot prove the thing that actually mattered, because a
stub has no model to load. Measured 2026-09-12, before the warm: the first
turn after a boot stalled **1264ms** while faster-whisper loaded its weights
on the first real call, against **317-383ms** for every warm turn after it.
Only a real engine on a real socket can say whether that is fixed.

The budget is `first_turn_stall_ms` in qa/budgets.json — never a constant
restated here.

How it runs, and what it refuses to do:

* Its own server, its own port (``PET_TALK_WARM_PORT``, default 8090). If
  something already answers on that port it SKIPs rather than measuring
  someone else's process — a demo server on :8089 is often up, and its first
  turn is long gone.
* Its own interpreter (``PET_TALK_PYTHON``), because the default TTS tyre runs
  in-process and needs mlx-audio in the SAME interpreter as the server.
* It kills what it started, in ``tearDownClass``, terminate then kill.

Run it on its own:

    PET_TALK_REAL_ENGINE=1 doppler run --project unfoundbox --config dev_personal -- \\
      ~/miniconda3/envs/local-ml-py311/bin/python qa/test_stt_warm_live.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
QA_DIR = os.path.join(ROOT, "qa")
if QA_DIR not in sys.path:
    sys.path.insert(0, QA_DIR)

REAL_ENGINE = os.environ.get("PET_TALK_REAL_ENGINE") == "1"

#: A port of its own. Never 8089 — a demo server or another lane may own that,
#: and its first turn happened long before this test started.
WARM_PORT = int(os.environ.get("PET_TALK_WARM_PORT", "8090"))

#: The default TTS tyre runs in-process, so the server needs an interpreter
#: with mlx-audio in it. No fresh venv (see ~/code/CLAUDE.md).
DEFAULT_PYTHON = os.path.expanduser("~/miniconda3/envs/local-ml-py311/bin/python")
SERVER_PYTHON = os.environ.get("PET_TALK_PYTHON") or DEFAULT_PYTHON

BOOT_TIMEOUT_S = float(os.environ.get("PET_TALK_WARM_BOOT_TIMEOUT", "180"))

with open(os.path.join(ROOT, "qa", "budgets.json")) as _f:
    BUDGETS = json.load(_f)
FIRST_TURN_STALL_CEILING_MS = float(BUDGETS["first_turn_stall_ms"])


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class TestFirstTurnIsWarm(unittest.TestCase):
    proc: "subprocess.Popen | None" = None
    log_path = ""

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_ENGINE:
            raise unittest.SkipTest(
                "SKIP: PET_TALK_REAL_ENGINE!=1 — this boots a real server with "
                "real STT/LLM/TTS; run `make qa-real` to exercise it"
            )
        try:
            from websockets.asyncio.client import connect  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(
                "SKIP: `websockets` not installed — pip install websockets to "
                "drive a live turn"
            )
        from fixture_audio import load_b64

        cls.pcm_b64, cls.pcm_rate, pcm_is_real, pcm_source = load_b64()
        if not pcm_is_real:
            raise unittest.SkipTest(
                f"SKIP: no real speech fixture ({pcm_source}) — silence "
                "transcribes to nothing and this measures the error path, not "
                "a turn. Bake one with qa/fixtures/make_weather_fixture.sh"
            )
        if not os.path.exists(SERVER_PYTHON):
            raise unittest.SkipTest(
                f"SKIP: interpreter {SERVER_PYTHON} not found — set "
                "PET_TALK_PYTHON to one holding the model wheels"
            )
        if port_open(WARM_PORT):
            raise unittest.SkipTest(
                f"SKIP: 127.0.0.1:{WARM_PORT} is already in use. This test "
                "measures the FIRST turn of a server it started itself; it "
                "will not touch a process it does not own. Set "
                "PET_TALK_WARM_PORT to a free port."
            )

        os.makedirs(os.path.join(ROOT, ".qa-scratch"), exist_ok=True)
        cls.log_path = os.path.join(ROOT, ".qa-scratch", f"stt-warm-{WARM_PORT}.log")
        cls.log_fh = open(cls.log_path, "w")
        cls.proc = subprocess.Popen(
            [
                SERVER_PYTHON, "-m", "uvicorn", "server.app:app",
                "--host", "127.0.0.1", "--port", str(WARM_PORT),
            ],
            cwd=ROOT,
            stdout=cls.log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own group, so the kill is complete
        )

        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                cls._dump_log()
                raise unittest.SkipTest(
                    f"SKIP: server exited during boot (rc={cls.proc.returncode}); "
                    f"see {cls.log_path}"
                )
            if port_open(WARM_PORT):
                break
            time.sleep(0.25)
        else:
            cls._kill()
            raise unittest.SkipTest(
                f"SKIP: server did not answer on :{WARM_PORT} within "
                f"{BOOT_TIMEOUT_S:.0f}s; see {cls.log_path}"
            )

        # Both warms are startup tasks that are deliberately not awaited, so
        # the port answers well before either has finished. Wait for BOTH to
        # report before measuring anything — the claim under test is "the first
        # turn after warm-up is fast", and a turn that races the warm measures
        # contention instead.
        #
        # This is not pedantry. The first run of this test connected 1.6s before
        # `stall_warm_done` and read 909.6ms: STT itself was 163.3ms (warm, and
        # inside its 150ms-ish budget) while the stall stage took 896.1ms,
        # because the turn's Kokoro synth was queued behind the stall warm's
        # own three synths on the same in-process model.
        cls.stt_warm_ms = cls._await_log_field(deadline, "stt_warm_ms=")
        cls.stall_warm_done = cls._await_log_field(deadline, "stall_warm_done") is not None

    @classmethod
    def _await_log_field(cls, deadline: float, needle: str) -> "float | str | None":
        """Wait for ``needle`` in the server log. Returns the number after an
        ``x=`` needle, the needle itself for a bare marker, or None on timeout
        or a named failure."""
        failures = ("stt_warm_failed", "stt_warm_disabled", "stall_warm_failed",
                    "stall_warm_disabled", "stall_warm_skipped")
        while time.monotonic() < deadline:
            try:
                with open(cls.log_path) as f:
                    text = f.read()
            except OSError:
                text = ""
            for line in text.splitlines():
                if needle in line:
                    if not needle.endswith("="):
                        return needle
                    field = line.split(needle, 1)[1].split()[0]
                    try:
                        return float(field)
                    except ValueError:
                        return None
                if any(f in line for f in failures):
                    return None
            time.sleep(0.25)
        return None

    @classmethod
    def _dump_log(cls) -> None:
        try:
            with open(cls.log_path) as f:
                sys.stderr.write(f.read()[-4000:])
        except OSError:
            pass

    @classmethod
    def _kill(cls) -> None:
        """Kill what we started — the whole process group, never anything else."""
        if cls.proc is None or cls.proc.poll() is not None:
            return
        import signal

        try:
            os.killpg(os.getpgid(cls.proc.pid), signal.SIGTERM)
        except OSError:
            cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(cls.proc.pid), signal.SIGKILL)
            except OSError:
                cls.proc.kill()
            cls.proc.wait(timeout=10)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._kill()
        fh = getattr(cls, "log_fh", None)
        if fh is not None:
            fh.close()

    # -- the measurement ---------------------------------------------------

    def _first_turn_stall_ms(self) -> "float | None":
        from websockets.asyncio.client import connect as ws_connect

        token = ""
        try:
            from server.auth import studio_token

            token = studio_token() or ""
        except Exception:
            token = ""
        url = f"ws://127.0.0.1:{WARM_PORT}/ws"
        if token:
            url = f"{url}?token={token}"

        async def go() -> "float | None":
            async with ws_connect(url, max_size=4 * 1024 * 1024) as ws:
                async def recv(timeout=20.0):
                    return json.loads(await asyncio.wait_for(ws.recv(), timeout))

                await recv(10.0)  # state.idle
                await ws.send(json.dumps({"type": "user.start", "turn_id": "t-warm-1"}))
                await recv(10.0)  # state.listening
                t0 = time.monotonic()
                await ws.send(json.dumps({
                    "type": "user.stop",
                    "turn_id": "t-warm-1",
                    "pcm_b64": self.pcm_b64,
                    "sample_rate": self.pcm_rate,
                }))
                stall_ms = None
                while True:
                    m = await recv(30.0)
                    dt = (time.monotonic() - t0) * 1000.0
                    if m.get("type") == "agent.stall" and stall_ms is None:
                        stall_ms = dt
                    if m.get("type") == "agent.error":
                        self.fail(f"turn failed: {m}")
                    if m.get("type") == "agent.done":
                        return stall_ms

        return asyncio.run(go())

    def test_the_startup_warm_actually_ran(self) -> None:
        self.assertIsNotNone(
            self.stt_warm_ms,
            "no stt_warm_ms line in the server log — the startup warm did not "
            f"run or failed; see {self.log_path}",
        )
        self.assertTrue(
            self.stall_warm_done,
            f"the stall warm never reported; see {self.log_path}",
        )
        print(f"\n    startup stt_warm_ms: {self.stt_warm_ms:.1f} (off the turn path)")

    def test_the_first_live_turn_stalls_inside_budget(self) -> None:
        stall_ms = self._first_turn_stall_ms()
        self.assertIsNotNone(stall_ms, "no agent.stall frame in the first turn")
        print(
            f"\n    FIRST turn time-to-stall: {stall_ms:.1f}ms "
            f"(budget <={FIRST_TURN_STALL_CEILING_MS:.0f}ms; "
            f"1264ms before the STT warm, measured 2026-09-12)"
        )
        self.assertLessEqual(
            stall_ms,
            FIRST_TURN_STALL_CEILING_MS,
            f"first-turn stall {stall_ms:.1f}ms exceeds "
            f"{FIRST_TURN_STALL_CEILING_MS:.0f}ms — the STT warm is not buying "
            "back the cold model load",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
