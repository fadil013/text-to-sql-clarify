"""Proactive client-side pacing, so we stay under a provider's free-tier requests/minute cap
instead of bursting and relying on reactive 429/503 retries (which is slow and wastes quota).
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Blocks the caller just long enough to keep calls at least `min_interval_s` apart."""

    def __init__(self, min_interval_s: float):
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()
