"""Every answer that used the inbox ends with one fixed line saying how far back it was read (Erez 2026-10-13).
Added by code: zero prompt tokens, always present, in Hebrew when the parent wrote Hebrew."""

from datetime import datetime

import pytest

from easytop.adapters.gateway import Gateway
from easytop.adapters.inbox_note import NOTE_EN, NOTE_HE, inbox_note
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import ScriptedClient, response, text, tool_use

NOW = datetime(2026, 10, 5, 18, 42)
PARENT = "telegram:100"
BRIEF = {"name": "get_tomorrow_brief", "input": {}}


def msgs(**args):
    return {"name": "get_messages", "input": args}


def test_the_brief_reads_the_inbox_so_the_note_is_added_in_the_parents_language():
    assert inbox_note([BRIEF], "he", NOW) == NOTE_HE
    for language in ("en", "ru", "ar", "other"):
        assert inbox_note([BRIEF], language, NOW) == NOTE_EN


def test_the_notes_say_seven_days_in_as_few_words_as_possible():
    assert "7" in NOTE_HE and "7 days" in NOTE_EN
    assert len(NOTE_HE) <= 32 and len(NOTE_EN) <= 32  # Erez 2026-10-13: as short as possible


@pytest.mark.parametrize("tools", [[], [{"name": "list_kids", "input": {}}], [{"name": "get_schedule", "input": {}}]])
def test_no_note_when_the_inbox_was_not_read(tools):
    assert inbox_note(tools, "he", NOW) is None


def test_get_messages_in_the_default_window_or_a_recent_since_gets_the_note():
    assert inbox_note([msgs()], "en", NOW) == NOTE_EN
    assert inbox_note([msgs(since="2026-10-02T00:00:00")], "en", NOW) == NOTE_EN


@pytest.mark.parametrize(
    "args",
    [
        {"since": "2026-09-01T00:00:00"},  # reaches back further than 7 days
        {"since": "2026-09-01T00:00:00Z"},
        {"unread": True},  # unread messages have no limit
        {"since": "not a date"},  # cannot tell: do not claim 7 days
    ],
)
def test_no_note_when_a_call_reached_further_back_than_seven_days(args):
    assert inbox_note([BRIEF, msgs(**args)], "en", NOW) is None


def make(client):
    def agent_for(parent_id):
        svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
        return Agent(client, svc, clock=lambda: NOW)

    return Gateway(
        Allowlist({PARENT}), RateLimiter(30, clock=lambda: NOW), agent_for, clock=lambda: NOW
    )


async def test_the_gateway_appends_the_note_after_an_answer_that_used_the_brief():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief", focus="homework")),
        response(text("Noa: page 42")),
    )
    r = await make(c).handle(Incoming(PARENT, text="מה שיעורי הבית?"))
    assert r.text == f"Noa: page 42\n\n{NOTE_HE}"


async def test_an_english_question_gets_the_english_note():
    c = ScriptedClient(response(tool_use("t1", "get_tomorrow_brief")), response(text("Noa: page 42")))
    r = await make(c).handle(Incoming(PARENT, text="What does Noa need tomorrow?"))
    assert r.text.endswith(f"\n\n{NOTE_EN}")


async def test_an_answer_without_inbox_use_is_returned_as_is():
    c = ScriptedClient(response(text("שלום")))
    r = await make(c).handle(Incoming(PARENT, text="היי"))
    assert r.text == "שלום"
