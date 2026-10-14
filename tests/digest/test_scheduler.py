import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from easytop.adapters.model import LinkButton, Reply
from easytop.digest.nudge import NUDGE, NudgeScheduler
from easytop.errors import SessionExpired, UpstreamError
from easytop.store.base import DigestSetting
from easytop.store.memory import InMemoryStore

TZ = ZoneInfo("Asia/Jerusalem")


def at(hour, minute=0, second=10):
    return datetime(2026, 10, 11, hour, minute, second, tzinfo=TZ)


class FakeFamily:
    def __init__(self, result=True, error=None):
        self.result, self.error = result, error

    async def school_tomorrow(self):
        if self.error:
            raise self.error
        return self.result


class FakeAgent:
    def __init__(self, family):
        self.family = family


class FakeRegistry:
    def __init__(self, agents):
        self.agents = agents  # parent_id -> FakeAgent | None
        self.expired_taken: list[str] = []

    async def agent_for(self, parent_id):
        return self.agents.get(parent_id)

    async def take_expired(self, parent_id):
        self.expired_taken.append(parent_id)
        return True


class Harness:
    def __init__(self, agents, now):
        self.store = InMemoryStore()
        self.registry = FakeRegistry(agents)
        self.sent: list[tuple[str, Reply]] = []
        self.now = now

        async def notify(pid, reply):
            self.sent.append((pid, reply))

        async def expired_reply(pid):
            return Reply(text="expired", link=LinkButton("reconnect", "https://x/?t=1"))

        self.scheduler = NudgeScheduler(
            self.store,
            self.registry,
            notify,
            expired_reply=expired_reply,
            clock=lambda: self.now,
        )

    async def enable(self, user, hour=19, minute=0):
        p = await self.store.get_or_create_parent("telegram", user)
        await self.store.set_digest(p.id, DigestSetting(True, time(hour, minute)))


async def test_a_parent_due_now_with_school_tomorrow_gets_the_plain_nudge():
    h = Harness({"telegram:1": FakeAgent(FakeFamily(True))}, at(19))
    await h.enable("1")
    assert await h.scheduler.tick() == 1
    assert h.sent == [("telegram:1", Reply(text=NUDGE))]  # nothing about the kids, no buttons


async def test_the_same_minute_is_never_sent_twice():
    h = Harness({"telegram:1": FakeAgent(FakeFamily(True))}, at(19))
    await h.enable("1")
    assert await h.scheduler.tick() == 1
    h.now = at(19, 0, 50)
    assert await h.scheduler.tick() == 0
    assert len(h.sent) == 1


async def test_it_is_sent_again_the_next_day():
    h = Harness({"telegram:1": FakeAgent(FakeFamily(True))}, at(19))
    await h.enable("1")
    await h.scheduler.tick()
    h.now = datetime(2026, 10, 12, 19, 0, 10, tzinfo=TZ)
    assert await h.scheduler.tick() == 1


async def test_no_school_tomorrow_is_a_silent_skip():
    h = Harness({"telegram:1": FakeAgent(FakeFamily(False))}, at(19))
    await h.enable("1")
    assert await h.scheduler.tick() == 0
    assert h.sent == []


async def test_only_parents_due_at_this_minute_are_considered():
    h = Harness(
        {"telegram:1": FakeAgent(FakeFamily(True)), "telegram:2": FakeAgent(FakeFamily(True))},
        at(19),
    )
    await h.enable("1", 19, 0)
    await h.enable("2", 20, 0)
    await h.scheduler.tick()
    assert [pid for pid, _ in h.sent] == ["telegram:1"]


async def test_the_clock_is_read_in_israel_time_whatever_the_server_zone():
    utc = datetime(2026, 10, 11, 16, 0, 10, tzinfo=ZoneInfo("UTC"))  # 19:00 in Israel (UTC+3)
    h = Harness({"telegram:1": FakeAgent(FakeFamily(True))}, utc)
    await h.enable("1", 19, 0)
    assert await h.scheduler.tick() == 1


async def test_a_parent_without_a_session_gets_the_reconnect_notice_instead():
    h = Harness({"telegram:1": None}, at(19))
    await h.enable("1")
    assert await h.scheduler.tick() == 0
    ((pid, reply),) = h.sent
    assert pid == "telegram:1" and reply.link is not None and reply.text == "expired"


async def test_a_session_that_dies_during_the_check_also_gets_the_reconnect_notice():
    h = Harness({"telegram:1": FakeAgent(FakeFamily(error=SessionExpired()))}, at(19))
    await h.enable("1")
    await h.scheduler.tick()
    ((_, reply),) = h.sent
    assert reply.link is not None
    assert h.registry.expired_taken == [
        "telegram:1"
    ]  # the flag is consumed, not left for the next answer


async def test_one_parents_failure_is_logged_by_type_and_the_others_still_get_theirs(caplog):
    secret = "https://api.telegram.org/bot123:ABC/x"
    h = Harness(
        {
            "telegram:1": FakeAgent(FakeFamily(error=UpstreamError(secret))),
            "telegram:2": FakeAgent(FakeFamily(True)),
        },
        at(19),
    )
    await h.enable("1")
    await h.enable("2")
    with caplog.at_level(logging.INFO, logger="easytop.digest"):
        assert await h.scheduler.tick() == 1
    assert [pid for pid, _ in h.sent] == ["telegram:2"]
    text = caplog.text
    assert "UpstreamError" in text and "ABC" not in text and "sent=1 skipped=0 failed=1" in text


async def test_run_ticks_until_cancelled_and_survives_a_failing_tick():
    h = Harness({}, at(19))
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        return 0

    h.scheduler.tick = tick

    async def no_wait(_seconds):
        await asyncio.sleep(0)

    h.scheduler._sleep = no_wait
    task = asyncio.create_task(h.scheduler.run())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2
