"""Store interface. Holds parents, confirmed kids, the ENCRYPTED Webtop session and digest settings.
Never Webtop content (messages, homework, grades...). Keyed by parent, not channel, so WhatsApp can
share it in stage 2. Encryption itself is phase 5: the store only ever sees opaque bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol


class StoreError(Exception):
    """A database problem. Carries the HTTP status and Postgres code when known, never a key or a URL."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Parent:
    id: str
    channel: str
    channel_user_id: str


@dataclass(frozen=True)
class StoredKid:
    webtop_kid_id: str
    name: str
    class_name: str
    school_id: str
    school_name: str
    context_ref: str | None = None  # opaque pointer to the Webtop school/role context


CONTENT_LEVELS = ("none", "due", "all")


@dataclass(frozen=True)
class DigestSetting:
    """The evening reminder: on or off, at what Israel time, and how much it says. `content` is "none" (the fixed
    message, nothing about the children), "due" (what is due tomorrow, written by code) or "all" (also the day's
    other parts, written by code). Only the level is stored, never any school content (Erez 2026-10-13)."""

    enabled: bool
    local_time: time | None
    content: str = "none"

    def __post_init__(self) -> None:
        if self.content not in CONTENT_LEVELS:
            raise ValueError(f"content must be one of {CONTENT_LEVELS}")


@dataclass(frozen=True)
class SessionInfo:
    """Who has a stored Webtop session and since when it was saved (None when a store cannot tell)."""

    channel: str
    channel_user_id: str
    saved_at: datetime | None


class Store(Protocol):
    async def get_or_create_parent(self, channel: str, channel_user_id: str) -> Parent: ...

    async def find_parent(self, channel: str, channel_user_id: str) -> Parent | None: ...

    async def save_kids(self, parent_id: str, kids: list[StoredKid]) -> None: ...

    async def list_kids(self, parent_id: str) -> list[StoredKid]:
        """The parent's confirmed kids; the order is not specified."""
        ...

    async def save_session(self, parent_id: str, encrypted: bytes) -> None: ...

    async def load_session(self, parent_id: str) -> bytes | None: ...

    async def clear_session(self, parent_id: str) -> None: ...

    async def list_sessions(self) -> list[SessionInfo]:
        """Everyone with a stored session (the hourly health check walks this list). No session contents."""
        ...

    async def set_digest(self, parent_id: str, setting: DigestSetting) -> None:
        """Raises ValueError for an enabled digest without a time."""
        ...

    async def get_digest(self, parent_id: str) -> DigestSetting: ...

    async def due_digests(self, local_time: time) -> list[Parent]:
        """Parents whose nudge is enabled for exactly this hour and minute (seconds ignored), one row per
        channel identity."""
        ...

    async def delete_parent(self, parent_id: str) -> None: ...

    # ---- invites and the stored allowlist (who may use the bot besides the admin ids in the environment) ----

    async def create_invite(self, code_hash: str, created_by: str, expires_at: datetime) -> None:
        """Remember a one-time invite by the HASH of its code, never the code itself."""
        ...

    async def redeem_invite(self, code_hash: str, used_by: str, now: datetime) -> bool:
        """Use the invite up: True for exactly one caller while it is unused and not expired, else False.
        Atomic: two people opening it at once let in only one."""
        ...

    async def delete_invite(self, code_hash: str) -> None: ...

    async def add_allowed(self, channel: str, channel_user_id: str, added_via: str) -> None:
        """Idempotent."""
        ...

    async def remove_allowed(self, channel: str, channel_user_id: str) -> None: ...

    async def list_allowed(self) -> list[tuple[str, str]]:
        """(channel, channel_user_id) of everyone let in by an invite."""
        ...
