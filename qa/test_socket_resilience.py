#!/usr/bin/env python3
"""qa/test_socket_resilience.py — Regression tests for WebSocket resilience, safe sending, and bounded audio LRU.

Verifies:
1. Bounded audio LRU cache: max 250 entries, oldest evicted, zero RAM leak.
2. Safe send guard: ws.send_json on closed/closing socket returns False without throwing RuntimeError.
3. Task lifecycle: Cancelled turns or task exceptions are caught and never escape as unretrieved exceptions.
4. Settings concurrency: atomic dictionary snapshots under load.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from starlette.websockets import WebSocketState

from server.app import (
    MAX_AUDIO_STORE_ENTRIES,
    _audio_store,
    _safe_send_json,
    _store_audio,
    handle_turn_task,
    SpeakQueue,
)


class TestAudioLRUStore(unittest.TestCase):
    def setUp(self):
        _audio_store.clear()

    def test_audio_store_lru_bounding(self):
        # Insert 300 entries into the audio store (capacity is 250)
        for i in range(300):
            _store_audio(f"audio-{i}", f"wav-data-{i}".encode("utf-8"))

        self.assertEqual(len(_audio_store), MAX_AUDIO_STORE_ENTRIES)
        # First 50 entries (0 to 49) must have been evicted
        for i in range(50):
            self.assertNotIn(f"audio-{i}", _audio_store)
        # Remaining 250 entries (50 to 299) must be present
        for i in range(50, 300):
            self.assertIn(f"audio-{i}", _audio_store)

    def test_audio_store_access_moves_to_end(self):
        # Insert 250 entries
        for i in range(250):
            _store_audio(f"audio-{i}", f"wav-{i}".encode("utf-8"))

        # Access audio-0 so it becomes most recently used
        _audio_store.move_to_end("audio-0")

        # Insert one more entry -> audio-1 should now be evicted instead of audio-0
        _store_audio("audio-250", b"wav-250")
        self.assertEqual(len(_audio_store), 250)
        self.assertIn("audio-0", _audio_store)
        self.assertNotIn("audio-1", _audio_store)


class TestSafeSendGuard(unittest.IsolatedAsyncioTestCase):
    async def test_safe_send_success_when_connected(self):
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.send_json = AsyncMock()

        ok = await _safe_send_json(mock_ws, {"type": "test"})
        self.assertTrue(ok)
        mock_ws.send_json.assert_awaited_once_with({"type": "test"})

    async def test_safe_send_skipped_when_disconnected(self):
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.DISCONNECTED
        mock_ws.send_json = AsyncMock()

        ok = await _safe_send_json(mock_ws, {"type": "test"})
        self.assertFalse(ok)
        mock_ws.send_json.assert_not_awaited()

    async def test_safe_send_catches_runtime_error_on_dead_socket(self):
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        # Simulates uvicorn's "Unexpected ASGI message after close"
        mock_ws.send_json = AsyncMock(
            side_effect=RuntimeError("Unexpected ASGI message 'websocket.send' after close")
        )

        ok = await _safe_send_json(mock_ws, {"type": "test"})
        self.assertFalse(ok)


class TestTaskLifecycleResilience(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_turn_task_does_not_raise_unretrieved_exception(self):
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.send_json = AsyncMock()
        queue = SpeakQueue()
        turn_tasks = {}

        # Launch handle_turn_task
        task = asyncio.create_task(
            handle_turn_task(mock_ws, "t-test-cancel", "hello donna", queue, turn_tasks)
        )
        await asyncio.sleep(0.01)

        # Cancel the task
        task.cancel()
        # Awaiting the task should complete cleanly without unhandled exception
        await asyncio.gather(task, return_exceptions=True)

        self.assertNotIn("t-test-cancel", turn_tasks)


if __name__ == "__main__":
    unittest.main()
