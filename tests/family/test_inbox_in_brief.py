"""The inbox is material for every answer category (Spec 13): the brief carries the last 7 days of messages,
once for the whole family, for every focus. Nothing is filtered by a date guess."""

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import SessionExpired, UpstreamError
from easytop.family.service import MAX_MESSAGE_CHARS, FamilyService
from tests.family.test_service import NOW


def msg(mid, body, *, subject="Note", sender="Ms. Teacher", days_ago=2, kid_id=None, unread=False):
    return {
        "id": mid,
        "kid_id": kid_id,
        "sender": sender,
        "subject": subject,
        "body": body,
        "sent_days_ago": days_ago,
        "unread": unread,
        "has_attachments": False,
    }


def service(*extra, conn_cls=FakeConnector):
    data = load_synthetic()
    data["messages"].extend(extra)
    conn = conn_cls(NOW.date(), data)
    return conn, FamilyService(conn, clock=lambda: NOW)


UNDATED = msg("u1", "Math homework: do page 50 before the next lesson. Also bring a ruler.")


@pytest.mark.parametrize("focus", [None, "homework", "equipment", "exams", "flow"])
async def test_an_undated_teacher_message_reaches_the_brief_for_every_focus(focus):
    _, svc = service(UNDATED)
    out = await svc.get_tomorrow_brief(focus=focus)
    assert "u1" in [m["id"] for m in out["messages"]]
    assert all("messages" not in k and "messages_mentioning_day" not in k for k in out["kids"])


async def test_the_inbox_is_fetched_once_for_the_whole_family_not_per_kid():
    conn, svc = service(UNDATED)
    await svc.get_tomorrow_brief()
    fetches = [c for c in conn.calls if c[0] == "get_messages"]
    assert fetches == [("get_messages", None)]


async def test_the_window_is_seven_days_back_to_midnight():
    _, svc = service(
        msg("d7", "seven days old", days_ago=7), msg("d8", "eight days old", days_ago=8)
    )
    ids = [m["id"] for m in (await svc.get_tomorrow_brief())["messages"]]
    assert "d7" in ids and "d8" not in ids


async def test_messages_that_name_the_day_come_first_then_newest():
    _, svc = service(
        msg("new", "no date here at all", days_ago=0),
        msg("tue", "Bring the signed form on Tuesday", days_ago=3),
    )
    ids = [m["id"] for m in (await svc.get_tomorrow_brief())["messages"]]
    assert ids.index("tue") < ids.index("new")  # Tuesday is the brief's day; newest otherwise


async def test_identical_messages_collapse_to_the_newest_one():
    _, svc = service(
        msg("a1", "Reminder:  bring   your PE kit", subject="PE", days_ago=3),
        msg("a2", "reminder: bring your PE kit", subject="PE", days_ago=1),
        msg("b1", "reminder: bring your PE kit", subject="PE", sender="Someone else", days_ago=2),
    )
    ids = [m["id"] for m in (await svc.get_tomorrow_brief())["messages"]]
    assert "a2" in ids and "a1" not in ids  # same sender, subject and text: newest kept
    assert "b1" in ids  # another sender is a different fact


async def test_a_date_at_the_end_of_a_long_message_survives():
    body = "Dear parents. " + "General news. " * 80 + "The math test is on 15.10."
    assert 600 < len(body) <= MAX_MESSAGE_CHARS
    _, svc = service(msg("long", body))
    got = next(m for m in (await svc.get_tomorrow_brief())["messages"] if m["id"] == "long")
    assert got["text"].endswith("15.10.") and "truncated" not in got


async def test_a_failing_inbox_never_blocks_the_kids():
    class NoInbox(FakeConnector):
        async def get_messages(self, kid, since, unread):
            raise UpstreamError("inbox down")

    _, svc = service(conn_cls=NoInbox)
    out = await svc.get_tomorrow_brief()
    assert out["messages"] == [] and out["messages_error"] == "unavailable"
    assert all(k["ok"] for k in out["kids"]) and out["session_expired"] is False


async def test_an_expired_session_on_the_inbox_is_flagged():
    class Expired(FakeConnector):
        async def get_messages(self, kid, since, unread):
            raise SessionExpired("gone")

    _, svc = service(conn_cls=Expired)
    out = await svc.get_tomorrow_brief()
    assert out["messages_error"] == "session_expired" and out["session_expired"] is True


async def test_a_since_with_a_timezone_does_not_crash_get_messages():
    _, svc = service(msg("old2", "old read", days_ago=20))
    out = await svc.get_messages(since="2026-09-01T00:00:00Z")
    assert "old2" in [m["id"] for m in out["messages"]]


async def test_get_messages_defaults_to_the_last_seven_days_but_unread_has_no_limit():
    _, svc = service(
        msg("old", "old but unread", days_ago=20, unread=True), msg("old2", "old read", days_ago=20)
    )
    default = [m["id"] for m in (await svc.get_messages())["messages"]]
    assert "old" not in default and "old2" not in default
    unread = [m["id"] for m in (await svc.get_messages(unread=True))["messages"]]
    assert "old" in unread
    older = [m["id"] for m in (await svc.get_messages(since="2026-09-01T00:00:00"))["messages"]]
    assert "old2" in older
