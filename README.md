# Hermes Quota Pane

A small native pane for Hermes Desktop showing account quota or balance for:

- ChatGPT / Codex
- OpenCode Go
- Command Code
- DeepSeek
- OpenRouter

It uses the supported Desktop Plugin SDK and a profile-aware `plugin_api.py` backend. It adds no tools, hooks, prompt content, footer fields, or core patches. Provider failures are isolated; the last good snapshot remains visible briefly while refresh retries run in the background.

## Install

```bash
hermes plugins install ./hermes-quota-pane
hermes plugins enable quota-pane
```

Restart the Hermes backend once so the Python API route is mounted. The Desktop half hot-loads and registers a right-side pane named **Cotas**.

## Credentials

The backend reuses Hermes provider credentials. It never returns credentials to the renderer.

- `openai-codex`: Hermes Codex OAuth/runtime credentials
- `opencode-go`: `OPENCODE_GO_API_KEY`
- `commandcode`: `COMMANDCODE_API_KEY`
- `deepseek`: `DEEPSEEK_API_KEY`
- `openrouter`: `OPENROUTER_API_KEY`

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
