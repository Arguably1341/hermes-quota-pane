# Hermes Quota Pane

A small native pane for Hermes Desktop showing account quota or balance for:

- ChatGPT / Codex
- OpenCode Go
- Command Code
- DeepSeek
- Firecrawl
- OpenRouter
- Parallel

It uses the supported Desktop Plugin SDK and a profile-aware `plugin_api.py` backend. It adds no tools, hooks, prompt content, footer fields, or core patches. Provider failures are isolated; the last good snapshot remains visible briefly while refresh retries run in the background.

## Install

```bash
hermes plugins install ./hermes-quota-pane
hermes plugins enable quota-pane
```

Restart the Hermes backend once so the Python API route is mounted. The Desktop half hot-loads and registers a right-side pane named **Cotas**.

A hand-written plugin dropped straight into `~/.hermes/plugins/` never gets the install-time
activation nudge, so already-running processes keep their boot-time plugin registry. Reload them hot
instead of restarting: the messaging gateway via the control-socket `reload-plugins` verb, the
Desktop backend via `POST /api/dashboard/agent-plugins/activate`. Editing `dashboard/*.py` still needs
a backend restart — the FastAPI route is imported once at boot.

## Credentials

The backend reuses Hermes provider credentials. It never returns credentials to the renderer.

- `openai-codex`: Hermes Codex OAuth/runtime credentials
- `opencode-go`: `OPENCODE_GO_API_KEY`
- `commandcode`: `COMMANDCODE_API_KEY`
- `deepseek`: `DEEPSEEK_API_KEY`
- `firecrawl`: `FIRECRAWL_API_KEY`
- `openrouter`: `OPENROUTER_API_KEY`
- `parallel`: `PARALLEL_OAUTH_REFRESH_TOKEN` + `PARALLEL_OAUTH_CLIENT_ID` (see below)

## Parallel balance

Parallel has no API-key path to the balance: `GET /account/service/v1/balance` rejects
`PARALLEL_API_KEY` (product scope) and accepts only an OAuth device-flow access token. The card
therefore needs a one-time login, run as a **Member** of the organization:

```bash
~/.hermes/hermes-agent/venv/bin/python \
    ~/.hermes/plugins/quota-pane/scripts/parallel_login.py
```

The script opens no browser (headless host): it prints a verification URL and a user code, waits for
the authorization, then stores the refresh token in the default Hermes home `.env`. Run the login
from the **default profile**, not with `hermes --profile marie` or
`HERMES_HOME=.../profiles/marie`. Subsequent rotations use the core's atomic file writer without
publishing the token to the current profile's in-memory secret scope or process environment.

**Intentional cross-profile exception:** Parallel is an organization-level balance shared across
profiles. The Desktop uses a separate backend per profile; only this card reads the token from the
default Hermes home's `.env`, and every backend writes the rotated token back to that same file. No
credential is copied into Marie's `.env`, and the token is never sent to the renderer. The card is
labeled **Parallel · compartilhado**. All other cards continue to resolve credentials in their own
profile. A `0600` interprocess lock in the default home serializes the refresh exchange so Ada and
Marie cannot race and invalidate each other's rotating token. This arrangement intentionally gives
the quota-pane backend of an enabled profile read access to this *one* shared credential; do not
enable the plugin for profiles whose operators must not see the organization's balance.

**New profiles** need the flag too: the pane backend mounts plugin API routes at boot and gates them
on the profile's `plugins.enabled`. A profile created with *clone from default* inherits it;
fresh-created profiles are covered by the `quota-pane-new-profiles` watchdog
(`~/.hermes/scripts/quota-pane-new-profiles.py`, every 10 minutes, silent unless it acts), and the
pane appears once that profile's backend recycles — an already-booted backend keeps the route
unmounted until it restarts (the Desktop pool evicts idle backends; quitting and reopening the app
restarts the default one).

Two independent limits keep this read-only:

- the grant requests the minimal scope **`balance:read`** — no `keys:*`, `apps:*` or `balance:add`;
- the adapter knows exactly one route, `GET /balance`, and never creates a key or charges a card.

The refresh token **rotates on every exchange** (the server returns a new pair). The adapter persists
and reads back the rotated value; if that write fails, the consumed pair is gone and the card reports
that the renewed token could not be saved until a new device flow. A postpaid organization
(`will_invoice: true`) returns zeros for both cent fields, so the card says so instead of showing a
fake balance.

## Data behavior

- cache TTL: 90 seconds
- stale-good horizon: 5 minutes
- one in-flight refresh per provider
- reads are immediate; refresh happens in worker threads
- missing or rejected credentials produce an explicit unavailable card, never a fake zero
- a stale card reports **why** it went stale (`coleta falhando há N min`), so a permanent collection
  failure is visible instead of masquerading as ordinary expiry

## Profile-scoped credentials

Worker threads do not inherit request contextvars, and a Desktop backend that serves more than one
profile home flips `agent.secret_scope` to fail-closed — an unscoped secret read *raises* rather than
borrowing another profile's value. Fetchers therefore run inside
`set_secret_scope(launch_secret_scope(get_process_hermes_home()))`; the OpenRouter card is the one that
breaks without it, because it is the only adapter that resolves its key through the scope-aware core
path (`agent/account_usage.py`) instead of reading `$HERMES_HOME/.env` directly.

Symptom if this regresses: **OpenRouter alone** shows `dados expirados` while the other four cards
stay fresh.
