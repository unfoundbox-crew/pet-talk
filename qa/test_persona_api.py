#!/usr/bin/env python3
"""qa/test_persona_api.py — unit tests for persona customizability, CRUD, and Hippocampus endpoints.

TDD contract:
  - list_personas returns all built-ins (donna, jarvis, zuck)
  - save_persona creates custom persona file with frontmatter + tone body
  - load_persona loads custom persona back faithfully
  - delete_persona prevents deletion of built-in personas, deletes custom personas cleanly
  - FastAPI endpoints /personas and /ledger work with standard HTTP methods
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.persona import (
    DEFAULT_PERSONA,
    Persona,
    delete_persona,
    list_personas,
    load_persona,
    sanitize_persona_name,
    save_persona,
)


class TestPersonaCustomization(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pet_talk_personas_test_")
        # Populate with a dummy built-in persona
        self.builtin_name = "donna"
        donna_path = os.path.join(self.temp_dir, "donna.md")
        with open(donna_path, "w", encoding="utf-8") as f:
            f.write("---\nvoice: af_heart\nspeed: 1.05\nstalls:\n  - Looking into that.\ntone: Donna tone\n---\nYou are Donna.\n")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_list_personas(self):
        personas = list_personas(personas_dir=self.temp_dir)
        names = [p.name for p in personas]
        self.assertIn("donna", names)
        self.assertEqual(personas[0].voice, "af_heart")
        self.assertEqual(personas[0].speed, 1.05)

    def test_save_and_load_custom_persona(self):
        custom = Persona(
            name="researcher",
            voice="shannon",
            speed=1.1,
            stalls=["Searching archive.", "Checking citations."],
            tone="Precise, academic, evidence-backed.",
        )
        saved_path = save_persona(custom, personas_dir=self.temp_dir)
        self.assertTrue(os.path.isfile(saved_path))

        loaded = load_persona("researcher", personas_dir=self.temp_dir)
        self.assertEqual(loaded.name, "researcher")
        self.assertEqual(loaded.voice, "shannon")
        self.assertEqual(loaded.speed, 1.1)
        self.assertEqual(loaded.stalls, ["Searching archive.", "Checking citations."])
        self.assertIn("Precise, academic, evidence-backed.", loaded.tone)

    def test_delete_persona(self):
        # Cannot delete built-in
        with self.assertRaises(ValueError):
            delete_persona("donna", personas_dir=self.temp_dir)

        # Create custom persona
        custom = Persona(name="temp_bot", voice="af_heart", speed=1.0, stalls=["One sec."], tone="Temp.")
        save_persona(custom, personas_dir=self.temp_dir)
        self.assertTrue(os.path.isfile(os.path.join(self.temp_dir, "temp_bot.md")))

        # Now delete custom persona
        deleted = delete_persona("temp_bot", personas_dir=self.temp_dir)
        self.assertTrue(deleted)
        self.assertFalse(os.path.isfile(os.path.join(self.temp_dir, "temp_bot.md")))


class TestPersonaNameSanitizer(unittest.TestCase):
    """delete_persona used to lowercase-and-strip only: a name could carry a
    path. Both writes and deletes now go through one sanitizer."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pet_talk_persona_traversal_")
        self.outside = os.path.join(self.temp_dir, "outside.md")
        with open(self.outside, "w", encoding="utf-8") as f:
            f.write("---\nvoice: af_heart\nspeed: 1.0\nstalls:\n  - x\n---\nbody\n")
        self.personas_dir = os.path.join(self.temp_dir, "personas")
        os.makedirs(self.personas_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sanitizer_strips_path_characters(self):
        self.assertEqual(sanitize_persona_name("../../Outside"), "outside")
        self.assertEqual(sanitize_persona_name("a/b/c"), "abc")
        with self.assertRaises(ValueError):
            sanitize_persona_name("../..")
        with self.assertRaises(ValueError):
            sanitize_persona_name("")

    def test_delete_cannot_reach_outside_the_personas_dir(self):
        self.assertFalse(
            delete_persona("../outside", personas_dir=self.personas_dir),
            "traversal name resolved to a file",
        )
        self.assertTrue(
            os.path.isfile(self.outside), "a file outside personas/ was deleted"
        )


class TestFastApiEndpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app import app
        from server.auth import STUDIO_TOKEN_HEADER, studio_token

        self.client = TestClient(app, headers={STUDIO_TOKEN_HEADER: studio_token()})
        self.anon = TestClient(app)

    def test_persona_write_routes_need_the_studio_token(self):
        self.assertEqual(self.anon.post("/personas", json={"name": "x"}).status_code, 401)
        self.assertEqual(self.anon.delete("/personas/x").status_code, 401)

    def test_get_personas_endpoint(self):
        r = self.client.get("/personas")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)
        names = [p["name"] for p in data]
        self.assertIn("donna", names)
        self.assertIn("jarvis", names)

    def test_get_single_persona(self):
        r = self.client.get("/personas/donna")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["name"], "donna")
        self.assertIn("voice", data)

    def test_get_ledger(self):
        r = self.client.get("/ledger")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertIsInstance(data.get("turns"), list)


if __name__ == "__main__":
    unittest.main()

