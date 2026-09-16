"""Provider adapters owned by Quota Pane.

Only Codex and OpenRouter delegate to Hermes' stable account-usage contract.
OpenCode Go, Command Code, and DeepSeek stay here so the plugin works on stock
Hermes and never relies on footer patches.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, fetch_account_usage
from hermes_cli.auth import resolve_api_key_provider_credentials


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _snapshot(
    provider: str,
    source: str,
    *,
    windows: list[AccountUsageWindow] | None = None,
    details: list[str] | None = None,
    plan: str | None = None,
    unavailable_reason: str | None = None,
) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        provider=provider,
        source=source,
        fetched_at=_now(),
        plan=plan,
        windows=tuple(windows or ()),
        details=tuple(details or ()),
        unavailable_reason=unavailable_reason,
    )


def _credentials(provider: str) -> tuple[str, str]:
    try:
        resolved = resolve_api_key_provider_credentials(provider)
        return str(resolved.get("api_key") or "").strip(), str(resolved.get("base_url") or "").strip().rstrip("/")
    except Exception:
        return "", ""


def _http_reason(exc: Exception, label: str) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return f"credencial recusada (HTTP {status})"
        return f"{label} indisponível (HTTP {status})"
    return f"{label} indisponível"


def codex_snapshot() -> AccountUsageSnapshot | None:
    return fetch_account_usage("openai-codex")


def openrouter_snapshot() -> AccountUsageSnapshot | None:
    return fetch_account_usage("openrouter")


def opencode_go_snapshot() -> AccountUsageSnapshot:
    provider = "opencode-go"
    token, base_url = _credentials(provider)
    if not token:
        return _snapshot(provider, "opencode_go_usage_api", unavailable_reason="sem credencial OPENCODE_GO_API_KEY")
    url = f"{base_url or 'https://opencode.ai/zen/go/v1'}/usage"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=10.0)
        response.raise_for_status()
        usage = (response.json() or {}).get("usage")
        if not isinstance(usage, dict):
            raise ValueError("payload sem usage")
        windows: list[AccountUsageWindow] = []
        for key, label in (("rolling", "5h"), ("weekly", "7d"), ("monthly", "30d")):
            item = usage.get(key)
            if not isinstance(item, dict) or str(item.get("status") or "").lower() in {"error", "failed", "unavailable"}:
                continue
            percent = _number(item.get("percent"))
            if percent is None:
                continue
            windows.append(AccountUsageWindow(label, max(0.0, min(100.0, percent)), _datetime(item.get("resetsAt"))))
        return _snapshot(provider, "opencode_go_usage_api", windows=windows) if windows else _snapshot(
            provider, "opencode_go_usage_api", unavailable_reason="API sem dados de uso"
        )
    except Exception as exc:
        return _snapshot(provider, "opencode_go_usage_api", unavailable_reason=_http_reason(exc, "API de uso"))


_COMMANDCODE_PLANS: dict[str, tuple[str, float]] = {
    "individual-goat": ("GOAT", 70.0),
    "individual-go": ("Go", 10.0),
    "individual-pro-v1": ("Pro", 80.0),
    "individual-pro": ("Pro", 30.0),
    "individual-provider": ("Provider", 15.0),
    "individual-max": ("Max", 150.0),
    "individual-ultra": ("Ultra", 300.0),
    "teams-pro": ("Teams Pro", 40.0),
}


def _commandcode_plan(value: Any) -> tuple[str, float] | None:
    slug = str(value or "").strip().lower().replace("_", "-")
    for key in sorted(_COMMANDCODE_PLANS, key=len, reverse=True):
        if slug.startswith(key):
            return _COMMANDCODE_PLANS[key]
    return (slug.replace("individual-", "").replace("-", " ").title(), 0.0) if slug else None


def commandcode_snapshot() -> AccountUsageSnapshot:
    provider = "commandcode"
    token, _ = _credentials(provider)
    if not token:
        return _snapshot(provider, "commandcode_alpha_billing", unavailable_reason="sem credencial COMMANDCODE_API_KEY")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "cli",
        "x-cli-environment": "production",
    }
    try:
        with httpx.Client(base_url="https://api.commandcode.ai", headers=headers, timeout=10.0) as client:
            credits_response = client.get("/alpha/billing/credits")
            credits_response.raise_for_status()
            credits_payload = credits_response.json() or {}
            try:
                subscriptions_response = client.get("/alpha/billing/subscriptions")
                subscriptions_response.raise_for_status()
                subscriptions_payload = subscriptions_response.json() or {}
            except Exception:
                subscriptions_payload = {}
    except Exception as exc:
        return _snapshot(provider, "commandcode_alpha_billing", unavailable_reason=_http_reason(exc, "billing API"))

    windows: list[AccountUsageWindow] = []
    limits = credits_payload.get("windowLimits") if isinstance(credits_payload, dict) else None
    if isinstance(limits, dict):
        for key, label in (("fiveHour", "5h"), ("weekly", "7d")):
            item = limits.get(key)
            if not isinstance(item, dict):
                continue
            used, cap = _number(item.get("used")), _number(item.get("cap"))
            if used is None or cap is None or cap <= 0:
                continue
            reset_raw = _number(item.get("resetAt"))
            reset_at = _datetime(reset_raw / 1000.0) if reset_raw and reset_raw > 0 else None
            detail = "limite atingido" if item.get("exceeded") and reset_at is None else "janela ociosa" if used <= 0 and reset_at is None else None
            windows.append(AccountUsageWindow(label, max(0.0, min(100.0, used / cap * 100.0)), reset_at, detail))

    subscription = subscriptions_payload.get("data") if isinstance(subscriptions_payload, dict) else None
    plan = _commandcode_plan(subscription.get("planId") if isinstance(subscription, dict) else None)
    credits = credits_payload.get("credits") if isinstance(credits_payload, dict) else None
    details: list[str] = []
    if isinstance(credits, dict):
        monthly = max(0.0, _number(credits.get("monthlyCredits")) or 0.0)
        purchased = max(0.0, _number(credits.get("purchasedCredits")) or 0.0)
        free = max(0.0, _number(credits.get("freeCredits")) or 0.0)
        if purchased + free > 0:
            details.append(f"Créditos extras: ${purchased + free:.2f}")
        if plan and plan[1] > 0:
            remaining = monthly + purchased + free
            pool = max(plan[1], monthly) + purchased + free
            reset_at = _datetime(subscription.get("currentPeriodEnd")) if isinstance(subscription, dict) and str(subscription.get("status") or "").lower() == "active" else None
            windows.append(AccountUsageWindow("30d", max(0.0, min(100.0, (pool - remaining) / pool * 100.0)), reset_at))
    return _snapshot(provider, "commandcode_alpha_billing", windows=windows, details=details, plan=plan[0] if plan else None) if windows or details else _snapshot(
        provider, "commandcode_alpha_billing", unavailable_reason="billing API sem dados de uso"
    )


def deepseek_snapshot() -> AccountUsageSnapshot:
    provider = "deepseek"
    token, base_url = _credentials(provider)
    if not token:
        return _snapshot(provider, "deepseek_balance_api", unavailable_reason="sem credencial DEEPSEEK_API_KEY")
    url = f"{base_url or 'https://api.deepseek.com'}/user/balance"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=10.0)
        response.raise_for_status()
        payload = response.json() or {}
        rows = payload.get("balance_infos")
        if not isinstance(rows, list) or not rows:
            return _snapshot(provider, "deepseek_balance_api", unavailable_reason="API sem saldo")
        details = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "USD")
            total = _number(row.get("total_balance"))
            paid = _number(row.get("topped_up_balance"))
            granted = _number(row.get("granted_balance"))
            if total is None:
                continue
            suffix = []
            if paid is not None:
                suffix.append(f"pago {paid:.2f}")
            if granted is not None:
                suffix.append(f"concedido {granted:.2f}")
            details.append(f"Saldo {currency}: {total:.2f}" + (f" ({' · '.join(suffix)})" if suffix else ""))
        if payload.get("is_available") is False:
            details.append("Saldo indisponível para chamadas de API")
        return _snapshot(provider, "deepseek_balance_api", details=details) if details else _snapshot(
            provider, "deepseek_balance_api", unavailable_reason="API sem saldo válido"
        )
    except Exception as exc:
        return _snapshot(provider, "deepseek_balance_api", unavailable_reason=_http_reason(exc, "API de saldo"))


FETCHERS: dict[str, Callable[[], AccountUsageSnapshot | None]] = {
    "openai-codex": codex_snapshot,
    "opencode-go": opencode_go_snapshot,
    "commandcode": commandcode_snapshot,
    "deepseek": deepseek_snapshot,
    "openrouter": openrouter_snapshot,
}
