"""Once the reminder is set it is fully automatic: the nightly nudge is fixed text sent by a timer and never calls
the model (Erez 2026-10-13). Only setting it in free words costs a model call."""

import inspect
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from easytop.adapters.model import LinkButton, Reply
from easytop.agent.core import Agent, Conversation
from easytop.connector.fake import FakeConnector
from easytop.digest import nudge
from easytop.digest.nudge import NUDGE, NudgeScheduler
from easytop.family.service import FamilyService
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.registry import AgentRegistry, store_parent
from easytop.onboarding.vault import SessionVault
from easytop.store.base import DigestSetting
from easytop.store.memory import InMemoryStore
from tests.agent.fakes import ScriptedClient, response, text, tool_use

TZ = ZoneInfo("Asia/Jerusalem")
MONDAY_EVENING = datetime(2026, 10, 5, 18, 42)  # tomorrow, Tuesday, is a school day


def test_the_nudge_module_cannot_reach_the_model():
    source = inspect.getsource(nudge)
    assert "anthropic" not in source.lower()
    assert "messages.create" not in source


async def test_setting_the_reminder_in_words_is_the_only_model_call():
    store = InMemoryStore()
    vault = SessionVault(store, SessionCipher(new_key()))
    client = ScriptedClient(
        response(tool_use("t1", "set_reminder", action="on", time="20:00")),
        response(text("Done, I'll remind you at 20:00.")),
    )

    def connector_factory(cookies, on_expired):
        return FakeConnector(date(2026, 10, 5))

    registry = AgentRegistry(
        store,
        vault,
        connector_factory=connector_factory,
        agent_factory=lambda c: Agent(
            client, FamilyService(c, clock=lambda: MONDAY_EVENING), clock=lambda: MONDAY_EVENING
        ),
    )
    sp = await store_parent(store, "telegram:1")
    await vault.save(sp.id, {"webToken": "tok"})

    agent = await registry.agent_for("telegram:1")
    r = await agent.ask(Conversation(), "remind me every evening at 8")
    assert r.tool_calls == ["set_reminder"]
    assert await store.get_digest(sp.id) == DigestSetting(True, time(20))  # bound to this parent
    calls_for_setting_it = len(client.calls)
    assert calls_for_setting_it == 2

    sent: list[tuple[str, Reply]] = []

    async def notify(pid, reply):
        sent.append((pid, reply))

    async def expired_reply(pid):
        return Reply(text="expired", link=LinkButton("reconnect", "https://x/?t=1"))

    scheduler = NudgeScheduler(
        store,
        registry,
        notify,
        expired_reply=expired_reply,
        clock=lambda: datetime(2026, 10, 5, 20, 0, 10, tzinfo=TZ),
    )
    assert await scheduler.tick() == 1
    assert sent == [("telegram:1", Reply(text=NUDGE))]  # fixed text, nothing about the kids
    assert len(client.calls) == calls_for_setting_it  # the nudge made no model call at all


async def test_a_content_reminder_is_written_by_code_every_evening_with_no_model_call():
    store = InMemoryStore()
    vault = SessionVault(store, SessionCipher(new_key()))
    client = ScriptedClient(
        response(tool_use("t1", "set_reminder", action="on", time="20:00", content="due")),
        response(text("הוגדר: 20:00 עם שיעורי הבית של מחר.")),
    )
    registry = AgentRegistry(
        store,
        vault,
        connector_factory=lambda cookies, on_expired: FakeConnector(date(2026, 10, 5)),
        agent_factory=lambda c: Agent(
            client, FamilyService(c, clock=lambda: MONDAY_EVENING), clock=lambda: MONDAY_EVENING
        ),
    )
    sp = await store_parent(store, "telegram:1")
    await vault.save(sp.id, {"webToken": "tok"})
    agent = await registry.agent_for("telegram:1")
    await agent.ask(Conversation(), "תזכיר לי ב-20:00 מה השיעורי בית של מחר")
    assert await store.get_digest(sp.id) == DigestSetting(True, time(20), "due")
    calls_for_setting_it = len(client.calls)

    sent: list[tuple[str, Reply]] = []

    async def notify(pid, reply):
        sent.append((pid, reply))

    async def expired_reply(pid):
        return Reply(text="expired", link=LinkButton("reconnect", "https://x/?t=1"))

    scheduler = NudgeScheduler(
        store,
        registry,
        notify,
        expired_reply=expired_reply,
        clock=lambda: datetime(2026, 10, 5, 20, 0, 10, tzinfo=TZ),
    )
    assert await scheduler.tick() == 1
    assert "42" in sent[0][1].text and sent[0][1].text != NUDGE  # tomorrow's homework, from Webtop's data
    assert len(client.calls) == calls_for_setting_it  # written by code: no model call
