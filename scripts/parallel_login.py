#!/usr/bin/env python3
"""Login OAuth (device flow, RFC 8628) do Parallel para o card de saldo do Quota Pane.

Pede um grant com o escopo MÍNIMO ``balance:read`` — sem ``keys:*``, ``apps:*``
nem ``balance:add``. O fetcher do painel, por sua vez, só conhece ``GET /balance``.
São duas camadas independentes: mesmo que a conta do usuário seja Admin, este
grant não cobra o cartão nem emite chave de API.

Grava no ``.env`` do perfil (0600, pelo mesmo escritor que o core usa para o token
OAuth da Anthropic):

    PARALLEL_OAUTH_REFRESH_TOKEN   refresh token — ROTACIONA a cada troca
    PARALLEL_OAUTH_CLIENT_ID       client_id registrado, reusado no refresh

Uso, no host do Hermes, como o usuário dono do perfil:

    ~/.hermes/hermes-agent/venv/bin/python \\
        ~/.hermes/plugins/quota-pane/scripts/parallel_login.py

Autentique no navegador com o usuário Member da organização. O device flow é
interativo: este script fica aguardando a autorização e termina sozinho.
"""
from __future__ import annotations

import json
import os
import platform as _platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPO = Path(os.environ.get("HERMES_AGENT_REPO") or (Path.home() / ".hermes" / "hermes-agent"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli.config import get_env_value, save_env_value  # noqa: E402

PLATFORM_URL = os.environ.get("PARALLEL_PLATFORM_URL", "https://platform.parallel.ai").rstrip("/")
SERVICE_API_URL = os.environ.get("PARALLEL_SERVICE_API_URL", "https://api.parallel.ai/account").rstrip("/")

REFRESH_ENV = "PARALLEL_OAUTH_REFRESH_TOKEN"
CLIENT_ID_ENV = "PARALLEL_OAUTH_CLIENT_ID"
CLIENT_NAME = "hermes-quota-pane"

#: Escopo pedido. Deliberadamente NÃO inclui keys/apps/balance:add.
SCOPE = "balance:read"

DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def _post_form(path: str, data: dict[str, str], timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(
        f"{PLATFORM_URL}{path}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — HTTPS fixo
        return json.loads(response.read().decode())


def _post_json(path: str, body: dict, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        f"{PLATFORM_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — HTTPS fixo
        return json.loads(response.read().decode())


def _platform_info() -> dict[str, str]:
    raw = {
        "system": _platform.system(),
        "release": _platform.release(),
        "machine": _platform.machine(),
        "processor": _platform.processor(),
        "version": _platform.version(),
        "os_name": os.name,
    }
    return {k: v for k, v in raw.items() if v}


def ensure_client_id() -> str:
    """Reusa o client_id do perfil ou registra um novo (o refresh exige o mesmo client_id)."""
    stored = str(get_env_value(CLIENT_ID_ENV) or "").strip()
    if stored:
        return stored
    print("Registrando client OAuth com o Parallel…")
    data = _post_json("/getServiceKeys/register", {"client_name": CLIENT_NAME, "platform": _platform_info()})
    client_id = str(data.get("client_id") or "").strip()
    if not client_id:
        raise SystemExit("Registro do client não devolveu client_id.")
    save_env_value(CLIENT_ID_ENV, client_id)
    return client_id


def request_device_code(client_id: str) -> dict:
    try:
        return _post_form("/getServiceKeys/device/code", {"client_id": client_id, "scope": SCOPE})
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Falha ao pedir o device code: {exc.code} - {exc.read().decode()}") from exc


def poll_token(info: dict, client_id: str) -> dict:
    """Aguarda a autorização no navegador (RFC 8628 Step 3)."""
    interval = max(1, int(info.get("interval") or 5))
    deadline = time.monotonic() + int(info.get("expires_in") or 600)
    while time.monotonic() < deadline:
        try:
            return _post_form(
                "/getServiceKeys/token",
                {
                    "grant_type": DEVICE_CODE_GRANT_TYPE,
                    "device_code": str(info.get("device_code") or ""),
                    "client_id": client_id,
                },
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                error = str(json.loads(body).get("error") or "")
            except Exception:
                error = ""
            if error == "authorization_pending":
                pass
            elif error == "slow_down":
                interval += 5
            elif error == "expired_token":
                raise SystemExit("Device code expirou — rode o login de novo.") from exc
            elif error == "access_denied":
                raise SystemExit("Autorização negada no navegador.") from exc
            else:
                raise SystemExit(f"Troca de token falhou: {exc.code} - {body}") from exc
        time.sleep(interval)
    raise SystemExit("Tempo esgotado aguardando a autorização.")


def verify_balance(access_token: str) -> None:
    """Confirma que o token lê o saldo e mostra o que o card vai exibir."""
    request = urllib.request.Request(
        f"{SERVICE_API_URL}/service/v1/balance",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — HTTPS fixo
        payload = json.loads(response.read().decode())
    if payload.get("will_invoice"):
        print("Saldo: organização PÓS-PAGA (fatura) — os campos de centavos vêm sempre 0.")
        print("O card vai mostrar essa condição em vez de um valor.")
        return
    cents = float(payload.get("credit_balance_cents") or 0.0)
    pending = float(payload.get("pending_debit_balance_cents") or 0.0)
    print(f"Saldo pré-pago: ${cents / 100.0:.2f}" + (f" (retido: ${pending / 100.0:.2f})" if pending else ""))


def main() -> int:
    client_id = ensure_client_id()
    info = request_device_code(client_id)

    complete = str(info.get("verification_uri_complete") or info.get("verification_uri") or "")
    print()
    print("Autorize no navegador com o usuário MEMBER da organização Parallel:")
    print(f"  {complete}")
    print(f"  código: {info.get('user_code')}")
    print(f"  escopo pedido: {SCOPE}")
    print()
    print("Aguardando autorização…")

    token = poll_token(info, client_id)
    access_token = str(token.get("access_token") or "").strip()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise SystemExit("Resposta de token sem access_token/refresh_token.")

    granted = str(token.get("scope") or "").strip() or "(não informado)"
    print(f"Autorizado. Organização: {token.get('org_name') or token.get('org_id')}")
    print(f"Escopo concedido: {granted}")
    if granted != "(não informado)" and granted.split() != [SCOPE]:
        print(f"AVISO: o servidor concedeu escopo além de '{SCOPE}': {granted}")

    save_env_value(REFRESH_ENV, refresh_token)
    verify_balance(access_token)
    print()
    print(f"Refresh token gravado em {REFRESH_ENV} (~/.hermes/.env, 0600).")
    print("O card Parallel do painel de cotas aparece no próximo refresh (cache 90s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
