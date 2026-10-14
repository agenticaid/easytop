"""Store on Supabase Postgres, through the Data API (PostgREST) with the server-side service key.

Server-only access: the bot holds the service key (from the environment, never the database), the tables have
RLS on with no policies and anon/authenticated have no privileges, so nothing else can read them. Only the
Easytop tables are touched: parent identity, confirmed kids, the ENCRYPTED Webtop session (opaque bytes) and
digest settings. Never Webtop content.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, time
from typing import Any

import httpx

from easytop.store.base import DigestSetting, Parent, SessionInfo, StoredKid, StoreError

UNIQUE_VIOLATION = "23505"


def supabase_setting(name: str) -> str | None:
    """`SUPABASE_URL` / `SUPABASE_SERVICE_KEY` as Cloud Run's secrets set them, else the `_DEV` name from the Mac's
    .env (Erez 2026-10-11). The `_PROD` names are never read here, so a local run cannot touch the prod database."""
    return os.environ.get(name) or os.environ.get(name + "_DEV") or None


def _in_list(values: list[str]) -> str:
    """A PostgREST `in` list with every value double-quoted (commas, quotes and backslashes are safe)."""
    quoted = ['"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values]
    return "(" + ",".join(quoted) + ")"


class SupabaseStore:
    def __init__(
        self,
        url: str,
        service_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self._base = url.rstrip("/") + "/rest/v1"
        # Per Supabase's API key guide the key goes on `apikey` only: secret keys are not JWTs.
        self._headers = {"apikey": service_key}
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    @classmethod
    def from_env(cls) -> SupabaseStore:
        url, key = supabase_setting("SUPABASE_URL"), supabase_setting("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise StoreError("set SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (.env)")
        return cls(url, key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        prefer: str | None = None,
    ) -> list[dict[str, Any]]:
        headers = {**self._headers, **({"Prefer": prefer} if prefer else {})}
        try:
            r = await self._client.request(
                method, f"{self._base}/{table}", params=params, json=json, headers=headers
            )
        except httpx.HTTPError as e:  # its message holds the URL: never pass it on
            raise StoreError(f"cannot reach the database ({type(e).__name__})") from None
        if r.status_code >= 400:
            try:
                code = r.json().get("code")
            except ValueError:
                code = None
            raise StoreError(
                f"database request failed: {method} {table} HTTP {r.status_code}"
                + (f" code {code}" if code else ""),
                status=r.status_code,
                code=code,
            )
        return r.json() if r.content else []

    # ---- parents ----
    async def find_parent(self, channel: str, channel_user_id: str) -> Parent | None:
        rows = await self._request(
            "GET",
            "parent_channels",
            params={
                "select": "parent_id",
                "channel": f"eq.{channel}",
                "channel_user_id": f"eq.{channel_user_id}",
                "limit": "1",
            },
        )
        return Parent(rows[0]["parent_id"], channel, channel_user_id) if rows else None

    async def get_or_create_parent(self, channel: str, channel_user_id: str) -> Parent:
        if (found := await self.find_parent(channel, channel_user_id)) is not None:
            return found
        new_id = str(uuid.uuid4())  # made here, so the insert never depends on server defaults
        await self._request("POST", "parents", json={"id": new_id}, prefer="return=minimal")
        try:
            await self._request(
                "POST",
                "parent_channels",
                json={
                    "parent_id": new_id,
                    "channel": channel,
                    "channel_user_id": channel_user_id,
                },
                prefer="return=minimal",
            )
        except StoreError as e:
            await self.delete_parent(new_id)  # do not leave an orphan parent behind
            if e.code == UNIQUE_VIOLATION and (
                winner := await self.find_parent(channel, channel_user_id)
            ):
                return winner  # two first logins at once: the other one won
            raise
        return Parent(new_id, channel, channel_user_id)

    async def delete_parent(self, parent_id: str) -> None:
        await self._request("DELETE", "parents", params={"id": f"eq.{parent_id}"})  # cascades

    # ---- kids ----
    async def save_kids(self, parent_id: str, kids: list[StoredKid]) -> None:
        """Replace the set: upsert the given kids first, then delete the others, so a failed write
        never leaves the parent with fewer kids than before."""
        if kids:
            await self._request(
                "POST",
                "kids",
                params={"on_conflict": "parent_id,webtop_kid_id"},
                json=[
                    {
                        "parent_id": parent_id,
                        "webtop_kid_id": k.webtop_kid_id,
                        "name": k.name,
                        "class_name": k.class_name,
                        "school_id": k.school_id,
                        "school_name": k.school_name,
                        "context_ref": k.context_ref,
                    }
                    for k in kids
                ],
                prefer="resolution=merge-duplicates,return=minimal",
            )
        stale: dict[str, str] = {"parent_id": f"eq.{parent_id}"}
        if kids:
            stale["webtop_kid_id"] = "not.in." + _in_list([k.webtop_kid_id for k in kids])
        await self._request("DELETE", "kids", params=stale)

    async def list_kids(self, parent_id: str) -> list[StoredKid]:
        rows = await self._request(
            "GET",
            "kids",
            params={
                "select": "webtop_kid_id,name,class_name,school_id,school_name,context_ref",
                "parent_id": f"eq.{parent_id}",
                "order": "webtop_kid_id.asc",
            },
        )
        return [StoredKid(**row) for row in rows]

    # ---- session (ciphertext) ----
    async def save_session(self, parent_id: str, encrypted: bytes) -> None:
        await self._request(
            "POST",
            "webtop_sessions",
            params={"on_conflict": "parent_id"},
            json={
                "parent_id": parent_id,
                "encrypted_session": "\\x" + encrypted.hex(),  # PostgREST bytea = \x-hex
                "updated_at": datetime.now(UTC).isoformat(),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def load_session(self, parent_id: str) -> bytes | None:
        rows = await self._request(
            "GET",
            "webtop_sessions",
            params={"select": "encrypted_session", "parent_id": f"eq.{parent_id}"},
        )
        return bytes.fromhex(rows[0]["encrypted_session"].removeprefix("\\x")) if rows else None

    async def clear_session(self, parent_id: str) -> None:
        await self._request("DELETE", "webtop_sessions", params={"parent_id": f"eq.{parent_id}"})

    async def list_sessions(self) -> list[SessionInfo]:
        rows = await self._request(
            "GET", "webtop_sessions", params={"select": "parent_id,updated_at"}
        )  # never the session column
        if not rows:
            return []
        saved = {r["parent_id"]: r["updated_at"] for r in rows}
        channels = await self._request(
            "GET",
            "parent_channels",
            params={
                "select": "parent_id,channel,channel_user_id",
                "parent_id": "in." + _in_list(list(saved)),
                "order": "channel_user_id.asc",
            },
        )
        return [
            SessionInfo(
                c["channel"],
                c["channel_user_id"],
                datetime.fromisoformat(saved[c["parent_id"]]) if saved[c["parent_id"]] else None,
            )
            for c in channels
        ]

    # ---- digest ----
    async def set_digest(self, parent_id: str, setting: DigestSetting) -> None:
        if setting.enabled and setting.local_time is None:
            raise ValueError("an enabled digest needs a time")
        await self._request(
            "POST",
            "digest_settings",
            params={"on_conflict": "parent_id"},
            json={
                "parent_id": parent_id,
                "enabled": setting.enabled,
                "local_time": setting.local_time.strftime("%H:%M:%S")
                if setting.local_time
                else None,
                "content": setting.content,
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def get_digest(self, parent_id: str) -> DigestSetting:
        rows = await self._request(
            "GET",
            "digest_settings",
            params={"select": "enabled,local_time,content", "parent_id": f"eq.{parent_id}"},
        )
        if not rows:
            return DigestSetting(False, None)
        t = rows[0]["local_time"]
        return DigestSetting(
            rows[0]["enabled"],
            time.fromisoformat(t) if t else None,
            rows[0].get("content") or "none",
        )

    async def due_digests(self, local_time: time) -> list[Parent]:
        rows = await self._request(
            "GET",
            "digest_settings",
            params={
                "select": "parent_id",
                "enabled": "eq.true",
                "local_time": f"eq.{local_time.strftime('%H:%M')}:00",
            },
        )
        if not rows:
            return []
        channels = await self._request(
            "GET",
            "parent_channels",
            params={
                "select": "parent_id,channel,channel_user_id",
                "parent_id": "in." + _in_list([r["parent_id"] for r in rows]),
                "order": "channel_user_id.asc",
            },
        )
        return [Parent(c["parent_id"], c["channel"], c["channel_user_id"]) for c in channels]

    # ---- invites and the stored allowlist ----
    async def create_invite(self, code_hash: str, created_by: str, expires_at: datetime) -> None:
        await self._request(
            "POST",
            "invites",
            json={
                "code_hash": code_hash,
                "created_by": created_by,
                "expires_at": expires_at.isoformat(),
            },
            prefer="return=minimal",
        )

    async def redeem_invite(self, code_hash: str, used_by: str, now: datetime) -> bool:
        """One conditional UPDATE ... WHERE unused AND not expired RETURNING: the database decides who wins."""
        rows = await self._request(
            "PATCH",
            "invites",
            params={
                "code_hash": f"eq.{code_hash}",
                "used_at": "is.null",
                "expires_at": f"gt.{now.isoformat()}",
                "select": "code_hash",
            },
            json={"used_by": used_by, "used_at": now.isoformat()},
            prefer="return=representation",
        )
        return bool(rows)

    async def delete_invite(self, code_hash: str) -> None:
        await self._request("DELETE", "invites", params={"code_hash": f"eq.{code_hash}"})

    async def add_allowed(self, channel: str, channel_user_id: str, added_via: str) -> None:
        await self._request(
            "POST",
            "allowed_users",
            params={"on_conflict": "channel,channel_user_id"},
            json={"channel": channel, "channel_user_id": channel_user_id, "added_via": added_via},
            prefer="resolution=ignore-duplicates,return=minimal",
        )

    async def remove_allowed(self, channel: str, channel_user_id: str) -> None:
        await self._request(
            "DELETE",
            "allowed_users",
            params={"channel": f"eq.{channel}", "channel_user_id": f"eq.{channel_user_id}"},
        )

    async def list_allowed(self) -> list[tuple[str, str]]:
        rows = await self._request(
            "GET", "allowed_users", params={"select": "channel,channel_user_id"}
        )
        return [(r["channel"], r["channel_user_id"]) for r in rows]
