"""The analytics events each part of the bot emits (Spec 07, usage analytics). Strict schema (tests/conftest.py)."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import anthropic
import pytest

from easytop.adapters.gateway import Gateway
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.errors import UpstreamError
from easytop.family.service import FamilyService
from tests.adapters.test_gateway import multi
from tests.agent.fakes import ScriptedClient, response, text, tool_use
from tests.onboarding.test_flow import Harness

NOW = datetime(2026, 10, 5, 18, 42)
PARENT = "telegram:100"


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.INFO)

    def get(name=None):
        out = [r.analytics for r in caplog.records if hasattr(r, "analytics")]
        return [e for e in out if name is None or e["event"] == name]

    return get


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gateway(client, *, limit=30, mono=None):
    def agent_for(parent_id):
        return Agent(
            client, FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW), clock=lambda: NOW
        )

    return Gateway(
        Allowlist({PARENT}),
        RateLimiter(limit, clock=lambda: NOW),
        agent_for,
        monotonic=mono or Mono(),
    )


def brief_then_answer(**inp):
    return [
        response(
            tool_use("t1", "get_tomorrow_brief", **inp), in_tok=900, out_tok=40, cache_read=3000
        ),
        response(text("answer"), in_tok=1200, out_tok=300, cache_read=3000, cache_write=10),
    ]


# ---- gateway: the question event ----


async def test_an_answered_question_is_one_event_with_labels_tokens_cost_and_time(events):
    mono = Mono()
    client = ScriptedClient(*brief_then_answer(focus="equipment"))
    original = client._create

    async def slow(**kw):  # each model call "takes" 1.5 s
        mono.t += 1.5
        return await original(**kw)

    client.messages.create = slow
    await gateway(client, mono=mono).handle(Incoming(PARENT, text="מה צריך להביא מחר?"))
    (q,) = events("question")
    assert q["outcome"] == "answered" and q["category"] == "tomorrow" and q["focus"] == "equipment"
    assert q["kids_scope"] == "all" and q["language"] == "he" and q["follow_up"] is False
    assert q["model"] == "claude-sonnet-5-5" and q["answer_ms"] == 3000
    assert q["tool_count"] == 1 and q["tools"] == ["get_tomorrow_brief"]
    assert (q["input_tokens"], q["output_tokens"]) == (2100, 340)
    assert (q["cache_read_tokens"], q["cache_write_tokens"]) == (6000, 10)
    assert q["cost_usd"] > 0 and q["parent_ref"].startswith("p_")


async def test_a_question_within_ten_minutes_of_the_last_is_a_follow_up(events):
    mono = Mono()
    g = gateway(
        ScriptedClient(response(text("a")), response(text("b")), response(text("c"))), mono=mono
    )
    await g.handle(Incoming(PARENT, text="hi"))
    mono.t += 9 * 60
    await g.handle(Incoming(PARENT, text="and Ido?"))
    mono.t += 11 * 60
    await g.handle(Incoming(PARENT, text="what about tomorrow?"))
    assert [q["follow_up"] for q in events("question")] == [False, True, False]


async def test_no_tool_answer_is_answered_no_data(events):
    await gateway(ScriptedClient(response(text("I only do school info")))).handle(
        Incoming(PARENT, text="write me a poem")
    )
    (q,) = events("question")
    assert q["outcome"] == "answered_no_data" and q["category"] == "none" and q["language"] == "en"


async def test_a_model_failure_is_an_error_outcome_with_its_type(events):
    err = anthropic.APIConnectionError(request=NS(method="POST", url="https://x"))
    await gateway(ScriptedClient(err, err)).handle(Incoming(PARENT, text="hi"))
    (q,) = events("question")
    assert q["outcome"] == "error" and q["error_type"] == "APIConnectionError"


async def test_rate_limited_questions_are_counted_without_a_model_call(events):
    g = gateway(ScriptedClient(response(text("1"))), limit=1)
    await g.handle(Incoming(PARENT, text="a"))
    await g.handle(Incoming(PARENT, text="b"))
    assert [q["outcome"] for q in events("question")] == ["answered_no_data", "rate_limited"]
    assert "model" not in events("question")[1]


async def test_strangers_and_bad_invites_are_access_denied(events):
    from easytop.access.invites import InviteService
    from easytop.store.memory import InMemoryStore

    allow = Allowlist({PARENT})
    invites = InviteService(InMemoryStore(), allow, link=lambda p: f"https://t.me/b?start={p}")
    g = Gateway(allow, RateLimiter(30), lambda pid: None, invites=invites)
    await g.handle(Incoming("telegram:999", text="hi"))
    await g.handle(Incoming("telegram:998", text="/start inv_nope"))
    assert [e["reason"] for e in events("access_denied")] == ["not_allowed", "bad_invite"]


async def test_an_answer_that_meets_an_expired_session_is_counted_as_such(events):
    c = ScriptedClient(response(text("x")))
    gw, flow, registry, hooks = multi(c, connected=True)
    await flow.on_connected(PARENT, {"webToken": "t"})
    await gw.handle(Incoming(PARENT, text="yes"))
    original_ask = Agent.ask

    async def ask_that_expires(self, conv, question):
        await hooks["expire"]()
        return await original_ask(self, conv, question)

    Agent.ask = ask_that_expires
    try:
        await gw.handle(Incoming(PARENT, text="What does Noa need?"))
    finally:
        Agent.ask = original_ask
    assert events("question")[-1]["outcome"] == "session_expired"
    assert len(events("session_expired")) == 1


# ---- onboarding, invites ----


async def test_onboarding_funnel_events(events):
    h = Harness()
    await h.say("telegram:1", "/start")
    await h.connect("telegram:1")
    await h.say("telegram:1", "כן")
    await h.say("telegram:1", "19:30")
    await h.say("telegram:1", "תזכורת כבוי")
    assert [e["reason"] for e in events("connect_offered")] == ["first"]
    (k,) = events("kids_confirmed")
    assert (k["kid_count"], k["school_count"], k["reconnect"]) == (2, 2, False)
    assert [(e["state"], e.get("hour")) for e in events("nudge_setting")] == [
        ("on", 19),
        ("off", None),
    ]


async def test_rejected_kids_declined_nudge_and_failed_lookup(events):
    h = Harness()
    await h.connect("telegram:1")
    await h.say("telegram:1", "לא")
    assert events("kids_rejected")[0]["kid_count"] == 2
    assert events("connect_offered")[-1]["reason"] == "wrong_account"
    await h.connect("telegram:1")
    await h.say("telegram:1", "yes")
    await h.say("telegram:1", "no")
    assert events("nudge_setting")[-1]["state"] == "declined"
    h2 = Harness()
    h2.connector_error = UpstreamError("down")
    await h2.connect("telegram:2")
    assert events("error")[-1] == {
        **events("error")[-1],
        "where": "onboarding",
        "error_type": "UpstreamError",
    }


async def test_reconnecting_with_the_same_kids_counts_as_a_reconnect(events):
    h = Harness()
    await h.connect("telegram:1")
    await h.say("telegram:1", "yes")
    await h.connect("telegram:1")
    assert [e["reconnect"] for e in events("kids_confirmed")] == [False, True]


async def test_invites_created_and_redeemed(events):
    from easytop.access.invites import InviteService
    from easytop.store.memory import InMemoryStore

    allow = Allowlist({"telegram:1"})
    svc = InviteService(InMemoryStore(), allow, link=lambda p: f"https://t.me/b?start={p}")
    inv = await svc.create("telegram:1")
    await svc.redeem(inv.url.split("inv_")[1], "telegram:2")
    assert events("invite_created")[0]["ttl_hours"] == 24
    assert len(events("invite_redeemed")) == 1
    assert events("invite_created")[0]["parent_ref"] != events("invite_redeemed")[0]["parent_ref"]


# ---- nudge, health ----


async def test_nudge_results(events):
    from datetime import time

    from easytop.digest.nudge import NudgeScheduler
    from easytop.onboarding.registry import store_parent
    from easytop.store.base import DigestSetting
    from easytop.store.memory import InMemoryStore
    from tests.digest.test_scheduler import FakeAgent, FakeFamily, FakeRegistry

    store = InMemoryStore()
    agents = {
        "telegram:1": FakeAgent(FakeFamily(True)),
        "telegram:2": FakeAgent(FakeFamily(False)),
        "telegram:3": None,
        "telegram:4": FakeAgent(FakeFamily(error=RuntimeError("x"))),
    }
    for pid in agents:
        sp = await store_parent(store, pid)
        await store.set_digest(sp.id, DigestSetting(True, time(19, 0)))

    async def notify(pid, reply):
        pass

    async def expired_reply(pid):
        return None

    now = datetime(2026, 10, 11, 16, 0, 5, tzinfo=UTC)  # 19:00 in Israel
    s = NudgeScheduler(
        store, FakeRegistry(agents), notify, expired_reply=expired_reply, clock=lambda: now
    )
    await s.tick()
    assert sorted(e["result"] for e in events("nudge")) == [
        "expired",
        "failed",
        "no_school",
        "sent",
    ]
    assert events("error")[0]["where"] == "nudge"


async def test_session_checks_carry_status_and_age(events):
    from tests.health.test_session_health import make

    health, _ = await make(
        {"telegram:1": [True], "telegram:2": [False, False], "telegram:3": [None]}, hours_later=26
    )
    await health.tick()
    got = sorted((e["status"], round(e["age_h"])) for e in events("session_check"))
    assert got == [("dead", 26), ("ok", 26), ("unknown", 26)]


def test_no_event_without_its_schema_entry():
    """A guard for this file: the events asserted above all exist in the schema."""
    from easytop.analytics import SCHEMA

    for name in (
        "question",
        "access_denied",
        "connect_offered",
        "kids_confirmed",
        "kids_rejected",
        "nudge_setting",
        "invite_created",
        "invite_redeemed",
        "nudge",
        "session_check",
        "session_expired",
        "error",
    ):
        assert name in SCHEMA
    assert timedelta  # imported for readers who extend the follow-up tests


def test_the_bot_configures_analytics_from_its_environment(monkeypatch):
    from easytop import analytics, bot

    monkeypatch.setenv("EASYTOP_ENV", "dev")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "k" * 44)
    first = bot.configure_analytics()
    again = bot.configure_analytics()
    assert first.env == "dev" and first.strict is False and first.synthetic is False
    assert (
        first.ref("telegram:1") == again.ref("telegram:1") == analytics.current().ref("telegram:1")
    )


async def test_telegram_errors_are_counted_by_type(events):
    from easytop.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(gateway(ScriptedClient()))
    await adapter.on_error(
        None, NS(error=TimeoutError("https://api.telegram.org/bot123:SECRET/getUpdates"))
    )
    (e,) = events("error")
    assert (e["where"], e["error_type"]) == ("telegram", "TimeoutError")
