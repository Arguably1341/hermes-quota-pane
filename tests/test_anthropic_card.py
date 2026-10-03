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
             patch("agent.anthropic_credentials.claude_code_credentials_path", return_value=Path("/nonexistent/.credentials.json")), \
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

    def _run_cli_fallback(self, record):
        import json
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": record}), encoding="utf-8")
            with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=None), \
                 patch("agent.anthropic_credentials.claude_code_credentials_path", return_value=path), \
                 patch("agent.anthropic_credentials._refresh_oauth_token", side_effect=AssertionError("refreshed")), \
                 patch.object(providers.httpx, "get", return_value=Response(PAYLOAD)) as get:
                return providers.anthropic_snapshot(), get

    def test_cli_file_fallback_when_hermes_has_no_login(self):
        import time

        snap, get = self._run_cli_fallback({"accessToken": "sk-ant-oat01-cli", "expiresAt": (time.time() + 3600) * 1000})
        self.assertTrue(snap.available)
        self.assertEqual(snap.source, "oauth_usage_api_cli")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-ant-oat01-cli")

    def test_expired_cli_token_is_never_refreshed(self):
        import time

        snap, get = self._run_cli_fallback({"accessToken": "old", "refreshToken": "r", "expiresAt": (time.time() - 10) * 1000})
        self.assertFalse(snap.available)
        self.assertIn("expirado", snap.unavailable_reason)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
