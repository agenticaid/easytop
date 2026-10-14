"""A daily cap on questions per parent (Erez 2026-10-13): at most N questions that reach the model per calendar
day in Israel time, kept in memory, admins exempt, off unless EASYTOP_DAILY_QUESTION_CAP is set."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import anthropic
import pytest

from easytop import bot
from easytop.access.invites import InviteService
from easytop.adapters.gateway import ASK_SOMETHING, Gateway, daily_cap_text
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, DailyCap, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from easytop.store.memory import InMemoryStore
from tests.agent.fakes import ScriptedClient, response, text

# 2026-10-13 18:00 UTC is 21:00 in Israel (UTC+3 in October): the same Israel day for a few hours
NOW = datetime(2026, 10, 13, 18, 0, tzinfo=UTC)
PARENT = "telegram:100"
ADMIN = "telegram:1"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


# --- the counter on its own ---------------------------------------------------------------------------------


def test_the_nth_question_passes_and_the_next_is_refused():
    cap = DailyCap(2, clock=Clock())
    assert cap.take("p") and cap.take("p")
    assert not cap.take("p")


def test_a_refused_question_is_not_counted_so_the_refusal_stays_a_refusal():
    cap = DailyCap(1, clock=Clock())
    assert cap.take("p")
    assert not cap.take("p") and not cap.take("p")


def test_each_parent_has_their_own_count():
    cap = DailyCap(1, clock=Clock())
    assert cap.take("a") and cap.take("b")
    assert not cap.take("a") and not cap.take("b")


def test_the_count_resets_at_midnight_in_israel_not_at_midnight_utc():
    clock = Clock(datetime(2026, 10, 13, 20, 30, tzinfo=UTC))  # 23:30 Israel
    cap = DailyCap(1, clock=clock)
    assert cap.take("p") and not cap.take("p")
    clock.now = datetime(
        2026, 10, 13, 21, 5, tzinfo=UTC
    )  # 00:05 Israel the next day, still 13.10 in UTC
    assert cap.take("p")


def test_a_late_evening_in_utc_is_still_the_same_israel_day_until_israel_midnight():
    clock = Clock(datetime(2026, 10, 13, 5, 0, tzinfo=UTC))  # 08:00 Israel
    cap = DailyCap(1, clock=clock)
    assert cap.take("p")
    clock.now = datetime(2026, 10, 13, 20, 59, tzinfo=UTC)  # 23:59 Israel, same day
    assert not cap.take("p")


def test_winter_time_moves_the_israel_midnight_by_an_hour():
    clock = Clock(datetime(2026, 12, 15, 21, 30, tzinfo=UTC))  # 23:30 Israel (UTC+2)
    cap = DailyCap(1, clock=clock)
    assert cap.take("p")
    clock.now = datetime(2026, 12, 15, 22, 30, tzinfo=UTC)  # 00:30 Israel the next day
    assert cap.take("p")


def test_a_refund_gives_the_question_back():
    cap = DailyCap(1, clock=Clock())
    assert cap.take("p")
    cap.refund("p")
    assert cap.take("p")


def test_a_refund_never_goes_below_zero():
    cap = DailyCap(1, clock=Clock())
    cap.refund("p")  # nothing taken yet
    cap.refund("p")
    assert cap.take("p") and not cap.take("p")


def test_old_days_do_not_pile_up_in_memory():
    clock = Clock()
    cap = DailyCap(5, clock=clock)
    for day in range(30):
        clock.now = NOW + timedelta(days=day)
        cap.take(f"p{day}")
    assert len(cap._counts) <= 1


# --- the setting ---------------------------------------------------------------------------------------------


def test_unset_or_zero_means_no_cap(monkeypatch):
    monkeypatch.delenv("EASYTOP_DAILY_QUESTION_CAP", raising=False)
    assert bot.daily_cap_from_env() is None
    monkeypatch.setenv("EASYTOP_DAILY_QUESTION_CAP", "0")
    assert bot.daily_cap_from_env() is None
    monkeypatch.setenv("EASYTOP_DAILY_QUESTION_CAP", " ")
    assert bot.daily_cap_from_env() is None


def test_a_number_sets_the_cap(monkeypatch):
    monkeypatch.setenv("EASYTOP_DAILY_QUESTION_CAP", "5")
    cap = bot.daily_cap_from_env()
    assert cap is not None and cap.limit == 5


@pytest.mark.parametrize("bad", ["five", "-1", "2.5"])
def test_a_value_that_is_not_a_whole_number_stops_the_bot_instead_of_running_uncapped(
    monkeypatch, bad
):
    monkeypatch.setenv("EASYTOP_DAILY_QUESTION_CAP", bad)
    with pytest.raises(bot.BotConfigError, match="EASYTOP_DAILY_QUESTION_CAP"):
        bot.daily_cap_from_env()


def test_the_multi_parent_gateway_takes_the_cap(monkeypatch):
    import inspect

    assert "daily_cap" in inspect.signature(bot.build_multi_gateway).parameters


# --- the gateway ---------------------------------------------------------------------------------------------


def make(client, *, cap=2, admin_ids=(), limit=30, agent_none=False):
    def agent_for(parent_id):
        if agent_none:
            return None
        return Agent(
            client,
            FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW),
            clock=lambda: NOW,
        )

    allow = Allowlist({PARENT, ADMIN, *admin_ids})
    invites = InviteService(InMemoryStore(), Allowlist({ADMIN}), link=lambda payload: None)
    return Gateway(
        allow,
        RateLimiter(limit, clock=lambda: NOW),
        agent_for,
        invites=invites,
        daily_cap=DailyCap(cap, clock=Clock()) if cap else None,
    )


async def ask(g, who=PARENT, what="What does Noa need?"):
    return await g.handle(Incoming(who, text=what))


async def test_the_question_after_the_cap_gets_the_refusal_and_never_reaches_the_model():
    c = ScriptedClient(response(text("one")), response(text("two")))
    g = make(c, cap=2)
    assert (await ask(g)).text == "one"
    assert (await ask(g)).text == "two"
    r = await ask(g)
    assert r.text == daily_cap_text(2)
    assert len(c.calls) == 2


def test_the_refusal_is_hebrew_first_then_english_and_names_the_number_and_midnight():
    t = daily_cap_text(5)
    he, en = t.split("\n\n")
    assert "5" in he and "חצות" in he
    assert "5 questions" in en and "midnight" in en and "Israel" in en


async def test_without_a_cap_nothing_is_limited():
    c = ScriptedClient(*[response(text(str(i))) for i in range(8)])
    g = make(c, cap=0)
    for i in range(8):
        assert (await ask(g)).text == str(i)


async def test_an_admin_is_never_capped():
    c = ScriptedClient(*[response(text(str(i))) for i in range(4)])
    g = make(c, cap=1)
    for i in range(4):
        assert (await ask(g, ADMIN)).text == str(i)


async def test_a_guest_is_capped_even_though_they_are_let_in():
    c = ScriptedClient(response(text("ok")))
    g = make(c, cap=1)
    assert (await ask(g, PARENT)).text == "ok"
    assert (await ask(g, PARENT)).text == daily_cap_text(1)


async def test_commands_and_empty_messages_do_not_count():
    c = ScriptedClient(response(text("the only answer")))
    g = make(c, cap=1)
    await ask(g, what="/start")
    await ask(g, what="   ")
    await g.handle(Incoming(PARENT, button="tomorrow"))
    assert (await ask(g)).text == "the only answer"


async def test_an_answer_that_failed_on_our_side_is_not_counted():
    err = anthropic.APIConnectionError(request=NS(method="POST", url="https://x"))
    c = ScriptedClient(err, err, response(text("fine now")))
    g = make(c, cap=1)
    await ask(g)  # the model call fails (both attempts)
    assert (await ask(g)).text == "fine now"


async def test_a_question_without_a_session_is_not_counted():
    c = ScriptedClient(response(text("answer")))
    g = make(c, cap=1, agent_none=True)
    assert (await ask(g)).text == ASK_SOMETHING
    assert (await ask(g)).text == ASK_SOMETHING  # never the cap text: nothing was spent


async def test_the_cap_is_per_parent():
    c = ScriptedClient(response(text("a")), response(text("b")))
    g = make(c, cap=1, admin_ids=("telegram:200",))
    assert (await ask(g, PARENT)).text == "a"
    assert (await ask(g, "telegram:200")).text == "b"


async def test_the_hourly_limit_still_applies_first():
    c = ScriptedClient(response(text("a")))
    g = make(c, cap=5, limit=1)
    await ask(g)
    r = await ask(g)
    assert "hourly" in r.text


async def test_a_refusal_is_logged_without_any_text(caplog):
    caplog.set_level(logging.INFO)
    g = make(ScriptedClient(response(text("a"))), cap=1)
    await ask(g, what="a secret school question")
    await ask(g, what="a secret school question")
    lines = [r.getMessage() for r in caplog.records if "daily cap" in r.getMessage()]
    assert lines == [f"daily cap reached parent={PARENT} cap=1"]
    assert not any("secret" in r.getMessage() for r in caplog.records)


async def test_a_refusal_is_counted_as_a_rate_limited_question_for_analytics(caplog):
    caplog.set_level(logging.INFO)
    g = make(ScriptedClient(response(text("a"))), cap=1)
    await ask(g)
    await ask(g)
    outcomes = [
        r.analytics["outcome"]
        for r in caplog.records
        if hasattr(r, "analytics") and r.analytics["event"] == "question"
    ]
    assert outcomes == ["answered_no_data", "rate_limited"]
