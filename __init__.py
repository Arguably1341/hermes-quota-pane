"""Quota Pane has no model-visible hooks or tools; its backend is mounted from dashboard/plugin_api.py."""


def register(ctx):
    """Satisfy the native plugin contract without expanding the agent surface."""
