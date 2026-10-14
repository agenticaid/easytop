"""One-time connect links: the token in a "Connect Webtop" button names the parent it was made for.

The login page cannot see who the visitor is, so the token IS the identity for that page: unguessable
(256 bits), single use, short lived (30 minutes), one per parent (a new one replaces the old). Held in memory on the one
server that also serves the login page; a restart just means the parent taps the button again.
"""

from __future__ import annotations

import hmac
import secrets
import time

TTL_SECONDS = 1800


class ConnectLinks:
    def __init__(self, ttl: float = TTL_SECONDS):
        self._ttl = ttl
        self._by_token: dict[str, tuple[str, float]] = {}  # token -> (parent_id, expires_at)
        self._by_parent: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._by_token)

    def issue(self, parent_id: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self._forget_expired(now)
        old = self._by_parent.pop(parent_id, None)
        if old is not None:
            self._by_token.pop(old, None)
        token = secrets.token_urlsafe(32)
        self._by_token[token] = (parent_id, now + self._ttl)
        self._by_parent[parent_id] = token
        return token

    def peek(self, token: object, *, now: float | None = None) -> str | None:
        """The parent a live token belongs to, without using it up."""
        now = time.time() if now is None else now
        if not isinstance(token, str) or not token or len(token) > 200:
            return None
        found = None
        for known, (parent_id, expires) in self._by_token.items():
            # compare against every token, not just a dict hit, so timing says nothing about prefixes
            if hmac.compare_digest(known.encode(), token.encode()) and expires > now:
                found = parent_id
        return found

    def consume(self, token: object, *, now: float | None = None) -> str | None:
        parent_id = self.peek(token, now=now)
        if parent_id is not None:
            self._by_token.pop(token, None)  # type: ignore[arg-type]
            self._by_parent.pop(parent_id, None)
        return parent_id

    def _forget_expired(self, now: float) -> None:
        for token in [t for t, (_, exp) in self._by_token.items() if exp <= now]:
            parent_id, _ = self._by_token.pop(token)
            if self._by_parent.get(parent_id) == token:
                del self._by_parent[parent_id]
