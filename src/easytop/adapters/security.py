"""Allowlist, per-parent rate limit and daily cap. All run before anything reaches Claude."""

from __future__ import annotations

import os
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


class Allowlist:
    """Empty allowlist denies everyone (fail closed)."""

    def __init__(self, ids: set[str] | frozenset[str] | list[str] = frozenset()):
        self._ids = frozenset(str(i).strip() for i in ids if str(i).strip())

    @classmethod
    def from_env(cls, var: str = "TELEGRAM_ALLOWED_IDS", prefix: str = "telegram:") -> Allowlist:
        raw = os.environ.get(var, "")
        return cls({f"{prefix}{p.strip()}" for p in raw.split(",") if p.strip()})

    def allow(self, parent_id: str) -> None:
        """Let one more id in (a person admitted by an invite)."""
        self._ids = self._ids | {parent_id}

    @property
    def ids(self) -> frozenset[str]:
        return self._ids

    def is_allowed(self, parent_id: str) -> bool:
        return parent_id in self._ids


class RateLimiter:
    """Sliding window: at most `limit` questions per `window_seconds` per parent."""

    def __init__(
        self,
        limit: int = 30,
        window_seconds: float = 3600.0,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self._limit, self._window, self._clock = limit, window_seconds, clock
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)

    def check(self, parent_id: str) -> float:
        """Record a hit and return 0 if allowed, else seconds to wait (hit not recorded)."""
        now = self._clock()
        q = self._hits[parent_id]
        while q and (now - q[0]).total_seconds() >= self._window:
            q.popleft()
        if len(q) >= self._limit:
            return self._window - (now - q[0]).total_seconds()
        q.append(now)
        return 0.0


ISRAEL = ZoneInfo("Asia/Jerusalem")


class DailyCap:
    """At most `limit` questions per parent per calendar day in Israel time (Erez 2026-10-13). Kept in memory, so
    a restart gives everyone a fresh day; only the current day is held, so it never grows. `take` counts a
    question atomically (no await between the check and the count); `refund` gives one back after a failure."""

    def __init__(
        self,
        limit: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.limit = limit
        self._clock = clock
        self._day: date | None = None
        self._counts: dict[str, int] = {}

    def _today(self) -> date:
        today = self._clock().astimezone(ISRAEL).date()
        if today != self._day:
            self._day, self._counts = today, {}
        return today

    def take(self, parent_id: str) -> bool:
        """Count one question and return True, or return False (nothing counted) when today's are used up."""
        self._today()
        used = self._counts.get(parent_id, 0)
        if used >= self.limit:
            return False
        self._counts[parent_id] = used + 1
        return True

    def refund(self, parent_id: str) -> None:
        self._today()
        if self._counts.get(parent_id, 0) > 0:
            self._counts[parent_id] -= 1
