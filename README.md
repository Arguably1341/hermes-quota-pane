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
