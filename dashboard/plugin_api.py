"""Quota Pane backend, mounted at /api/plugins/quota-pane/."""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    "openrouter": "OpenRouter",
}
CACHE_TTL_SECONDS = 90.0
STALE_MAX_SECONDS = 300.0

_lock = threading.RLock()
_cache: dict[str, tuple[Any, float]] = {}
_in_flight: set[str] = set()
_last_attempt: dict[str, float] = {}
_pool = ThreadPoolExecutor(max_workers=len(FETCHERS), thread_name_prefix="quota-pane")


def _refresh(provider: str) -> None:
    try:
        snapshot = FETCHERS[provider]()
        with _lock:
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
    if entry is None:
        return {**base, "available": False, "windows": [], "details": [], "reason": "primeira coleta em andamento"}
    snapshot, monotonic_at = entry
    cache_age = max(0.0, time.monotonic() - monotonic_at)
    if cache_age > STALE_MAX_SECONDS:
        return {**base, "available": False, "windows": [], "details": [], "reason": "dados expirados"}
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
