"""In-process rate limiting.

Not Redis, and deliberately so: studio is one process with one SQLite file, and
the documented switch point to Postgres is the same point where this would need
a shared store. A per-process sliding window is honest about what it can do — it
stops one client hammering one endpoint, and it does not pretend to stop a
distributed attack.

Two limits matter:

* **login requests, by client address** — the magic-link endpoint sends email, so
  it is the one place an attacker can make the server do expensive work on their
  behalf;
* **job submissions, by account** — a loop here costs credits and provider calls.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def check(self, key: str) -> float | None:
        """Count one hit. Returns None, or seconds to wait when over the limit."""
        now = self._clock()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.limit:
                return max(0.0, hits[0] + self.window - now)
            hits.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def _limiter(request: Request, name: str, limit: int, window: float) -> RateLimiter:
    """One limiter per app, created on first use.

    Created lazily rather than at import so a test that never runs the lifespan
    still gets a working limiter, and so a fresh app gets fresh counters.
    """
    store = getattr(request.app.state, "limiters", None)
    if store is None:
        store = {}
        request.app.state.limiters = store
    limiter = store.get(name)
    if limiter is None:
        limiter = RateLimiter(limit, window)
        store[name] = limiter
    return limiter


def client_address(request: Request) -> str:
    # Trust no proxy header by default: a spoofable X-Forwarded-For is worse than
    # no rate limit at all, because it looks like one.
    return request.client.host if request.client else "unknown"


def enforce_login_limit(request: Request, limit: int, window: float) -> None:
    limiter = _limiter(request, "login", limit, window)
    retry_after = limiter.check(client_address(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sign-in requests, try again shortly",
            headers={"retry-after": str(int(retry_after) + 1)},
        )


def enforce_job_limit(request: Request, user_id: str, limit: int, window: float) -> None:
    limiter = _limiter(request, "jobs", limit, window)
    retry_after = limiter.check(user_id)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many job submissions, slow down",
            headers={"retry-after": str(int(retry_after) + 1)},
        )


__all__ = [
    "RateLimiter",
    "client_address",
    "enforce_job_limit",
    "enforce_login_limit",
]
