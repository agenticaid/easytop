"""One agent per parent, built from that parent's stored (encrypted) Webtop session.

Replaces the single `--session` file of the Phase 4 bot. The connector reports an expired session through
its `on_expired` hook: the registry then forgets the dead session, drops the cached agent and remembers
that this parent must reconnect, so the gateway can answer with a reconnect button instead of the model's
words.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from easytop import analytics
from easytop.agent.core import Agent
from easytop.connector.base import Connector
from easytop.digest.nudge import StoreReminders
from easytop.errors import EasytopError, SessionExpired
from easytop.onboarding.vault import SessionVault
from easytop.store.base import Parent, Store

log = logging.getLogger("easytop.registry")

ExpiredHook = Callable[[], Awaitable[None]]
ConnectorFactory = Callable[[dict[str, str], ExpiredHook], Connector]
AgentFactory = Callable[[Connector], Agent]


async def store_parent(store: Store, parent_id: str) -> Parent:
    """The store's parent row for a channel identity such as "telegram:12345"."""
    channel, _, channel_user_id = parent_id.partition(":")
    return await store.get_or_create_parent(channel, channel_user_id)


class AgentRegistry:
    def __init__(
        self,
        store: Store,
        vault: SessionVault,
        *,
        connector_factory: ConnectorFactory,
        agent_factory: AgentFactory,
    ):
        self._store, self._vault = store, vault
        self._connector_factory, self._agent_factory = connector_factory, agent_factory
        self._agents: dict[str, tuple[Agent, Any]] = {}  # parent_id -> (agent, connector)
        self._expired: set[str] = set()

    async def agent_for(self, parent_id: str) -> Agent | None:
        """The parent's agent, or None when they have no live Webtop session (not connected or expired)."""
        cached = self._agents.get(parent_id)
        if cached is not None:
            return cached[0]
        sp = await store_parent(self._store, parent_id)
        cookies = await self._vault.load(sp.id)
        if cookies is None:
            return None

        async def on_expired() -> None:
            await self._on_expired(parent_id, sp.id)

        connector = self._connector_factory(cookies, on_expired)
        agent = self._agent_factory(connector)
        agent.bind_reminders(StoreReminders(self._store, sp.id, parent_id))
        self._agents[parent_id] = (agent, connector)
        return agent

    async def session_status(self, parent_id: str) -> bool | None:
        """Is this parent's stored session alive? True / False, or None when it cannot be told (no stored session,
        or Webtop did not answer properly). Asks a throwaway connector: nothing cached, no expiry hook, no flag."""
        sp = await store_parent(self._store, parent_id)
        cookies = await self._vault.load(sp.id)
        if cookies is None:
            return None

        async def never() -> (
            None
        ):  # a health check only reports; it does not decide what dead means
            return None

        connector = self._connector_factory(cookies, never)
        try:
            return await connector.check_session()
        except SessionExpired:
            return False
        except EasytopError:
            return None
        finally:
            await self._close(connector)

    async def drop_session(self, parent_id: str) -> None:
        """Forget a session found dead by the health check: the stored copy and the cached agent. Unlike an expiry
        met while answering, no "reconnect" flag is raised; the parent's next message gets the usual notice."""
        sp = await store_parent(self._store, parent_id)
        await self._vault.clear(sp.id)
        await self.invalidate(parent_id)
        log.info("session dropped parent=%s", parent_id)

    async def take_expired(self, parent_id: str) -> bool:
        """True once after the parent's session was found expired; the flag then clears."""
        if parent_id in self._expired:
            self._expired.discard(parent_id)
            return True
        return False

    async def invalidate(self, parent_id: str) -> None:
        """Forget the cached agent (after a reconnect or an expiry); the next call rebuilds it."""
        cached = self._agents.pop(parent_id, None)
        if cached is not None:
            await self._close(cached[1])

    async def _on_expired(self, parent_id: str, store_id: str) -> None:
        log.info("session expired parent=%s", parent_id)
        analytics.emit("session_expired", parent_id)
        self._expired.add(parent_id)
        await self._vault.clear(store_id)
        await self.invalidate(parent_id)

    @staticmethod
    async def _close(connector: Any) -> None:
        close = getattr(connector, "aclose", None)
        if close is not None:
            res = close()
            if inspect.isawaitable(res):
                try:
                    await res
                except Exception as e:  # closing a dead connector must never break the flow
                    log.warning("closing a connector failed: %s", type(e).__name__)
