"""A parent's Webtop session cookies, encrypted at rest. The store only sees opaque bytes."""

from __future__ import annotations

import logging

from easytop.errors import SessionDecryptError
from easytop.onboarding.crypto import SessionCipher
from easytop.store.base import Store

log = logging.getLogger("easytop.vault")


class SessionVault:
    def __init__(self, store: Store, cipher: SessionCipher):
        self._store, self._cipher = store, cipher

    async def save(self, parent_id: str, cookies: dict[str, str]) -> None:
        await self._store.save_session(parent_id, self._cipher.encrypt(cookies))

    async def load(self, parent_id: str) -> dict[str, str] | None:
        blob = await self._store.load_session(parent_id)
        if blob is None:
            return None
        try:
            return self._cipher.decrypt(blob)
        except SessionDecryptError:
            # Wrong key or damaged data is useless to keep; the parent reconnects.
            log.warning("stored session unreadable, cleared parent=%s", parent_id)
            await self._store.clear_session(parent_id)
            return None

    async def clear(self, parent_id: str) -> None:
        await self._store.clear_session(parent_id)
