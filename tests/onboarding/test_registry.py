from datetime import date

from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.registry import AgentRegistry, store_parent
from easytop.onboarding.vault import SessionVault
from easytop.store.memory import InMemoryStore

COOKIES = {"webToken": "tok-1"}


class Harness:
    def __init__(self):
        self.store = InMemoryStore()
        self.vault = SessionVault(self.store, SessionCipher(new_key()))
        self.built: list[tuple[dict, object]] = []
        self.closed = 0
        self.check_result: object = True  # what a connector's check_session answers (or raises)
        harness = self

        def connector_factory(cookies, on_expired):
            harness.built.append((cookies, on_expired))
            c = FakeConnector(date(2026, 10, 4))

            async def aclose():
                harness.closed += 1

            async def check_session():
                if isinstance(harness.check_result, Exception):
                    raise harness.check_result
                return harness.check_result

            c.aclose = aclose
            c.check_session = check_session
            return c

        self.registry = AgentRegistry(
            self.store,
            self.vault,
            connector_factory=connector_factory,
            agent_factory=lambda connector: Agent(object(), connector_family(connector)),
        )

    async def connect(self, parent_id: str, cookies=COOKIES):
        sp = await store_parent(self.store, parent_id)
        await self.vault.save(sp.id, cookies)
        return sp


def connector_family(connector):
    from easytop.family.service import FamilyService

    return FamilyService(connector)


async def test_store_parent_splits_channel_and_id_and_is_stable():
    store = InMemoryStore()
    a = await store_parent(store, "telegram:42")
    b = await store_parent(store, "telegram:42")
    assert a == b and (a.channel, a.channel_user_id) == ("telegram", "42")


async def test_no_session_means_no_agent():
    h = Harness()
    assert await h.registry.agent_for("telegram:1") is None
    assert h.built == []


async def test_agent_is_built_from_the_decrypted_session_and_cached():
    h = Harness()
    await h.connect("telegram:1")
    first = await h.registry.agent_for("telegram:1")
    assert isinstance(first, Agent)
    assert await h.registry.agent_for("telegram:1") is first
    assert [c for c, _ in h.built] == [COOKIES]


async def test_parents_get_their_own_agents():
    h = Harness()
    await h.connect("telegram:1", {"webToken": "one"})
    await h.connect("telegram:2", {"webToken": "two"})
    a1, a2 = await h.registry.agent_for("telegram:1"), await h.registry.agent_for("telegram:2")
    assert a1 is not a2
    assert sorted(c["webToken"] for c, _ in h.built) == ["one", "two"]


async def test_an_expired_session_is_flagged_once_cleared_and_the_agent_dropped():
    h = Harness()
    sp = await h.connect("telegram:1")
    await h.registry.agent_for("telegram:1")
    _, on_expired = h.built[0]
    await on_expired()
    assert await h.registry.take_expired("telegram:1") is True
    assert await h.registry.take_expired("telegram:1") is False
    assert await h.vault.load(sp.id) is None
    assert await h.registry.agent_for("telegram:1") is None
    assert h.closed == 1


async def test_reconnect_replaces_the_agent_with_one_for_the_new_session():
    h = Harness()
    sp = await h.connect("telegram:1", {"webToken": "old"})
    old = await h.registry.agent_for("telegram:1")
    await h.vault.save(sp.id, {"webToken": "new"})
    await h.registry.invalidate("telegram:1")
    new = await h.registry.agent_for("telegram:1")
    assert new is not old and h.built[-1][0] == {"webToken": "new"}
    assert h.closed == 1


async def test_one_parents_expiry_does_not_touch_another():
    h = Harness()
    await h.connect("telegram:1")
    await h.connect("telegram:2", {"webToken": "two"})
    await h.registry.agent_for("telegram:1")
    keep = await h.registry.agent_for("telegram:2")
    await h.built[0][1]()
    assert await h.registry.agent_for("telegram:2") is keep
    assert await h.registry.take_expired("telegram:2") is False


async def test_session_status_asks_a_throwaway_connector_and_changes_nothing():
    from easytop.errors import SessionExpired, UpstreamError

    h = Harness()
    await h.connect("telegram:1")
    agent = await h.registry.agent_for("telegram:1")  # a cached agent, as after a question
    built_before = len(h.built)

    h.check_result = True
    assert await h.registry.session_status("telegram:1") is True
    h.check_result = False
    assert await h.registry.session_status("telegram:1") is False
    h.check_result = SessionExpired("gone")
    assert await h.registry.session_status("telegram:1") is False
    h.check_result = UpstreamError("503")
    assert await h.registry.session_status("telegram:1") is None  # unknown, never "dead"

    assert len(h.built) == built_before + 4 and h.closed >= 4  # one throwaway connector each, closed
    assert await h.registry.agent_for("telegram:1") is agent  # the cached agent is untouched
    assert await h.registry.take_expired("telegram:1") is False  # no stale "reconnect" flag
    assert await h.vault.load((await store_parent(h.store, "telegram:1")).id) == COOKIES


async def test_session_status_is_none_without_a_stored_session():
    h = Harness()
    assert await h.registry.session_status("telegram:9") is None


async def test_drop_session_forgets_the_dead_session_without_a_reconnect_flag():
    h = Harness()
    sp = await h.connect("telegram:1")
    await h.registry.agent_for("telegram:1")
    await h.registry.drop_session("telegram:1")
    assert await h.vault.load(sp.id) is None
    assert await h.registry.agent_for("telegram:1") is None
    assert await h.registry.take_expired("telegram:1") is False
