"""A store kept in one JSON file: for development and small runs, so a restart does not make every parent
onboard again. The same behaviour as the in-memory store (one shared contract suite checks both).

Sessions arrive already encrypted (Spec 03: the store only ever sees ciphertext) and are written as base64.
The file is owner-only (0600) and replaced atomically, so a crash never leaves half a file. Production uses
the Supabase store; this one is not for several processes at once.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime, time
from pathlib import Path

from easytop.store.base import DigestSetting, Parent, StoredKid
from easytop.store.memory import InMemoryStore

log = logging.getLogger("easytop.store")


class FileStore(InMemoryStore):
    def __init__(self, path: Path | str):
        super().__init__()
        self._path = Path(path)
        self._load()

    # ---- reading and writing the file ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            parents = [Parent(**p) for p in data["parents"]]
            kids = {pid: [StoredKid(**k) for k in items] for pid, items in data["kids"].items()}
            sessions = {pid: base64.b64decode(b64) for pid, b64 in data["sessions"].items()}
            digest = {
                pid: DigestSetting(
                    d["enabled"],
                    time.fromisoformat(d["time"]) if d["time"] else None,
                    d.get("content", "none"),  # files written before the level existed
                )
                for pid, d in data["digest"].items()
            }
        except (OSError, ValueError, KeyError, TypeError):
            # A damaged file must not stop the bot: start empty (parents connect again) and say so, never why.
            log.warning("store file unreadable, starting empty: %s", self._path.name)
            return
        self._parents = {(p.channel, p.channel_user_id): p for p in parents}
        self._kids, self._sessions, self._digest = kids, sessions, digest
        try:  # files written before the health check have no saved times
            self._session_saved = {
                pid: datetime.fromisoformat(t) for pid, t in data.get("session_saved", {}).items()
            }
        except (ValueError, TypeError):
            log.warning("session times in the store file unreadable, starting without them")
        try:  # files written before invites existed have neither key
            self._invites = {
                h: {
                    "created_by": i["created_by"],
                    "expires_at": datetime.fromisoformat(i["expires_at"]),
                    "used_by": i["used_by"],
                }
                for h, i in data.get("invites", {}).items()
            }
            self._allowed = {(a["channel"], a["id"]): a["via"] for a in data.get("allowed", [])}
        except (ValueError, KeyError, TypeError):
            log.warning("invites in the store file unreadable, starting without them")

    def _flush(self) -> None:
        data = {
            "parents": [
                {"id": p.id, "channel": p.channel, "channel_user_id": p.channel_user_id}
                for p in self._parents.values()
            ],
            "kids": {
                pid: [
                    {
                        "webtop_kid_id": k.webtop_kid_id,
                        "name": k.name,
                        "class_name": k.class_name,
                        "school_id": k.school_id,
                        "school_name": k.school_name,
                        "context_ref": k.context_ref,
                    }
                    for k in items
                ]
                for pid, items in self._kids.items()
            },
            "sessions": {pid: base64.b64encode(b).decode() for pid, b in self._sessions.items()},
            "session_saved": {pid: t.isoformat() for pid, t in self._session_saved.items()},
            "digest": {
                pid: {
                    "enabled": d.enabled,
                    "time": d.local_time.isoformat() if d.local_time else None,
                    "content": d.content,
                }
                for pid, d in self._digest.items()
            },
            "invites": {
                h: {
                    "created_by": i["created_by"],
                    "expires_at": i["expires_at"].isoformat(),
                    "used_by": i["used_by"],
                }
                for h, i in self._invites.items()
            },
            "allowed": [
                {"channel": ch, "id": uid, "via": via} for (ch, uid), via in self._allowed.items()
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".store-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:  # mkstemp creates it 0600
                json.dump(data, f)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- every change is written straight away ----

    async def get_or_create_parent(self, channel: str, channel_user_id: str) -> Parent:
        known = (channel, channel_user_id) in self._parents
        parent = await super().get_or_create_parent(channel, channel_user_id)
        if not known:
            self._flush()
        return parent

    async def save_kids(self, parent_id: str, kids: list[StoredKid]) -> None:
        await super().save_kids(parent_id, kids)
        self._flush()

    async def save_session(self, parent_id: str, encrypted: bytes) -> None:
        await super().save_session(parent_id, encrypted)
        self._flush()

    async def clear_session(self, parent_id: str) -> None:
        await super().clear_session(parent_id)
        self._flush()

    async def set_digest(self, parent_id: str, setting: DigestSetting) -> None:
        await super().set_digest(parent_id, setting)
        self._flush()

    async def delete_parent(self, parent_id: str) -> None:
        await super().delete_parent(parent_id)
        self._flush()

    async def create_invite(self, code_hash: str, created_by: str, expires_at: datetime) -> None:
        await super().create_invite(code_hash, created_by, expires_at)
        self._flush()

    async def redeem_invite(self, code_hash: str, used_by: str, now: datetime) -> bool:
        used = await super().redeem_invite(code_hash, used_by, now)
        if used:
            self._flush()
        return used

    async def delete_invite(self, code_hash: str) -> None:
        await super().delete_invite(code_hash)
        self._flush()

    async def add_allowed(self, channel: str, channel_user_id: str, added_via: str) -> None:
        await super().add_allowed(channel, channel_user_id, added_via)
        self._flush()

    async def remove_allowed(self, channel: str, channel_user_id: str) -> None:
        await super().remove_allowed(channel, channel_user_id)
        self._flush()
