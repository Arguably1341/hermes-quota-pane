"""Provider adapters owned by Quota Pane.

Only Codex and OpenRouter delegate to Hermes' stable account-usage contract.
OpenCode Go, Command Code, DeepSeek, and Firecrawl stay here so the plugin works
on stock Hermes and never relies on footer patches.
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, fetch_account_usage
from agent.secret_scope import get_secret
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


def firecrawl_snapshot() -> AccountUsageSnapshot:
    provider = "firecrawl"
    token = str(get_secret("FIRECRAWL_API_KEY", "") or "").strip()
    if not token:
        return _snapshot(provider, "firecrawl_credit_usage_api", unavailable_reason="sem credencial FIRECRAWL_API_KEY")
    try:
        response = httpx.get(
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        data = (response.json() or {}).get("data")
        if not isinstance(data, dict):
            return _snapshot(provider, "firecrawl_credit_usage_api", unavailable_reason="API sem saldo")
        remaining = _number(data.get("remainingCredits"))
        plan = _number(data.get("planCredits"))
        if remaining is None or plan is None or plan <= 0:
            return _snapshot(provider, "firecrawl_credit_usage_api", unavailable_reason="API sem saldo válido")
        used = max(0.0, min(100.0, (plan - remaining) / plan * 100.0))
        reset_at = _datetime(data.get("billingPeriodEnd"))
        return _snapshot(
            provider,
            "firecrawl_credit_usage_api",
            windows=[AccountUsageWindow(
                label="Ciclo",
                used_percent=used,
                reset_at=reset_at,
                detail=f"{remaining:.0f} de {plan:.0f} créditos",
            )],
        )
    except Exception as exc:
        return _snapshot(provider, "firecrawl_credit_usage_api", unavailable_reason=_http_reason(exc, "API de créditos"))


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


# ---- Parallel (Account API: só GET /balance) ------------------------------------
#
# O saldo vive em ``GET /account/service/v1/balance``, e esse endpoint RECUSA a
# ``PARALLEL_API_KEY`` (escopo de produto). Ele só aceita um access token do OAuth
# de dispositivo (RFC 8628) contra ``platform.parallel.ai/getServiceKeys/*``.
#
# O grant é pedido com o escopo MÍNIMO ``balance:read`` — sem ``keys:*``,
# ``apps:*`` nem ``balance:add``. O fetcher, por sua vez, conhece uma única rota:
# ``GET /balance``. São duas camadas independentes: o grant não cobra cartão nem
# emite chave, e este código não sabe fazer outra chamada.
#
# O refresh token ROTACIONA a cada troca (o servidor devolve um par novo), então o
# valor novo volta para o ``.env`` do perfil padrão (saldo organizacional compartilhado)
# pelo escritor atômico do core, sem publicar o segredo em ``os.environ`` nem
# no scope do backend de outro perfil. O lock interprocesso protege a releitura,
# a troca e a gravação. Se a gravação falhar, o par antigo já foi consumido:
# o card mostra indisponível até um novo device flow.

_PARALLEL_PLATFORM_URL = "https://platform.parallel.ai"
_PARALLEL_SERVICE_API_URL = "https://api.parallel.ai/account"
_PARALLEL_REFRESH_ENV = "PARALLEL_OAUTH_REFRESH_TOKEN"
_PARALLEL_CLIENT_ID_ENV = "PARALLEL_OAUTH_CLIENT_ID"
_PARALLEL_FALLBACK_CLIENT_ID = "parallel-cli"
_PARALLEL_REFRESH_GRANT = "refresh_token"


@contextmanager
def _parallel_shared_home():
    """Share only Parallel's account balance, keeping the other cards profile-local."""
    from hermes_constants import get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(get_default_hermes_root())
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@contextmanager
def _parallel_refresh_lock():
    """Serialize refresh read/exchange/write across profile backend processes."""
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / ".parallel-balance.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _persist_parallel_refresh(refresh_token: str) -> None:
    """Atomically update the shared .env without publishing its secret to Marie's scope."""
    from agent.secret_scope import load_env_file
    from hermes_cli.config import (
        _env_line_defines_key, _env_write_blocked, _quote_env_value,
        _read_env_lines, _write_env_lines,
    )
    from hermes_constants import get_hermes_home

    if _env_write_blocked(_PARALLEL_REFRESH_ENV, "set"):
        raise OSError("Parallel refresh token is managed and cannot be updated")
    env_path = Path(get_hermes_home()) / ".env"
    lines = _read_env_lines(env_path)
    updated = f"{_PARALLEL_REFRESH_ENV}={_quote_env_value(refresh_token)}\n"
    index = next((i for i, line in enumerate(lines) if _env_line_defines_key(line, _PARALLEL_REFRESH_ENV)), None)
    if index is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(updated)
    else:
        lines[index] = updated
    # save_env_value would also publish to os.environ / current_secret_scope, leaking
    # the shared token into the current profile's in-memory environment.
    _write_env_lines(env_path, lines, preserve_mode=True)
    if load_env_file(env_path).get(_PARALLEL_REFRESH_ENV) != refresh_token:
        raise OSError("Parallel refresh token was not persisted")


def _parallel_access_token() -> tuple[str, str | None]:
    """Troca o refresh token compartilhado e persiste a rotação sob lock interprocesso."""
    with _parallel_refresh_lock():
        return _parallel_access_token_locked()


def _parallel_access_token_locked() -> tuple[str, str | None]:
    """O lock cobre a releitura do .env, a troca no servidor e a gravação."""
    from agent.secret_scope import load_env_file
    from hermes_constants import get_hermes_home

    # Releia no disco: outro backend do Desktop pode ter acabado de rotacionar
    # o token. O scope herdado do worker pertence à conversa (e pode ser Marie).
    secrets = load_env_file(Path(get_hermes_home()) / ".env")
    refresh_token = str(secrets.get(_PARALLEL_REFRESH_ENV) or "").strip()
    if not refresh_token:
        return "", "sem login Parallel (rode o device flow do usuário Member)"
    client_id = str(secrets.get(_PARALLEL_CLIENT_ID_ENV) or "").strip() or _PARALLEL_FALLBACK_CLIENT_ID
    try:
        response = httpx.post(
            f"{_PARALLEL_PLATFORM_URL}/getServiceKeys/token",
            data={
                "grant_type": _PARALLEL_REFRESH_GRANT,
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 401, 403):
            return "", "login Parallel expirado — refaça o device flow"
        return "", _http_reason(exc, "token Parallel")
    except Exception as exc:
        return "", _http_reason(exc, "token Parallel")

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return "", "token Parallel sem access_token"
    rotated = str(payload.get("refresh_token") or "").strip()
    if rotated and rotated != refresh_token:
        try:
            _persist_parallel_refresh(rotated)
        except Exception:
            return "", "não foi possível salvar o token Parallel renovado — refaça o login"
    return access_token, None


def parallel_snapshot() -> AccountUsageSnapshot:
    # O card é deliberadamente organizacional: o token e a rotação moram no
    # .env do perfil padrão, mesmo quando o Desktop usa o backend da Marie.
    with _parallel_shared_home():
        return _parallel_snapshot_shared()


def _parallel_snapshot_shared() -> AccountUsageSnapshot:
    provider = "parallel"
    access_token, reason = _parallel_access_token()
    if not access_token:
        return _snapshot(provider, "parallel_balance_api", unavailable_reason=reason)
    try:
        response = httpx.get(
            f"{_PARALLEL_SERVICE_API_URL}/service/v1/balance",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except Exception as exc:
        return _snapshot(provider, "parallel_balance_api", unavailable_reason=_http_reason(exc, "API de saldo"))

    details: list[str] = []
    if bool(payload.get("will_invoice")):
        # Pós-pago: os dois campos de centavos vêm sempre 0, então não inventamos saldo.
        details.append("Organização pós-paga (fatura) — sem saldo pré-pago")
    else:
        credit = _number(payload.get("credit_balance_cents")) or 0.0
        details.append(f"Saldo USD: {credit / 100.0:.2f}")
        pending = _number(payload.get("pending_debit_balance_cents")) or 0.0
        if pending > 0:
            details.append(f"Retido em tarefas em voo: ${pending / 100.0:.2f}")
    return _snapshot(provider, "parallel_balance_api", details=details)


# Order here is the card order in the pane. Command Code stays defined but is filtered out
# while its plan is exhausted — see DISABLED_PROVIDERS in plugin_api.py.
FETCHERS: dict[str, Callable[[], AccountUsageSnapshot | None]] = {
    "openai-codex": codex_snapshot,
    "opencode-go": opencode_go_snapshot,
    "deepseek": deepseek_snapshot,
    "openrouter": openrouter_snapshot,
    "parallel": parallel_snapshot,
    "firecrawl": firecrawl_snapshot,
    "commandcode": commandcode_snapshot,
}
