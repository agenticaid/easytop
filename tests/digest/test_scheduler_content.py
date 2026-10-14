"""The scheduler sends a content reminder written by code: no model call, no inbox read, no text in the logs."""

import logging
from datetime import time

from easytop.adapters.model import Reply
from easytop.digest.content import render_reminder
from easytop.digest.nudge import NUDGE
from easytop.errors import SessionExpired, UpstreamError
from easytop.store.base import DigestSetting
from tests.digest.test_content_render import BRIEF
from tests.digest.test_scheduler import FakeAgent, FakeFamily, Harness, at


class BriefFamily(FakeFamily):
    """A family service that also answers the content reminder's brief and remembers what it was asked."""

    def __init__(self, result=True, brief=BRIEF, brief_error=None):
        super().__init__(result)
        self.brief, self.brief_error, self.asked = brief, brief_error, []

    async def get_tomorrow_brief(self, kid=None, focus=None):
        self.asked.append((kid, focus))
        if self.brief_error:
            raise self.brief_error
        return self.brief


async def enable(h, content, user="1"):
    p = await h.store.get_or_create_parent("telegram", user)
    await h.store.set_digest(p.id, DigestSetting(True, time(19, 0), content))


async def test_a_due_reminder_carries_what_is_due_tomorrow_and_reads_no_inbox():
    family = BriefFamily()
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "due")
    assert await h.scheduler.tick() == 1
    assert h.sent == [("telegram:1", Reply(text=render_reminder(BRIEF, "due")))]
    assert family.asked == [(None, "due")]  # the focus that never reads the inbox


async def test_an_all_reminder_asks_for_every_part_of_the_day():
    family = BriefFamily()
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "all")
    await h.scheduler.tick()
    assert h.sent == [("telegram:1", Reply(text=render_reminder(BRIEF, "all")))]
    assert family.asked == [(None, "digest")]


async def test_the_plain_level_stays_the_fixed_text_and_never_reads_webtop_for_content():
    family = BriefFamily()
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "none")
    await h.scheduler.tick()
    assert h.sent == [("telegram:1", Reply(text=NUDGE))]
    assert family.asked == []


async def test_no_school_tomorrow_is_still_a_silent_skip_for_a_content_reminder():
    family = BriefFamily(result=False)
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "due")
    assert await h.scheduler.tick() == 0
    assert h.sent == [] and family.asked == []


async def test_when_the_brief_cannot_be_read_the_reminder_still_arrives_as_the_fixed_text():
    family = BriefFamily(brief_error=UpstreamError("school down"))
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "due")
    assert await h.scheduler.tick() == 1
    assert h.sent == [("telegram:1", Reply(text=NUDGE))]


async def test_a_session_that_expired_while_reading_gets_the_usual_reconnect_notice():
    family = BriefFamily(brief_error=SessionExpired("expired"))
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "due")
    assert await h.scheduler.tick() == 0
    assert h.sent[0][1].text == "expired" and h.sent[0][1].link is not None
    assert h.registry.expired_taken == ["telegram:1"]


async def test_the_school_text_of_a_content_reminder_never_reaches_a_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    family = BriefFamily()
    h = Harness({"telegram:1": FakeAgent(family)}, at(19))
    await enable(h, "all")
    await h.scheduler.tick()
    assert "עמ׳ 44" in h.sent[0][1].text  # it was sent...
    for planted in ("עמ׳ 44", "ספר ומחברת", "ענבר", "יחידה 1"):
        assert planted not in caplog.text  # ...and nothing of it was logged
