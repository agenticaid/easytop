"""In-memory Store for tests and local runs (nothing persisted)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from easytop.store.base import DigestSetting, Parent, SessionInfo, StoredKid


class InMemoryStore:
    def __init__(self) -> None:
        self._parents: dict[tuple[str, str], Parent] = {}
        self._kids: dict[str, list[StoredKid]] = {}
        self._sessions: dict[str, bytes] = {}
        self._session_saved: dict[str, datetime] = {}  # parent id -> when the session was last saved
        self._digest: dict[str, DigestSetting] = {}
        self._invites: dict[str, dict] = {}  # code hash -> created_by, expires_at, used_by
        self._allowed: dict[tuple[str, str], str] = {}  # (channel, user id) -> added_via

    async def find_parent(self, channel: str, channel_user_id: str) -> Parent | None:
        return self._parents.get((channel, channel_user_id))

    async def get_or_create_parent(self, channel: str, channel_user_id: str) -> Parent:
        key = (channel, channel_user_id)
        if key not in self._parents:
            self._parents[key] = Parent(str(uuid.uuid4()), channel, channel_user_id)
        return self._parents[key]

    async def save_kids(self, parent_id: str, kids: list[StoredKid]) -> None:
        self._kids[parent_id] = list(kids)

    async def list_kids(self, parent_id: str) -> list[StoredKid]:
        return list(self._kids.get(parent_id, []))

    async def save_session(self, parent_id: str, encrypted: bytes) -> None:
        self._sessions[parent_id] = encrypted
        self._session_saved[parent_id] = datetime.now(UTC)

    async def load_session(self, parent_id: str) -> bytes | None:
        return self._sessions.get(parent_id)

    async def clear_session(self, parent_id: str) -> None:
        self._sessions.pop(parent_id, None)
        self._session_saved.pop(parent_id, None)

    async def list_sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(p.channel, p.channel_user_id, self._session_saved.get(p.id))
            for p in self._parents.values()
            if p.id in self._sessions
        ]

    async def set_digest(self, parent_id: str, setting: DigestSetting) -> None:
        if setting.enabled and setting.local_time is None:
            raise ValueError("an enabled digest needs a time")
        self._digest[parent_id] = setting

    async def get_digest(self, parent_id: str) -> DigestSetting:
        return self._digest.get(parent_id, DigestSetting(False, None))

    async def due_digests(self, local_time: time) -> list[Parent]:
        minute = local_time.replace(second=0, microsecond=0)
        due = {
            pid
            for pid, d in self._digest.items()
            if d.enabled
            and d.local_time is not None
            and d.local_time.replace(second=0, microsecond=0) == minute
        }
        return [p for p in self._parents.values() if p.id in due]

    async def delete_parent(self, parent_id: str) -> None:
        self._parents = {k: v for k, v in self._parents.items() if v.id != parent_id}
        for d in (self._kids, self._sessions, self._session_saved, self._digest):
            d.pop(parent_id, None)

    async def create_invite(self, code_hash: str, created_by: str, expires_at: datetime) -> None:
        self._invites[code_hash] = {
            "created_by": created_by,
            "expires_at": expires_at,
            "used_by": None,
        }

    async def redeem_invite(self, code_hash: str, used_by: str, now: datetime) -> bool:
        invite = self._invites.get(code_hash)
        if invite is None or invite["used_by"] is not None or invite["expires_at"] <= now:
            return False
        invite["used_by"] = used_by  # no await between the check and this: atomic on one event loop
        return True

    async def delete_invite(self, code_hash: str) -> None:
        self._invites.pop(code_hash, None)

    async def add_allowed(self, channel: str, channel_user_id: str, added_via: str) -> None:
        self._allowed.setdefault((channel, channel_user_id), added_via)

    async def remove_allowed(self, channel: str, channel_user_id: str) -> None:
        self._allowed.pop((channel, channel_user_id), None)

    async def list_allowed(self) -> list[tuple[str, str]]:
        return list(self._allowed)
