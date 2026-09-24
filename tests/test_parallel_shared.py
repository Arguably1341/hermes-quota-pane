"""Parallel is the one deliberately shared card; all other profile secrets remain local."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent.secret_scope import current_secret_scope, load_env_file, reset_secret_scope, set_multiplex_active, set_secret_scope
from hermes_constants import get_hermes_home

PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "providers.py"
API_PATH = PROVIDERS_PATH.with_name("plugin_api.py")
spec = importlib.util.spec_from_file_location("quota_providers_shared_test", PROVIDERS_PATH)
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


class ParallelSharedTests(unittest.TestCase):
    def test_parallel_card_is_labeled_shared(self):
        spec = importlib.util.spec_from_file_location("quota_api_shared_label_test", API_PATH)
        api = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(api)
        self.assertIn("compartilhado", api.PROVIDER_LABELS["parallel"].lower())
        self.assertNotIn("compartilhado", api.PROVIDER_LABELS["firecrawl"].lower())

    def test_marie_reads_default_balance_and_rotates_only_default_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            marie = root / "profiles" / "marie"
            marie.mkdir(parents=True)
            (root / ".env").write_text("PARALLEL_OAUTH_CLIENT_ID=default-client\nPARALLEL_OAUTH_REFRESH_TOKEN=default-refresh\n")
            (marie / ".env").write_text("PARALLEL_OAUTH_CLIENT_ID=marie-client\nPARALLEL_OAUTH_REFRESH_TOKEN=marie-refresh\n")
            calls = []

            def fake_post(url, *, data, **kwargs):
                calls.append(("post", url, data.copy()))
                return Response({"access_token": "short-token", "refresh_token": "rotated-refresh"})

            def fake_get(url, *, headers, **kwargs):
                calls.append(("get", url, headers.copy()))
                return Response({"credit_balance_cents": 8579, "pending_debit_balance_cents": 0, "will_invoice": False})

            with patch.dict(os.environ, {"HERMES_HOME": str(marie)}), \
                 patch("hermes_constants._get_platform_default_hermes_home", return_value=root), \
                 patch.object(providers.httpx, "post", side_effect=fake_post), \
                 patch.object(providers.httpx, "get", side_effect=fake_get):
                set_multiplex_active(True)
                token = set_secret_scope(load_env_file(marie / ".env"))
                try:
                    from hermes_constants import get_default_hermes_root
                    self.assertEqual(get_default_hermes_root(), root)
                    snapshot = providers.parallel_snapshot()
                    self.assertEqual(current_secret_scope()["PARALLEL_OAUTH_REFRESH_TOKEN"], "marie-refresh")
                    self.assertNotEqual(os.environ.get("PARALLEL_OAUTH_REFRESH_TOKEN"), "rotated-refresh")
                    assert get_hermes_home() == marie  # override reset after the call
                finally:
                    reset_secret_scope(token)
                    set_multiplex_active(False)

            self.assertTrue(snapshot.available, snapshot.unavailable_reason)
            self.assertEqual(snapshot.details, ("Saldo USD: 85.79",))
            self.assertEqual(calls[0][2]["refresh_token"], "default-refresh")
            self.assertEqual(calls[0][2]["client_id"], "default-client")
            self.assertEqual(calls[0][2]["grant_type"], "refresh_token")
            self.assertEqual(load_env_file(root / ".env")["PARALLEL_OAUTH_REFRESH_TOKEN"], "rotated-refresh")
            self.assertEqual(load_env_file(marie / ".env")["PARALLEL_OAUTH_REFRESH_TOKEN"], "marie-refresh")
    def test_failed_rotation_never_reports_successful_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            root.mkdir()
            (root / ".env").write_text("PARALLEL_OAUTH_CLIENT_ID=client\nPARALLEL_OAUTH_REFRESH_TOKEN=initial\n")
            with patch.dict(os.environ, {"HERMES_HOME": str(root)}), \
                 patch("hermes_constants._get_platform_default_hermes_home", return_value=root), \
                 patch.object(providers.httpx, "post", return_value=Response({"access_token": "access", "refresh_token": "rotated"})), \
                 patch.object(providers.httpx, "get") as get_balance, \
                 patch.object(providers, "_persist_parallel_refresh", side_effect=OSError("disk full")):
                snapshot = providers.parallel_snapshot()
            self.assertFalse(snapshot.available)
            self.assertIn("renovado", snapshot.unavailable_reason)
            get_balance.assert_not_called()

    def test_silent_refusal_to_save_rotated_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            root.mkdir()
            (root / ".env").write_text("PARALLEL_OAUTH_REFRESH_TOKEN=initial\n")
            with patch.dict(os.environ, {"HERMES_HOME": str(root)}), \
                 patch("hermes_constants._get_platform_default_hermes_home", return_value=root), \
                 patch.object(providers.httpx, "post", return_value=Response({"access_token": "access", "refresh_token": "rotated"})), \
                 patch.object(providers.httpx, "get") as get_balance, \
                 patch("hermes_cli.config._write_env_lines", return_value=None):
                snapshot = providers.parallel_snapshot()
            self.assertFalse(snapshot.available)
            self.assertIn("renovado", snapshot.unavailable_reason)
            get_balance.assert_not_called()

    def test_two_backends_serialize_refresh_and_use_latest_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            marie = root / "profiles" / "marie"
            marie.mkdir(parents=True)
            (root / ".env").write_text("PARALLEL_OAUTH_CLIENT_ID=shared-client\nPARALLEL_OAUTH_REFRESH_TOKEN=initial-token\n")
            (marie / ".env").write_text("MARIE_ONLY=untouched\n")
            server = {"refresh": "initial-token", "calls": []}
            gate = threading.Lock()

            def fake_post(url, *, data, **kwargs):
                with gate:
                    server["calls"].append(data["refresh_token"])
                    if data["refresh_token"] != server["refresh"]:
                        return Response({})  # stale grant rejected by the token server
                    time.sleep(0.08)  # give another process time to read the old .env
                    server["refresh"] = "rotated-" + str(len(server["calls"]))
                    return Response({"access_token": "short-token", "refresh_token": server["refresh"]})

            def fake_get(url, **kwargs):
                return Response({"credit_balance_cents": 200, "will_invoice": False})

            with patch.dict(os.environ, {"HERMES_HOME": str(marie)}), \
                 patch("hermes_constants._get_platform_default_hermes_home", return_value=root), \
                 patch.object(providers.httpx, "post", side_effect=fake_post), \
                 patch.object(providers.httpx, "get", side_effect=fake_get):
                from hermes_constants import get_default_hermes_root
                self.assertEqual(get_default_hermes_root(), root)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(providers.parallel_snapshot)
                    second = pool.submit(providers.parallel_snapshot)
                    snapshots = [first.result(timeout=5), second.result(timeout=5)]

            self.assertTrue(all(s.available for s in snapshots), [s.unavailable_reason for s in snapshots])
            self.assertEqual(server["calls"], ["initial-token", "rotated-1"])
            self.assertEqual(load_env_file(root / ".env")["PARALLEL_OAUTH_REFRESH_TOKEN"], "rotated-2")
            self.assertEqual(load_env_file(marie / ".env"), {"MARIE_ONLY": "untouched"})


if __name__ == "__main__":
    unittest.main()
