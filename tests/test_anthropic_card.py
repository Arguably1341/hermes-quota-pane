"""Claude card: utilization is already a percent — values <= 1 must not be scaled."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "providers.py"
spec = importlib.util.spec_from_file_location("quota_providers_anthropic_test", PROVIDERS_PATH)
providers = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(providers)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


PAYLOAD = {
    "five_hour": {"utilization": 7.0, "resets_at": "2026-09-25T22:50:00+00:00"},
    "seven_day": {"utilization": 1.0, "resets_at": "2026-10-01T23:00:00+00:00"},
    "seven_day_opus": {"utilization": 50.0, "resets_at": None},
    "seven_day_sonnet": None,
}


class AnthropicCardTests(unittest.TestCase):
    def _run(self, payload, token="sk-ant-oat01-x"):
        with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=token), \
             patch.object(providers.httpx, "get", return_value=Response(payload)):
            return providers.anthropic_snapshot()

    def test_percent_is_not_rescaled_and_only_plan_windows_are_kept(self):
        snap = self._run(PAYLOAD)
        self.assertTrue(snap.available)
        self.assertEqual([(w.label, w.used_percent) for w in snap.windows], [("5h", 7.0), ("7d", 1.0)])
        self.assertIsNotNone(snap.windows[1].reset_at)

    def test_api_key_is_reported_as_unavailable(self):
        snap = self._run(PAYLOAD, token="sk-ant-api03-x")
        self.assertFalse(snap.available)
        self.assertIn("OAuth", snap.unavailable_reason)

    def test_missing_login_is_unavailable_not_zero(self):
        snap = self._run(PAYLOAD, token="")
        self.assertFalse(snap.available)
        self.assertEqual(snap.windows, ())


if __name__ == "__main__":
    unittest.main()
