"""
limits.py -- a tiny in-memory rate limiter (token bucket).

Keeps one over-eager script, browser tab stuck in a loop, or bad actor from
using up a small free-tier server. Limits are per signed-in user (or per IP for
the public sign-in pages). Nothing is stored; a restart resets everyone.
"""
import threading
import time


class RateLimiter:
    def __init__(self, per_minute, burst=None):
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else per_minute)
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, key, cost=1):
        """Returns (allowed, seconds_to_wait)."""
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > 20_000:                       # forget people who've been quiet for a while
                cutoff = now - 600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (cost - tokens) / self.rate if self.rate else 60.0
