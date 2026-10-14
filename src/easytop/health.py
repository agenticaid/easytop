"""The hourly session health check (Erez 2026-10-11).

Once an hour, inside the bot, every parent with a stored Webtop session is asked one cheap question (Webtop's own
CheckToken). A session that answers "dead" twice in a row (a glitch must never log a parent out) is forgotten, so
the parent's next message gets the usual reconnect notice instead of a failed fetch. Nobody is messaged by the check.

The log is content-free and doubles as a lifetime measurement: `session check parent=... valid=... age_h=...` every
hour and `session dead parent=... age_h=...` once, with how long that login lived. Never a cookie, never school data.
Note that the check is itself a call to Webtop, so it may count as activity for a session that is idle-limited.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from easytop import analytics
from easytop.store.base import SessionInfo, Store

log = logging.getLogger("easytop.health")

FIRST_CHECK_AFTER = 120.0  # seconds after the bot starts
CHECK_EVERY = 3600.0
RECHECK_AFTER = 5.0  # a "dead" answer is asked again after this long before it is believed


class SessionHealth:
    def __init__(
        self,
        store: Store,
        registry: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        first_after: float = FIRST_CHECK_AFTER,
        every: float = CHECK_EVERY,
        recheck_after: float = RECHECK_AFTER,
    ):
        """`registry` answers `session_status(parent_id)` (True / False / None) and `drop_session(parent_id)`."""
        self._store, self._registry = store, registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._first_after, self._every, self._recheck_after = first_after, every, recheck_after
        self._sleep = asyncio.sleep

    def _age(self, info: SessionInfo) -> float | None:
        if info.saved_at is None:
            return None
        return max(0.0, (self._clock() - info.saved_at).total_seconds() / 3600)

    def _age_hours(self, info: SessionInfo) -> str:
        age = self._age(info)
        return "unknown" if age is None else f"{age:.1f}"

    async def tick(self) -> dict[str, int]:
        """Check every stored session once; returns how many were ok, dead (and dropped) and unknown."""
        sessions = await self._store.list_sessions()
        outcomes = await asyncio.gather(*(self._check(info) for info in sessions))
        counts = {
            "ok": outcomes.count("ok"),
            "dead": outcomes.count("dead"),
            "unknown": outcomes.count("unknown"),
        }
        if sessions:
            log.info("session checks ok=%d dead=%d unknown=%d", *counts.values())
        return counts

    async def _check(self, info: SessionInfo) -> str:
        pid = f"{info.channel}:{info.channel_user_id}"
        age = self._age_hours(info)
        try:
            alive = await self._registry.session_status(pid)
            if alive is False:  # believed only when it is said twice
                await self._sleep(self._recheck_after)
                alive = await self._registry.session_status(pid)
            if alive is False:
                log.info("session dead parent=%s age_h=%s", pid, age)
                analytics.emit("session_check", pid, status="dead", age_h=self._age(info))
                await self._registry.drop_session(pid)
                return "dead"
            log.info("session check parent=%s valid=%s age_h=%s", pid, alive is True, age)
            status = "ok" if alive is True else "unknown"
            analytics.emit("session_check", pid, status=status, age_h=self._age(info))
            return "ok" if alive is True else "unknown"
        except Exception as e:  # one parent's problem must never stop the others
            log.warning("session check failed parent=%s err=%s", pid, type(e).__name__)
            analytics.emit_error("health", e)
            return "unknown"

    async def run(self) -> None:
        """Check shortly after start, then every hour, until cancelled."""
        await self._sleep(self._first_after)
        while True:
            try:
                await self.tick()
            except Exception as e:  # e.g. the database is briefly down: try again next hour
                log.warning("session health tick failed err=%s", type(e).__name__)
            await self._sleep(self._every)
