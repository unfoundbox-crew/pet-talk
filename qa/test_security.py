#!/usr/bin/env python3
"""qa/test_security.py — the studio token and the egress allowlist.

The exfiltration this suite reproduces and then proves refused:

    1. A key is stored (any provider credential).
    2. An unauthenticated ``POST /settings`` points ``llm_base_url`` at an
       attacker host while leaving the key alone (``"***"`` means "keep").
    3. A turn is triggered; the provider dials the attacker host with
       ``Authorization: Bearer <the user's key>``.

Step 2 is now 401 without ``X-Studio-Token`` and 400
``base_url_not_allowed`` with it. Reads stay open. The WS handshake needs the
token too, because a turn is what sends the key.

Run: ``python3 qa/test_security.py -v``
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("STT_PROVIDER", "stub")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("TTS_PROVIDER", "stub")
os.environ.setdefault("PET_TALK_SILENT", "1")

from fastapi.testclient import TestClient

from server import auth
from server.app import app
from server.auth import STUDIO_TOKEN_HEADER, base_url_allowed, studio_token

ATTACKER_BASE_URL = "https://exfil.attacker.example/v1"


def authed() -> TestClient:
    return TestClient(app, headers={STUDIO_TOKEN_HEADER: studio_token()})


def anonymous() -> TestClient:
    return TestClient(app)


class TestSettingsExfiltrationIsRefused(unittest.TestCase):
    """The verified finding, reproduced end to end."""

    def setUp(self):
        self.client = authed()
        self.anon = anonymous()
        self.client.post("/settings", json={"groq_api_key": "gsk-not-a-real-key"})

    def tearDown(self):
        self.client.post(
            "/settings",
            json={
                "groq_api_key": "",
                "llm_provider": "stub",
                "stt_provider": "stub",
                "tts_provider": "stub",
            },
        )

    def test_unauthenticated_settings_post_is_401(self):
        r = self.anon.post(
            "/settings",
            json={"llm_provider": "groq", "llm_base_url": ATTACKER_BASE_URL},
        )
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json().get("reason"), "unauthorized")
        # And the attacker endpoint never landed.
        current = self.client.get("/settings").json()["settings"]
        self.assertNotEqual(current["llm_base_url"], ATTACKER_BASE_URL)

    def test_authenticated_attacker_base_url_is_refused_by_allowlist(self):
        r = self.client.post(
            "/settings",
            json={"llm_provider": "groq", "llm_base_url": ATTACKER_BASE_URL},
        )
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("reason"), "base_url_not_allowed")
        self.assertEqual(body.get("field"), "llm_base_url")
        current = self.client.get("/settings").json()["settings"]
        self.assertNotEqual(current["llm_base_url"], ATTACKER_BASE_URL)

    def test_every_mutating_route_needs_the_token(self):
        cases = [
            ("post", "/settings", {"json": {"stt_provider": "stub"}}),
            ("post", "/personas", {"json": {"name": "intruder"}}),
            ("delete", "/personas/intruder", {}),
            ("delete", "/ledger", {}),
            ("post", "/transcribe", {"json": {"pcm_b64": "AAAA"}}),
        ]
        for method, path, kwargs in cases:
            with self.subTest(route=f"{method.upper()} {path}"):
                r = getattr(self.anon, method)(path, **kwargs)
                self.assertEqual(r.status_code, 401, f"{path}: {r.text}")
                self.assertEqual(r.json().get("reason"), "unauthorized")

    def test_reads_stay_open(self):
        for path in ("/health", "/voices", "/audio/does-not-exist"):
            with self.subTest(path=path):
                r = self.anon.get(path)
                self.assertNotEqual(r.status_code, 401, f"{path} must stay open")

    def test_ws_handshake_without_token_is_refused(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.anon.websocket_connect("/ws") as ws:
                ws.receive_json()

    def test_ws_handshake_with_token_is_accepted(self):
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json().get("type"), "state.idle")

    def test_ws_handshake_accepts_query_token_for_browsers(self):
        with self.anon.websocket_connect(f"/ws?token={studio_token()}") as ws:
            self.assertEqual(ws.receive_json().get("type"), "state.idle")

    def test_a_loopback_base_url_still_works(self):
        r = self.client.post(
            "/settings",
            json={
                "llm_provider": "litellm",
                "llm_base_url": "http://127.0.0.1:4000/v1",
                "llm_api_key": "test-key-not-real",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)


class TestBaseUrlAllowlist(unittest.TestCase):
    def test_loopback_and_rfc1918_allowed(self):
        for url in (
            "http://127.0.0.1:8088",
            "http://localhost:5173",
            "http://[::1]:8000/v1",
            "http://10.1.2.3:4000/v1",
            "http://172.16.4.5/v1",
            "http://192.168.1.9:8088",
        ):
            self.assertTrue(base_url_allowed(url), url)

    def test_known_provider_hosts_allowed(self):
        for url in (
            "https://api.groq.com/openai/v1",
            "https://api.openai.com/v1",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://api.smallest.ai/waves/v1/tts",
        ):
            self.assertTrue(base_url_allowed(url), url)

    def test_public_and_tailnet_hosts_refused(self):
        for url in (
            ATTACKER_BASE_URL,
            "http://evil.example.com",
            "https://api.groq.com.attacker.example/v1",
            "http://100.64.1.2:4000/v1",  # tailnet: allowlist it explicitly
            "file:///etc/passwd",
            "ftp://127.0.0.1/x",
            "not a url",
        ):
            self.assertFalse(base_url_allowed(url), url)

    def test_env_allowlist_opens_one_host(self):
        os.environ["PET_TALK_ALLOWED_HOSTS"] = "lenovo.local, 100.64.1.2"
        try:
            self.assertTrue(base_url_allowed("http://lenovo.local:4000/v1"))
            self.assertTrue(base_url_allowed("http://100.64.1.2:4000/v1"))
            self.assertFalse(base_url_allowed("http://other.local:4000/v1"))
        finally:
            os.environ.pop("PET_TALK_ALLOWED_HOSTS", None)

    def test_empty_means_unset_not_blocked(self):
        self.assertTrue(base_url_allowed(""))

    def test_allowlist_matches_the_hosts_the_code_actually_dials(self):
        """Drift guard: every https host hardcoded in the tyres is allowlisted."""
        sources = ["server/settings.py"] + [
            os.path.join("server", "providers", f)
            for f in sorted(os.listdir(os.path.join(ROOT, "server", "providers")))
            if f.endswith(".py")
        ]
        found: set[str] = set()
        for rel in sources:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                for match in re.findall(r"https://([A-Za-z0-9.\-]+)", f.read()):
                    found.add(match.lower().rstrip("."))
        # Documentation links are not endpoints; only keep api-ish hosts.
        endpoints = {h for h in found if h.startswith("api.") or h.endswith("googleapis.com")}
        missing = endpoints - set(auth.PROVIDER_HOSTS)
        self.assertEqual(
            missing,
            set(),
            f"provider hosts in the code are missing from auth.PROVIDER_HOSTS: {missing}",
        )


class TestTokenResolution(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("STUDIO_TOKEN", None)
        auth.reset_token_cache()

    def test_env_token_wins(self):
        os.environ["STUDIO_TOKEN"] = "env-token-value"
        auth.reset_token_cache()
        self.assertEqual(auth.studio_token(), "env-token-value")
        self.assertTrue(auth.token_matches("env-token-value"))
        self.assertFalse(auth.token_matches("wrong"))
        self.assertFalse(auth.token_matches(None))

    def test_generated_token_file_is_0600_and_gitignored(self):
        auth.reset_token_cache()
        token = auth.studio_token()
        self.assertTrue(token)
        if os.environ.get("STUDIO_TOKEN") or os.environ.get("STUDIO_TOKEN_FILE"):
            self.skipTest("SKIP: token comes from env in this environment")
        self.assertTrue(os.path.isfile(auth.TOKEN_PATH), auth.TOKEN_PATH)
        self.assertEqual(os.stat(auth.TOKEN_PATH).st_mode & 0o777, 0o600)
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(".qa-scratch/", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
