"""A homework with no explicit due date is due at its subject's next lesson. Once that lesson was held the due date
has passed and the homework must not be shown (Erez 2026-10-13): Sunday's Math homework, a Math lesson on Monday,
a question on Monday afternoon -> not shown. Only an explicit later date in the teacher's text keeps it alive.

The homework tab lists only the lessons a teacher filled in (measured on a real account: 60-80% of held lessons
have a row), so 'a lesson was held' comes from the weekly lesson list as well as from the rows."""

from datetime import date, datetime

import httpx

from easytop.connector.webtop import WebtopConnector
from tests.connector.test_webtop import FakeWebtop, _history
from tests.connector.test_webtop_homework_ahead import server_for

THURSDAY = date(2026, 10, 8)  # "today"; the week 4-10 Oct is the current one (index 0)
EVENING = datetime(2026, 10, 8, 18, 0)
MORNING = datetime(2026, 10, 8, 9, 0)
SUNDAY = date(2026, 10, 11)  # the brief's day (the next school day after the weekend)


def conn(server: FakeWebtop) -> WebtopConnector:
    return WebtopConnector(
        {"uniqueId": "u", "webToken": "login-token"},
        transport=httpx.MockTransport(server.handler),
        clock=lambda: THURSDAY,
    )


# study groups: 11 Math, 12 Hebrew, 13 Art, 14 Science, 15 English, 16 History, 17 Geography
WEEKS = {
    -1: [
        ("2026-09-27", 1, "History", 16)
    ],  # Sukkot: the school was closed (the fixture's days off)
    0: [
        ("2026-10-05", 2, "Math", 11),  # Monday: held, the teacher wrote nothing
        ("2026-10-05", 3, "Science", 14),
        ("2026-10-05", 1, "English", 15),  # Monday: cancelled (the tab says so)
        ("2026-10-08", 1, "Hebrew", 12),  # today
    ],
    1: [  # Sunday 11 Oct: every subject meets again
        (SUNDAY.isoformat(), 1, "Math", 11),
        (SUNDAY.isoformat(), 2, "Art", 13),
        (SUNDAY.isoformat(), 3, "Science", 14),
        (SUNDAY.isoformat(), 4, "History", 16),
        (SUNDAY.isoformat(), 5, "Hebrew", 12),
        (SUNDAY.isoformat(), 6, "English", 15),
        (SUNDAY.isoformat(), 7, "Geography", 17),
    ],
}
HISTORIES = {
    11: _history(("2026-10-04", "fractions", "Math page 6", True)),
    13: _history(("2026-10-04", "clay", "bring clay", True)),
    14: _history(("2026-10-04", "cells", "להגשה עד יום ה׳: פרק 4", True)),
    16: _history(("2026-09-24", "Egypt", "History worksheet", True)),
    12: _history(("2026-10-07", "reading", "Hebrew read p.9", True)),
    15: _history(
        ("2026-10-05", "cancelled", None, False), ("2026-10-04", "essay", "English write", True)
    ),
    17: _history(
        ("2026-10-05", "maps", None, True), ("2026-10-04", "rivers", "Geography map", True)
    ),
}


async def for_day(now: datetime):
    c = conn(server_for(WEEKS, HISTORIES))
    kid = (await c.list_kids())[0]
    got = await c.get_homework_for_day(kid, SUNDAY, now=now)
    return {h.subject: h.description for h in got}


async def test_a_lesson_held_after_the_homework_means_its_due_date_passed():
    got = await for_day(EVENING)
    assert "Math" not in got  # written Sunday, Math held on Monday (no row for it), asked Thursday


async def test_a_homework_with_no_lesson_after_it_is_still_pending():
    got = await for_day(EVENING)
    assert got["Art"] == "bring clay"


async def test_a_lesson_on_a_vacation_day_was_not_held_so_the_homework_before_it_stays():
    got = await for_day(EVENING)
    assert (
        got["History"] == "History worksheet"
    )  # given 24 Sep, the only lesson since was in the Sukkot break


async def test_a_cancelled_lesson_was_not_held_so_the_homework_stays():
    got = await for_day(EVENING)
    assert got["English"] == "English write"


async def test_a_recorded_lesson_after_it_also_means_passed_even_when_the_weekly_list_misses_it():
    got = await for_day(EVENING)
    assert "Geography" not in got  # the tab has a Monday row without homework: the lesson was held


async def test_an_explicit_later_date_in_the_text_keeps_it_even_though_a_lesson_was_held():
    got = await for_day(EVENING)
    assert (
        got["Science"] == "להגשה עד יום ה׳: פרק 4"
    )  # the Thursday after Sunday is today: not passed


async def test_todays_lesson_counts_as_held_only_from_the_afternoon():
    assert "Hebrew" not in await for_day(EVENING)  # written Wednesday, Hebrew held this morning
    assert (await for_day(MORNING))["Hebrew"] == "Hebrew read p.9"  # asked before school ended


async def test_without_a_clock_no_lesson_of_today_counts_as_held():
    c = conn(server_for(WEEKS, HISTORIES))
    kid = (await c.list_kids())[0]
    got = {h.subject for h in await c.get_homework_for_day(kid, SUNDAY)}
    assert "Hebrew" in got and "Math" not in got  # earlier days still count


async def test_the_upcoming_list_drops_a_homework_whose_next_lesson_already_passed_too():
    weeks = {
        0: [("2026-10-05", 2, "Math", 11)],  # Monday: held
        1: [
            ("2026-10-12", 2, "Math", 11),
            ("2026-10-13", 1, "Art", 13),
        ],  # next week, after the weekend
    }
    c = conn(server_for(weeks, HISTORIES))
    kid = (await c.list_kids())[0]
    got = await c.get_homework_ahead(kid, SUNDAY, 7, now=EVENING)
    assert [(h.subject, h.due) for h in got] == [("Art", date(2026, 10, 13))]


async def test_held_lessons_are_the_listed_lessons_on_school_days_up_to_now():
    c = conn(server_for(WEEKS, HISTORIES))
    kid = (await c.list_kids())[0]
    held = await c.get_held_lessons(kid, date(2026, 9, 25), EVENING)
    assert {(h.day.isoformat(), h.subject) for h in held} == {
        ("2026-10-05", "Math"),
        ("2026-10-05", "Science"),
        (
            "2026-10-05",
            "English",
        ),  # the list has no clock times; the cancellation is not known here
        ("2026-10-08", "Hebrew"),  # today, after 14:00
    }  # the Sukkot lesson of 27 Sep and next week's are not there
    morning = await c.get_held_lessons(kid, date(2026, 9, 25), MORNING)
    assert "Hebrew" not in {h.subject for h in morning}
