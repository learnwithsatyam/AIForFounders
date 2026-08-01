"""Rate limiting, deliberately the simplest thing that bounds the bill.

In-memory and per-process. That means limits reset when you redeploy, and if
you ever run more than one worker each gets its own counters. For a link you
send to testers that is fine, and it costs you no Redis. When it stops being
fine, replace check() with the same logic backed by Redis and nothing else in
the app changes.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

HOUR = 3600.0
DAY = 86400.0


@dataclass
class Limiter:
    per_hour: int
    per_day_global: int

    _by_ip: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _global: deque[float] = field(default_factory=deque)

    def _sweep(self, q: deque[float], window: float, now: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def check(self, ip: str) -> str | None:
        """Return None if allowed, or a message explaining the refusal."""
        now = time.monotonic()

        self._sweep(self._global, DAY, now)
        if len(self._global) >= self.per_day_global:
            return (
                "This demo has hit its daily question limit. "
                "Please try again tomorrow."
            )

        q = self._by_ip[ip]
        self._sweep(q, HOUR, now)
        if len(q) >= self.per_hour:
            wait = int((HOUR - (now - q[0])) / 60) + 1
            return (
                f"You have reached {self.per_hour} questions this hour. "
                f"Please try again in about {wait} minutes."
            )

        q.append(now)
        self._global.append(now)

        # Keep the dict from growing without bound on a long-running process.
        if len(self._by_ip) > 10_000:
            for k in [k for k, v in self._by_ip.items() if not v]:
                del self._by_ip[k]

        return None

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        self._sweep(self._global, DAY, now)
        return {"questions_today": len(self._global), "tracked_ips": len(self._by_ip)}
