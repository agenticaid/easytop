"""Homework a message gives for a subject, without a due date, is past once a lesson of that subject was held after
the message (Erez 2026-10-13). A model comparing dates in a table missed it on a real inbox, so code does the matching:
each message carries `lessons_held_since`, the children and the subjects it NAMES whose lesson came later."""

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import UpstreamError
from easytop.family.service import FamilyService
from tests.family.test_service import NOW

# NOW is Monday 2026-10-05 18:42
HELD = [
    {"kid_id": "k1", "days_ago": 0, "hour": 2, "subject": "Math"},  # Monday, after Sunday's message
    {"kid_id": "k1", "days_ago": 3, "hour": 1, "subject": "Science"},  # Friday, BEFORE the message
    {"kid_id": "k1", "days_ago": 1, "hour": 1, "subject": "Art"},  # Sunday, the day of the message
    {"kid_id": "k2", "days_ago": 0, "hour": 1, "subject": "Science"},
]


def message(mid, body, *, days_ago=1):
    return {
        "id": mid,
        "kid_id": None,
        "sender": "Ms. Dana",
        "subject": "Note",
        "body": body,
        "sent_days_ago": days_ago,
        "unread": False,
        "has_attachments": False,
    }


def service(*messages, conn_cls=FakeConnector):
    data = load_synthetic()
    data["held_lessons"] = HELD
    data["messages"].extend(messages)
    return FamilyService(conn_cls(NOW.date(), data), clock=lambda: NOW)


async def annotation(svc, mid, focus=None):
    out = await svc.get_tomorrow_brief(focus=focus)
    return next(m for m in out["messages"] if m["id"] == mid).get("lessons_held_since")


async def test_a_message_naming_a_subject_whose_lesson_came_later_says_so_per_child():
    svc = service(message("a", "Math homework: page 50 before the next lesson."))
    assert await annotation(svc, "a") == {"Noa": ["Math"]}  # Ido had no Math lesson since


async def test_every_child_whose_lesson_came_later_is_listed():
    svc = service(message("a", "Science project: bring the materials next week."))
    assert await annotation(svc, "a") == {"Ido": ["Science"]}  # Noa's Science was before the message


async def test_a_lesson_on_the_day_of_the_message_is_not_later():
    svc = service(message("a", "Art: bring clay."))
    assert await annotation(svc, "a") is None


async def test_a_message_that_names_no_subject_is_not_annotated():
    assert await annotation(service(message("a", "Please send the signed form.")), "a") is None


async def test_a_date_written_in_the_message_keeps_it_alive_so_it_is_not_annotated():
    svc = service(message("a", "Math homework: page 50, to be submitted by 15.10."))
    assert await annotation(svc, "a") is None


async def test_a_date_already_passed_does_not_keep_it_alive():
    svc = service(message("a", "Math homework: page 50, submit by 2.10."))
    assert await annotation(svc, "a") == {"Noa": ["Math"]}


@pytest.mark.parametrize(
    ("focus", "annotated"),
    [(None, True), ("homework", True), ("equipment", True), ("exams", False), ("flow", False)],
)
async def test_only_the_categories_a_message_can_assign_something_in_are_annotated(focus, annotated):
    svc = service(message("a", "Math homework: page 50."))
    assert (await annotation(svc, "a", focus) is not None) is annotated


async def test_the_child_level_table_is_gone():
    out = await service(message("a", "Math homework.")).get_tomorrow_brief(focus="homework")
    assert all("held_lessons" not in k for k in out["kids"])


async def test_a_failing_lookup_leaves_the_messages_unannotated_and_blocks_nothing():
    class NoHeld(FakeConnector):
        async def get_held_lessons(self, kid, since, now):
            raise UpstreamError("lesson list down")

    svc = service(message("a", "Math homework: page 50."), conn_cls=NoHeld)
    out = await svc.get_tomorrow_brief(focus="homework")
    assert all(k["ok"] for k in out["kids"])
    assert "lessons_held_since" not in next(m for m in out["messages"] if m["id"] == "a")
