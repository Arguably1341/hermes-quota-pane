"""Quota Pane backend, mounted at /api/plugins/quota-pane/."""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

# Dashboard API modules are loaded by file path, not package import. Load the
# sibling adapters explicitly so the plugin remains a self-contained folder.
_spec = importlib.util.spec_from_file_location("hermes_quota_pane_providers", Path(__file__).with_name("providers.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("could not load quota-pane providers")
_providers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_providers)
FETCHERS = _providers.FETCHERS

router = APIRouter()

PROVIDER_LABELS = {
    "openai-codex": "ChatGPT / Codex",
    "opencode-go": "OpenCode Go",
    "commandcode": "Command Code",
    "deepseek": "DeepSeek",
    "firecrawl": "Firecrawl",
    "openrouter": "OpenRouter",
}
CACHE_TTL_SECONDS = 90.0
STALE_MAX_SECONDS = 300.0

_lock = threading.RLock()
_cache: dict[str, tuple[Any, float]] = {}
_in_flight: set[str] = set()
_last_attempt: dict[str, float] = {}
_failures: dict[str, float] = {}
_pool = ThreadPoolExecutor(max_workers=len(FETCHERS), thread_name_prefix="quota-pane")


@contextmanager
def _profile_secrets():
    """Bind the owning profile's secret scope around a fetcher call.

    A Desktop backend that serves more than one profile home flips agent.secret_scope to
    fail-closed: an unscoped secret read RAISES instead of borrowing another profile's value
    (tui_gateway/launch_profile_policy.py). Our fetchers run in pool threads, which do not inherit
    the request's contextvars, and the OpenRouter adapter resolves its key through that scope-aware
    path (agent/account_usage.py::_fetch_openrouter_account_usage), so without this the card freezes
    on its last good snapshot. Cores without the scoping API fall through to the plain call.
    """
    token = None
    try:
        from agent.secret_scope import set_secret_scope
        from hermes_constants import get_process_hermes_home

        home = get_process_hermes_home()
        try:
            from tui_gateway.launch_profile_policy import launch_secret_scope

            secrets = launch_secret_scope(home)
        except Exception:
            from agent.secret_scope import build_profile_secret_scope

            secrets = build_profile_secret_scope(Path(home))
        token = set_secret_scope(secrets)
    except Exception:
        token = None
    try:
        yield
    finally:
        if token is not None:
            try:
                from agent.secret_scope import reset_secret_scope

                reset_secret_scope(token)
            except Exception:
                pass


def _failure_reason(failing_since: float) -> str:
    minutes = int(max(0.0, time.monotonic() - failing_since) // 60)
    return f"coleta falhando há {minutes} min" if minutes else "coleta falhando há menos de 1 min"


def _refresh(provider: str) -> None:
    try:
        try:
            with _profile_secrets():
                snapshot = FETCHERS[provider]()
        except Exception:
            snapshot = None
        with _lock:
            if snapshot is None:
                # Keep the START of the streak: the age of the failure is what makes the card useful.
                _failures.setdefault(provider, time.monotonic())
            else:
                _failures.pop(provider, None)
            existing = _cache.get(provider)
            good_recent = existing is not None and existing[0].available and time.monotonic() - existing[1] <= STALE_MAX_SECONDS
            if snapshot is not None and (snapshot.available or not good_recent):
                _cache[provider] = (snapshot, time.monotonic())
    finally:
        with _lock:
            _in_flight.discard(provider)


def _schedule(*, force: bool = False) -> None:
    now = time.monotonic()
    for provider in FETCHERS:
        with _lock:
            existing = _cache.get(provider)
            fresh = existing is not None and now - existing[1] < CACHE_TTL_SECONDS
            backed_off = now - _last_attempt.get(provider, 0.0) < CACHE_TTL_SECONDS
            if provider in _in_flight or (not force and (fresh or backed_off)):
                continue
            _in_flight.add(provider)
            _last_attempt[provider] = now
        try:
            _pool.submit(_refresh, provider)
        except Exception:
            with _lock:
                _in_flight.discard(provider)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _provider_payload(provider: str) -> dict[str, Any]:
    with _lock:
        entry = _cache.get(provider)
        refreshing = provider in _in_flight
    base = {"provider": provider, "label": PROVIDER_LABELS[provider], "refreshing": refreshing}
    failing_since = _failures.get(provider)
    if entry is None:
        reason = "primeira coleta em andamento" if failing_since is None else f"primeira coleta falhou — {_failure_reason(failing_since)}"
        return {**base, "available": False, "windows": [], "details": [], "reason": reason}
    snapshot, monotonic_at = entry
    cache_age = max(0.0, time.monotonic() - monotonic_at)
    if cache_age > STALE_MAX_SECONDS:
        # Report WHY the data went stale; a bare "dados expirados" hid a permanent collection failure.
        reason = "dados expirados" if failing_since is None else f"dados expirados — {_failure_reason(failing_since)}"
        return {**base, "available": False, "windows": [], "details": [], "reason": reason}
    return {
        **base,
        "available": snapshot.available,
        "plan": snapshot.plan,
        "fetched_at": _iso(snapshot.fetched_at),
        "age_seconds": round(cache_age, 1),
        "stale": cache_age > CACHE_TTL_SECONDS,
        "windows": [
            {
                "label": window.label,
                "used_percent": window.used_percent,
                "remaining_percent": max(0.0, min(100.0, 100.0 - window.used_percent)) if window.used_percent is not None else None,
                "reset_at": _iso(window.reset_at),
                "detail": window.detail,
            }
            for window in snapshot.windows
        ],
        "details": list(snapshot.details),
        "reason": snapshot.unavailable_reason,
    }


def _payload() -> dict[str, Any]:
    with _lock:
        refreshing = bool(_in_flight)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "refreshing": refreshing,
        "providers": [_provider_payload(provider) for provider in FETCHERS],
    }


@router.get("/quota")
async def quota(refresh: bool = Query(False)) -> dict[str, Any]:
    _schedule(force=refresh)
    return _payload()


@router.post("/refresh")
async def refresh() -> dict[str, Any]:
    _schedule(force=True)
    return _payload()


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "providers": list(FETCHERS)}


async def wait_for_initial_data(timeout: float = 15.0) -> dict[str, Any]:
    """Test helper; production consumers poll GET /quota without blocking."""
    _schedule(force=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if len(_cache) == len(FETCHERS) and not _in_flight:
                break
        await asyncio.sleep(0.05)
    return _payload()
