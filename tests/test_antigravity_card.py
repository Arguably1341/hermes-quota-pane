"""Antigravity card: 4 bars (Gemini/Claude x 5h/7d) parsed from ``agy -p /quota --output-format json``."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "providers.py"
spec = importlib.util.spec_from_file_location("quota_providers_antigravity_test", PROVIDERS_PATH)
providers = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(providers)

# Shape captured from agy 1.2.16 (2026-10-04); group order deliberately reversed vs. card order.
PAYLOAD = {
    "status": "SUCCESS",
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Claude and GPT models", "buckets": [
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1, "reset_time": "2026-10-11T00:33:11Z"},
            {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.5, "reset_time": "2026-10-04T05:33:11Z"},
        ]},
        {"name": "Gemini Models", "buckets": [
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.9858, "reset_time": "2026-10-10T23:05:31Z"},
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.9426, "reset_time": "2026-10-04T04:05:31Z"},
        ]},
    ]}},
}


class AntigravityCardTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        token = Path(self.home.name) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("{}")

    def _run(self, stdout="", returncode=0, side_effect=None, logged_in=True):
        if not logged_in:
            (Path(self.home.name) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").unlink()
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        with patch.object(providers.Path, "home", return_value=Path(self.home.name)), \
             patch.object(providers, "_antigravity_binary", return_value="/usr/bin/agy"), \
             patch("subprocess.run", return_value=completed, side_effect=side_effect) as run:
            return providers.antigravity_snapshot(), run

    def test_four_windows_in_card_order_with_used_percent(self):
        snap, run = self._run(json.dumps(PAYLOAD))
        self.assertTrue(snap.available)
        self.assertEqual(
            [(w.label, round(w.used_percent, 2)) for w in snap.windows],
            [("Gemini 5h", 5.74), ("Gemini 7d", 1.42), ("Claude 5h", 50.0), ("Claude 7d", 0.0)],
        )
        self.assertTrue(all(w.reset_at is not None for w in snap.windows))
        args = run.call_args.args[0]
        self.assertEqual(args[1:5], ["-p", "/quota", "--output-format", "json"])
        self.assertIn("--log-file", args)

    def test_no_login_does_not_spawn_the_cli(self):
        snap, run = self._run(logged_in=False)
        self.assertFalse(snap.available)
        self.assertIn("login", snap.unavailable_reason)
        run.assert_not_called()

    def test_auth_prompt_is_reported_as_expired(self):
        snap, _ = self._run("Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/...")
        self.assertFalse(snap.available)
        self.assertIn("expirado", snap.unavailable_reason)

    def test_timeout_and_garbage_are_unavailable_not_zero(self):
        snap, _ = self._run(side_effect=subprocess.TimeoutExpired("agy", 25))
        self.assertFalse(snap.available)
        self.assertEqual(snap.windows, ())
        snap, _ = self._run("not json", returncode=1)
        self.assertFalse(snap.available)
        self.assertIn("exit 1", snap.unavailable_reason)


if __name__ == "__main__":
    unittest.main()
