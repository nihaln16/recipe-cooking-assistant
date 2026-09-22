"""Process startup rules for local runs and Render."""

from __future__ import annotations

import os

# Placeholders that must never be accepted on Render.
_SESSION_PLACEHOLDERS = frozenset(
    {
        "dev-only-change-me",
        "change-me-to-a-long-random-string",
    }
)


class ProductionConfigError(RuntimeError):
    def __init__(self, missing: list[str]) -> None:
        names = ", ".join(missing)
        super().__init__(
            "Refusing to start. Set these environment variables: "
            f"{names}. Do not commit their values."
        )
        self.missing = missing


def on_render() -> bool:
    return os.environ.get("RENDER") == "true"


def assert_production_secrets() -> None:
    """Require real env vars on Render. Local runs may use a gitignored .env."""
    if not on_render():
        return
    missing: list[str] = []
    session = os.environ.get("SESSION_SECRET", "").strip()
    if not session or session in _SESSION_PLACEHOLDERS:
        missing.append("SESSION_SECRET")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        missing.append("OPENAI_API_KEY")
    if missing:
        raise ProductionConfigError(missing)


def serve_host() -> str:
    return "0.0.0.0" if on_render() else "127.0.0.1"


def serve_port() -> int:
    raw = os.environ.get("PORT", "8000")
    try:
        port = int(raw)
    except ValueError as exc:
        raise ProductionConfigError(["PORT"]) from exc
    if port <= 0:
        raise ProductionConfigError(["PORT"])
    return port
