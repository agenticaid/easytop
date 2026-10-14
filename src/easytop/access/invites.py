"""One-time invite links (Erez 2026-10-08). An admin asks the bot for an invite and gets a link such as
`t.me/<bot>?start=inv_<random>`. The first person to open it before it expires is added to the allowlist for good;
the link is then used up. Channel-agnostic: the adapter only supplies how to build the link.

Only the admin ids (the environment list, as it was when the service was made) can create invites; a guest never
becomes an admin. The code is stored only as a hash. A wrong, used or expired code looks to the caller exactly like
being a stranger, so nothing leaks about which codes exist. Logs never hold a code."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from easytop import analytics
from easytop.adapters.security import Allowlist
from easytop.store.base import Store

log = logging.getLogger("easytop.access")

INVITE_PREFIX = "inv_"
DEFAULT_TTL = timedelta(hours=24)
MAX_CODE_LEN = 64  # Telegram allows 64 characters in a start parameter
ISRAEL = ZoneInfo("Asia/Jerusalem")


@dataclass(frozen=True)
class Invite:
    url: str
    expires_at: datetime


def invite_code_of(text: str) -> str | None:
    """`/start inv_<code>` -> the code; anything else -> None."""
    parts = text.strip().split()
    if len(parts) != 2 or parts[0].lower() != "/start" or not parts[1].startswith(INVITE_PREFIX):
        return None
    code = parts[1].removeprefix(INVITE_PREFIX)
    return code if code and len(parts[1]) <= MAX_CODE_LEN else None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _term(ttl: timedelta) -> tuple[str, str]:
    """The term as Hebrew and English words: 24 hours, 1 hour, 30 minutes."""
    minutes = round(ttl.total_seconds() / 60)
    if minutes % 60 == 0:
        hours = minutes // 60
        return ("שעה אחת", "1 hour") if hours == 1 else (f"{hours} שעות", f"{hours} hours")
    return f"{minutes} דקות", f"{minutes} minutes"


class InviteService:
    def __init__(
        self,
        store: Store,
        allowlist: Allowlist,
        *,
        link: Callable[[str], str | None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = DEFAULT_TTL,
    ):
        """`link(payload)` builds the full link for a start payload, or None while the bot's username is unknown."""
        self._store, self._allow, self._link, self._clock, self._ttl = (
            store,
            allowlist,
            link,
            clock,
            ttl,
        )
        self._admins = allowlist.ids  # the environment list only: guests added later are not admins

    async def load(self) -> None:
        """At start-up: everyone let in earlier is allowed again."""
        for channel, user_id in await self._store.list_allowed():
            self._allow.allow(f"{channel}:{user_id}")

    def is_admin(self, parent_id: str) -> bool:
        return parent_id in self._admins

    async def create(self, parent_id: str) -> Invite | None:
        """A new invite, or None for a non-admin or while no link can be built."""
        if not self.is_admin(parent_id):
            return None
        code = secrets.token_urlsafe(16)
        url = self._link(INVITE_PREFIX + code)
        if url is None:
            return None
        expires_at = self._clock() + self._ttl
        await self._store.create_invite(_hash(code), parent_id, expires_at)
        log.info("invite created by=%s", parent_id)
        analytics.emit(
            "invite_created", parent_id, ttl_hours=round(self._ttl.total_seconds() / 3600)
        )
        return Invite(url, expires_at)

    async def redeem(self, code: str, parent_id: str) -> bool:
        """Use the code up and allow this person for good. False for any wrong, used or expired code."""
        if not await self._store.redeem_invite(_hash(code), parent_id, self._clock()):
            return False
        channel, _, user_id = parent_id.partition(":")
        await self._store.add_allowed(channel, user_id, "invite")
        self._allow.allow(parent_id)
        log.info("invite redeemed parent=%s", parent_id)
        analytics.emit("invite_redeemed", parent_id)
        return True

    def invite_text(self, invite: Invite) -> str:
        """What the admin reads: the link, how long it lives, and until when (Israel time)."""
        he, en = _term(self._ttl)
        until = invite.expires_at.astimezone(ISRAEL)
        when = f"{until:%H:%M}, {until.day}.{until.month}"
        return (
            f"קישור הזמנה חד־פעמי: {invite.url}\n"
            f"תקף ל־{he} (עד {when}), ונגמר אחרי שאדם אחד פותח אותו.\n\n"
            f"One-time invite link: {invite.url}\n"
            f"Expires in {en} (at {when}), and stops working once one person opens it."
        )
