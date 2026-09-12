#!/usr/bin/env python3
"""qa/test_launchd.py — QA suite for running pet-talk-server as a launchd
user agent (launchd/com.unfoundbox.pet-talk-server.plist.template,
bin/pet-talk-server, bin/agent-ctl.sh, `make install-agent` /
`make uninstall-agent` / `make agent-status`).

Hermetic by construction: every test here uses --dry-run (or make's own
native `-n` mode via `make ... --dry-run`) and never calls `launchctl
bootstrap`, never writes to ~/Library/LaunchAgents, never starts the real
server. Installing the agent for real is Saurabh's call — see AGENTS.md and
the docstring on the plist template.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "launchd", "com.unfoundbox.pet-talk-server.plist.template")
LAUNCHER = os.path.join(ROOT, "bin", "pet-talk-server")
AGENT_CTL = os.path.join(ROOT, "bin", "agent-ctl.sh")

LAUNCH_AGENTS_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.unfoundbox.pet-talk-server.plist"
)


def _skip_unless_darwin():
    if sys.platform != "darwin":
        raise unittest.SkipTest("SKIP: launchd is macOS-only")


def _skip_if_missing(path, label):
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"SKIP: {label} not found at {path} (OPTIONAL — another lane may add it)")


def _plist_mtime_or_none():
    try:
        return os.stat(LAUNCH_AGENTS_PLIST).st_mtime
    except FileNotFoundError:
        return None


class TestPlistTemplate(unittest.TestCase):
    def setUp(self):
        _skip_unless_darwin()
        _skip_if_missing(TEMPLATE, "launchd/com.unfoundbox.pet-talk-server.plist.template")
        _skip_if_missing(AGENT_CTL, "bin/agent-ctl.sh")

    def _rendered_plist(self):
        res = subprocess.run(
            [AGENT_CTL, "install", "--dry-run"],
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        )
        self.assertEqual(res.returncode, 0, f"agent-ctl.sh install --dry-run failed:\n{res.stdout}\n{res.stderr}")
        marker = "--- rendered plist ---"
        self.assertIn(marker, res.stdout, "install --dry-run must print the rendered plist")
        return res.stdout.split(marker, 1)[1]

    def test_dry_run_touches_no_state(self):
        before = _plist_mtime_or_none()
        self._rendered_plist()
        after = _plist_mtime_or_none()
        self.assertEqual(before, after, "install --dry-run must not write ~/Library/LaunchAgents")

    def test_template_has_no_stray_home_paths(self):
        """Every /Users/<name>/... in the rendered plist must be this
        checkout's own ROOT (or a path inside ~/Library, which is the launchd
        agent's declared log/plist location) — never some other user's or
        another machine's baked-in path."""
        rendered = self._rendered_plist()
        home = os.path.expanduser("~")
        for m in re.finditer(r"/Users/[^\s<\"']+", rendered):
            found = m.group(0)
            self.assertTrue(
                found.startswith(ROOT) or found.startswith(home + "/Library"),
                f"unexpected absolute path baked into the rendered plist: {found} "
                f"(expected it to start with the repo root {ROOT} or {home}/Library)",
            )

    def test_rendered_plist_validates(self):
        plutil = shutil.which("plutil")
        if not plutil:
            raise unittest.SkipTest("SKIP: plutil not on PATH")
        rendered = self._rendered_plist()
        # Isolate exactly the <?xml ...?>...</plist> document (the dry-run
        # output has our own log lines above and below it).
        start = rendered.index("<?xml")
        end = rendered.rindex("</plist>") + len("</plist>")
        doc = rendered[start:end]
        tmp_path = os.path.join(ROOT, ".qa-scratch", "rendered-agent-test.plist")
        os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
        with open(tmp_path, "w") as f:
            f.write(doc)
        try:
            res = subprocess.run([plutil, "-lint", tmp_path], capture_output=True, text=True, timeout=15)
            self.assertEqual(res.returncode, 0, f"plutil -lint failed:\n{res.stdout}\n{res.stderr}")
        finally:
            os.remove(tmp_path)

    def test_uninstall_dry_run_touches_no_state(self):
        before = _plist_mtime_or_none()
        res = subprocess.run(
            [AGENT_CTL, "uninstall", "--dry-run"],
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        )
        self.assertEqual(res.returncode, 0, f"agent-ctl.sh uninstall --dry-run failed:\n{res.stdout}\n{res.stderr}")
        self.assertNotIn("removed", res.stdout.lower())
        after = _plist_mtime_or_none()
        self.assertEqual(before, after, "uninstall --dry-run must not touch ~/Library/LaunchAgents")


class TestLauncherScript(unittest.TestCase):
    def setUp(self):
        _skip_if_missing(LAUNCHER, "bin/pet-talk-server")

    def test_bash_syntax(self):
        res = subprocess.run(["bash", "-n", LAUNCHER], capture_output=True, text=True, timeout=15)
        self.assertEqual(res.returncode, 0, f"bash -n failed:\n{res.stderr}")

    def test_help(self):
        res = subprocess.run([LAUNCHER, "--help"], capture_output=True, text=True, timeout=15)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PET_TALK_PYTHON", res.stdout)

    def test_dry_run_prints_resolved_values_and_touches_nothing(self):
        log_dir = os.path.expanduser("~/Library/Application Support/pet-talk")
        log_file = os.path.join(log_dir, "server.log")
        existed_before = os.path.isdir(log_dir)
        mtime_before = os.stat(log_file).st_mtime if os.path.isfile(log_file) else None

        env = dict(os.environ, PET_TALK_NO_DOPPLER="1")
        res = subprocess.run(
            [LAUNCHER, "--dry-run"], capture_output=True, text=True, timeout=15, cwd=ROOT, env=env,
        )
        self.assertEqual(res.returncode, 0, f"--dry-run failed:\n{res.stdout}\n{res.stderr}")
        for needle in ("interpreter:", "bind:", "log file:", "would exec:"):
            self.assertIn(needle, res.stdout, f"--dry-run output missing '{needle}'")
        self.assertIn("127.0.0.1:8089", res.stdout)

        mtime_after = os.stat(log_file).st_mtime if os.path.isfile(log_file) else None
        self.assertEqual(mtime_before, mtime_after, "--dry-run must not write/rotate server.log")
        if not existed_before:
            self.assertFalse(
                os.path.isdir(log_dir) and os.path.isfile(log_file),
                "--dry-run must not create server.log if it didn't already exist",
            )


class TestAgentCtlScript(unittest.TestCase):
    def setUp(self):
        _skip_if_missing(AGENT_CTL, "bin/agent-ctl.sh")

    def test_bash_syntax(self):
        res = subprocess.run(["bash", "-n", AGENT_CTL], capture_output=True, text=True, timeout=15)
        self.assertEqual(res.returncode, 0, f"bash -n failed:\n{res.stderr}")

    def test_status_when_not_installed_does_not_error(self):
        # This assumes the real agent is not installed on this machine; if it
        # IS installed (Saurabh ran `make install-agent` for real), status
        # still must not fail or write anything — just report differently.
        res = subprocess.run([AGENT_CTL, "status"], capture_output=True, text=True, timeout=15, cwd=ROOT)
        self.assertEqual(res.returncode, 0, f"status failed:\n{res.stdout}\n{res.stderr}")


class TestMakeTargets(unittest.TestCase):
    def setUp(self):
        _skip_if_missing(os.path.join(ROOT, "Makefile"), "Makefile")
        if not shutil.which("make"):
            raise unittest.SkipTest("SKIP: make not on PATH")

    def test_install_agent_dry_run_touches_no_state(self):
        before = _plist_mtime_or_none()
        # `make install-agent --dry-run`: GNU/BSD make treats a bare
        # `--dry-run` as its own -n flag (verified: it prints the recipe
        # line instead of running it), so this never calls launchctl or
        # writes ~/Library — no special-casing needed in the Makefile.
        res = subprocess.run(
            ["make", "install-agent", "--dry-run"],
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        )
        self.assertEqual(res.returncode, 0, f"make install-agent --dry-run failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("agent-ctl.sh", res.stdout, "expected make -n to print the recipe command")
        after = _plist_mtime_or_none()
        self.assertEqual(before, after, "make install-agent --dry-run must not touch ~/Library/LaunchAgents")

    def test_uninstall_agent_dry_run_touches_no_state(self):
        before = _plist_mtime_or_none()
        res = subprocess.run(
            ["make", "uninstall-agent", "--dry-run"],
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        )
        self.assertEqual(res.returncode, 0, f"make uninstall-agent --dry-run failed:\n{res.stdout}\n{res.stderr}")
        after = _plist_mtime_or_none()
        self.assertEqual(before, after, "make uninstall-agent --dry-run must not touch ~/Library/LaunchAgents")


if __name__ == "__main__":
    unittest.main(verbosity=2)
