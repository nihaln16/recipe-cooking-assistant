"""In-memory cap on paid imports for the public demo.

Counters live in this process only. A restart clears them.
"""

from __future__ import annotations

import threading
import time

from fastapi import Request

from recipe_cooking_assistant.runtime import on_render

LIMIT_MESSAGE = (
    "This demo can only extract a few recipes per hour. Please try again later."
)


class ImportLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_client: dict[str, list[float]] = {}
        self._process: list[float] = []

    def allow(
        self,
        client_key: str,
        *,
        per_client: int,
        per_process: int,
        window_seconds: int,
        now: float | None = None,
    ) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            cutoff = moment - window_seconds
            self._process = [stamp for stamp in self._process if stamp > cutoff]
            recent = [
                stamp
                for stamp in self._by_client.get(client_key, [])
                if stamp > cutoff
            ]
            if len(self._process) >= per_process or len(recent) >= per_client:
                self._by_client[client_key] = recent
                return False
            recent.append(moment)
            self._process.append(moment)
            self._by_client[client_key] = recent
            return True


def client_key(request: Request) -> str:
    """Identify the caller. Trust X-Forwarded-For only on Render."""
    if on_render():
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def request_exceeds_upload_budget(request: Request, *, max_images: int, max_upload_bytes: int) -> bool:
    """Reject a body larger than the existing per-image budget plus form overhead."""
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        length = int(raw)
    except ValueError:
        return True
    overhead = 256 * 1024
    return length > max_images * max_upload_bytes + overhead
