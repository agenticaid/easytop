"""What the content reminder reads (Erez 2026-10-13): code asks the family service for the day's parts WITHOUT the
inbox. Reading messages marks them read in Webtop; a timer must not do that behind the parent's back."""

from datetime import datetime

from easytop.agent.tools import dispatch
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService

NOW = datetime(2026, 10, 5, 18, 42)  # tomorrow, Tuesday, is a school day


def svc():
    conn = FakeConnector(NOW.date())
    return conn, FamilyService(conn, clock=lambda: NOW)


async def test_the_due_focus_has_homework_and_exams_only_and_never_reads_the_inbox():
    conn, s = svc()
    brief = await s.get_tomorrow_brief(None, "due")
    assert "messages" not in brief and brief["focus"] == "due"
    for kid in brief["kids"]:
        assert "homework_due" in kid and "upcoming_exams" in kid
        assert not {"lessons", "changes", "homework_upcoming", "calendar"} & set(kid)
    assert not [c for c in conn.calls if c[0] == "get_messages"]


async def test_the_digest_focus_has_every_part_of_the_day_but_still_not_the_inbox():
    conn, s = svc()
    brief = await s.get_tomorrow_brief(None, "digest")
    assert "messages" not in brief
    for kid in brief["kids"]:
        assert {"lessons", "changes", "homework_due", "homework_upcoming", "upcoming_exams"} <= set(kid)
    assert not [c for c in conn.calls if c[0] == "get_messages"]


async def test_the_model_cannot_ask_for_these_focuses():
    _, s = svc()
    for focus in ("due", "digest"):
        out = await dispatch(s, "get_tomorrow_brief", {"focus": focus})
        assert "error" in out
