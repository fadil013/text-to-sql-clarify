"""Proactive client-side pacing, so we stay under a provider's free-tier requests/minute cap
instead of bursting and relying on reactive 429/503 retries (which is slow and wastes quota).
"""
from __future__ import annotations

import re
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


class TokenWindowLimiter:
    """Keeps estimated tokens sent in any rolling 60s window under a budget (e.g. Groq's free
    tier allows 8,000 tokens/minute on gpt-oss-20b). Spacing calls by time alone is not enough when
    every call carries ~4k tokens of schema + glossary. Clock/sleep are injectable for tests."""

    def __init__(self, tokens_per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self._budget = tokens_per_minute
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._events: list[tuple[float, int]] = []

    def wait(self, tokens: int) -> None:
        tokens = min(tokens, self._budget)  # a single oversized call must still be able to go
        with self._lock:
            while True:
                now = self._clock()
                self._events = [(t, n) for t, n in self._events if now - t < 60.0]
                used = sum(n for _, n in self._events)
                if used + tokens <= self._budget:
                    self._events.append((now, tokens))
                    return
                # sleep until the oldest event leaves the window, then re-check
                self._sleep(max(0.05, 60.0 - (now - self._events[0][0])))


def estimate_tokens(*texts: str, output_allowance: int = 600) -> int:
    """Rough request cost: chars/3.5 for the prompt plus room for the reply (incl. reasoning)."""
    return int(sum(len(t) for t in texts) / 3.5) + output_allowance


_RETRY_IN = re.compile(r"try again in ([\d.]+)\s*(ms|s)\b", re.I)
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"([\d.]+)s"')
MAX_RETRY_WAIT_S = 75.0


def retry_after_seconds(resp, default: float) -> float:
    """How long the server asked us to wait (Retry-After header, Groq's 'try again in 7.2s', or
    Gemini's retryDelay), never less than `default`, never more than MAX_RETRY_WAIT_S."""
    asked = 0.0
    header = resp.headers.get("retry-after")
    if header:
        try:
            asked = float(header)
        except ValueError:
            pass
    body = resp.text or ""
    m = _RETRY_IN.search(body)
    if m:
        asked = max(asked, float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0))
    m = _RETRY_DELAY.search(body)
    if m:
        asked = max(asked, float(m.group(1)))
    return min(max(default, asked + 0.5 if asked else default), MAX_RETRY_WAIT_S)
