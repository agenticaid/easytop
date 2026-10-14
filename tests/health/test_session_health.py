"""The hourly session health check: who has a stored Webtop session, is it still alive, and for how long it lived.
Content-free: the log holds the parent id, yes/no and an age in hours, never a cookie or any school data."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from easytop.errors import UpstreamError
from easytop.health import SessionHealth
from easytop.onboarding.registry import store_parent
from easytop.store.memory import InMemoryStore


class FakeRegistry:
    """`answers[pid]` is the list of statuses session_status returns, one per call (True / False / None / an error)."""

    def __init__(self, answers):
        self.answers, self.dropped, self.asked = answers, [], []

    async def session_status(self, pid):
        self.asked.append(pid)
        result = self.answers[pid].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def drop_session(self, pid):
        self.dropped.append(pid)


async def make(answers, hours_later=26):
    store = InMemoryStore()
    for pid in answers:
        sp = await store_parent(store, pid)
        await store.save_session(sp.id, b"ciphertext")
    registry = FakeRegistry(answers)
    now = datetime.now(UTC) + timedelta(hours=hours_later)
    health = SessionHealth(store, registry, clock=lambda: now)

    async def no_wait(seconds):
        pass

    health._sleep = no_wait
    return health, registry


def health_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "easytop.health"]


async def test_a_live_session_is_logged_with_its_age_and_kept(caplog):
    caplog.set_level(logging.INFO)
    health, registry = await make({"telegram:1": [True]})
    assert await health.tick() == {"ok": 1, "dead": 0, "unknown": 0}
    lines = health_lines(caplog)
    assert any(
        line.startswith("session check parent=telegram:1 valid=True age_h=26.0") for line in lines
    )
    assert registry.dropped == []


async def test_a_session_that_answers_dead_twice_is_dropped_and_its_lifetime_logged(caplog):
    caplog.set_level(logging.INFO)
    health, registry = await make({"telegram:1": [False, False]})
    assert await health.tick() == {"ok": 0, "dead": 1, "unknown": 0}
    assert registry.dropped == ["telegram:1"]
    assert any(
        line.startswith("session dead parent=telegram:1 age_h=26.0")
        for line in health_lines(caplog)
    )


async def test_one_bad_answer_followed_by_a_good_one_keeps_the_session():
    health, registry = await make({"telegram:1": [False, True]})
    assert await health.tick() == {"ok": 1, "dead": 0, "unknown": 0}
    assert registry.dropped == []  # a glitch must never log a parent out


async def test_an_unknown_answer_never_drops_a_session(caplog):
    caplog.set_level(logging.INFO)
    health, registry = await make(
        {"telegram:1": [None], "telegram:2": [UpstreamError("503 SECRET")]}
    )
    assert await health.tick() == {"ok": 0, "dead": 0, "unknown": 2}
    assert registry.dropped == []
    assert "SECRET" not in "\n".join(health_lines(caplog))  # an error text never reaches the log


async def test_one_parent_failing_does_not_stop_the_others():
    health, registry = await make({"telegram:1": [RuntimeError("boom")], "telegram:2": [True]})
    assert await health.tick() == {"ok": 1, "dead": 0, "unknown": 1}
    assert sorted(registry.asked) == ["telegram:1", "telegram:2"]


async def test_nobody_connected_means_nothing_to_do_and_nothing_logged(caplog):
    caplog.set_level(logging.INFO)
    health, _ = await make({})
    assert await health.tick() == {"ok": 0, "dead": 0, "unknown": 0}
    assert health_lines(caplog) == []


async def test_run_checks_after_a_short_delay_then_every_interval_and_survives_a_failed_round():
    store = InMemoryStore()
    health = SessionHealth(store, FakeRegistry({}), first_after=120, every=3600)
    sleeps, ticks = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    async def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("database briefly down")
        return {}

    health._sleep, health.tick = fake_sleep, tick
    try:
        await health.run()
    except asyncio.CancelledError:
        pass
    assert sleeps == [120, 3600, 3600, 3600] and len(ticks) == 3
