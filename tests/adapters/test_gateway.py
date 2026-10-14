import logging
from datetime import datetime

from easytop.adapters.gateway import ASK_SOMETHING, PRIVATE_BETA, WELCOME, Gateway
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import ScriptedClient, response, text

NOW = datetime(2026, 10, 5, 18, 42)
PARENT = "telegram:100"


def make(client, limit=30):
    def agent_for(parent_id):
        svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
        return Agent(client, svc, clock=lambda: NOW)

    return Gateway(Allowlist({PARENT}), RateLimiter(limit, clock=lambda: NOW), agent_for)


async def test_stranger_never_reaches_claude():
    c = ScriptedClient()
    r = await make(c).handle(Incoming("telegram:999", text="hi"))
    assert r.text == PRIVATE_BETA and c.calls == []


async def test_allowed_question_goes_to_the_agent_and_the_answer_comes_back_as_is():
    c = ScriptedClient(response(text("Noa: page 42")))
    r = await make(c).handle(Incoming(PARENT, text="What does Noa need?"))
    assert r.text == "Noa: page 42" and r.buttons == []
    assert c.calls[0]["messages"][-1]["content"] == "What does Noa need?"


async def test_answers_carry_no_buttons_in_v1():
    c = ScriptedClient(response(text("a")), response(text("b")))
    g = make(c)
    assert (await g.handle(Incoming(PARENT, text="What does Noa need?"))).buttons == []
    assert (await g.handle(Incoming(PARENT, text="מה ענבר צריכה?"))).buttons == []


async def test_a_stale_button_press_asks_for_a_question_and_does_not_call_claude():
    c = ScriptedClient()
    r = await make(c).handle(Incoming(PARENT, button="tomorrow"))
    assert r.text == ASK_SOMETHING and c.calls == []


async def test_start_and_empty_do_not_call_claude():
    c = ScriptedClient()
    g = make(c)
    start = await g.handle(Incoming(PARENT, text="/start"))
    assert start.buttons == [] and "מחר" in start.text
    assert (await g.handle(Incoming(PARENT, text="  "))).text == ASK_SOMETHING
    assert c.calls == []


async def test_rate_limit_blocks_with_wait_time_and_skips_claude():
    c = ScriptedClient(response(text("1")), response(text("2")))
    g = make(c, limit=2)
    await g.handle(Incoming(PARENT, text="a"))
    await g.handle(Incoming(PARENT, text="b"))
    r = await g.handle(Incoming(PARENT, text="c"))
    assert "limit" in r.text and "60 min" in r.text
    assert len(c.calls) == 2


async def test_followups_share_conversation_per_parent():
    c = ScriptedClient(response(text("A1")), response(text("A2")))
    g = make(c)
    await g.handle(Incoming(PARENT, text="Q1"))
    await g.handle(Incoming(PARENT, text="Q2"))
    assert [m["content"] for m in c.calls[1]["messages"]] == ["Q1", "A1", "Q2"]


async def test_logs_are_content_free(caplog):
    caplog.set_level(logging.INFO, logger="easytop.gateway")
    c = ScriptedClient(response(text("SECRET-ANSWER-TEXT")))
    await make(c).handle(Incoming(PARENT, text="SECRET-QUESTION-TEXT"))
    logged = caplog.text
    assert "answered" in logged and "SECRET" not in logged


async def test_welcome_is_hebrew_first_and_teaches_the_free_language_method():
    from easytop.adapters.gateway import WELCOME

    first_line = WELCOME.splitlines()[0]
    assert any("\u05d0" <= ch <= "\u05ea" for ch in first_line)  # Hebrew leads
    for word in ("שיעורי בית", "ציוד", "מבחנים", "מהלך היום"):  # the four categories
        assert word in WELCOME
    assert "ארבעה" in WELCOME and "שלושה" not in WELCOME
    assert "ציוד בלבד" in WELCOME  # shows asking for one category
    assert "English" in WELCOME  # one short line for English speakers


def test_welcome_spells_webtop_the_way_hebrew_readers_expect():
    from easytop.adapters.gateway import WELCOME

    assert "הוובטופ" in WELCOME and "ווטופ" not in WELCOME.replace("וובטופ", "")


# ---- multi-parent mode: onboarding in front of the agent, reconnect on expiry ----


def multi(client, *, connected: bool, expire_during_answer: bool = False):
    """A gateway with the onboarding flow and a registry; returns (gateway, flow, calls of agent_for)."""
    from easytop.onboarding.crypto import SessionCipher, new_key
    from easytop.onboarding.flow import OnboardingFlow
    from easytop.onboarding.links import ConnectLinks
    from easytop.onboarding.registry import AgentRegistry
    from easytop.onboarding.vault import SessionVault
    from easytop.store.memory import InMemoryStore

    store = InMemoryStore()
    vault = SessionVault(store, SessionCipher(new_key()))
    hooks = {}

    def connector_factory(cookies, on_expired):
        hooks["expire"] = on_expired
        return FakeConnector(NOW.date())

    def agent_factory(connector):
        return Agent(client, FamilyService(connector, clock=lambda: NOW), clock=lambda: NOW)

    registry = AgentRegistry(
        store, vault, connector_factory=connector_factory, agent_factory=agent_factory
    )
    flow = OnboardingFlow(
        store,
        vault,
        registry,
        ConnectLinks(),
        connector_factory=connector_factory,
        connect_url=lambda t: f"https://login.example/?t={t}",
        welcome=WELCOME,
    )
    gw = Gateway(
        Allowlist({PARENT}),
        RateLimiter(30, clock=lambda: NOW),
        registry.agent_for,
        onboarding=flow,
        registry=registry,
    )
    return gw, flow, registry, hooks


async def test_multi_an_unconnected_parent_gets_the_connect_button_and_claude_is_not_called():
    c = ScriptedClient()
    gw, *_ = multi(c, connected=False)
    r = await gw.handle(Incoming(PARENT, text="What does Noa need?"))
    assert r.link is not None and c.calls == []


async def test_multi_a_stranger_is_still_refused_before_onboarding():
    c = ScriptedClient()
    gw, flow, *_ = multi(c, connected=False)
    r = await gw.handle(Incoming("telegram:999", text="hi"))
    assert r.text == PRIVATE_BETA and r.link is None


async def test_multi_a_connected_confirmed_parent_gets_agent_answers():
    c = ScriptedClient(response(text("Noa: page 42")))
    gw, flow, *_ = multi(c, connected=True)
    await flow.on_connected(PARENT, {"webToken": "t"})
    assert (await gw.handle(Incoming(PARENT, text="כן"))).text.startswith(WELCOME)  # then the nudge offer
    r = await gw.handle(Incoming(PARENT, text="What does Noa need?"))
    assert r.text == "Noa: page 42" and r.link is None


async def test_multi_an_answer_that_hits_an_expired_session_becomes_the_reconnect_prompt():
    from easytop.agent.core import Agent as _Agent  # noqa: F401

    c = ScriptedClient(response(text("The connection expired, whatever the model says")))
    gw, flow, registry, hooks = multi(c, connected=True)
    await flow.on_connected(PARENT, {"webToken": "t"})
    await gw.handle(Incoming(PARENT, text="yes"))
    original_ask = Agent.ask

    async def ask_that_expires(self, conv, question):
        await hooks["expire"]()  # the connector found the session dead during this answer
        return await original_ask(self, conv, question)

    Agent.ask = ask_that_expires
    try:
        r = await gw.handle(Incoming(PARENT, text="What does Noa need?"))
    finally:
        Agent.ask = original_ask
    assert r.link is not None and "פג" in r.text
    # and the next message offers the reconnect again, still without calling Claude
    calls_before = len(c.calls)
    r2 = await gw.handle(Incoming(PARENT, text="hello?"))
    assert r2.link is not None and len(c.calls) == calls_before


async def test_phase_4_single_session_mode_is_unchanged():
    c = ScriptedClient(response(text("Noa: page 42")))
    r = await make(c).handle(Incoming(PARENT, text="What does Noa need?"))
    assert r.text == "Noa: page 42" and r.link is None
