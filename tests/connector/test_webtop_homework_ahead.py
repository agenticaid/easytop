"""Upcoming homework (Erez 2026-10-13): homework is written into a lesson and is due at that subject's NEXT
lesson. For the days after the brief's day, each subject's latest written homework is returned with the date
of its next lesson as the due date. A subject that meets on the brief's own day is skipped (its pending
homework is the 'due tomorrow' item)."""

from datetime import date

from easytop.connector.http import CONTEXT_SWITCH_ENDPOINT, READ_ENDPOINTS
from tests.connector.test_webtop import (
    FakeWebtop,
    _history,
    _pupil_week,
    connector,
)

DAY = date(2026, 10, 4)  # a Sunday: the brief's day; TODAY is Wednesday 2026-09-30


def server_for(weeks: dict[int, list], histories: dict[int, dict]) -> FakeWebtop:
    """weeks: weekIndex -> entries for _pupil_week; each request gets only its own week, like Webtop."""
    server = FakeWebtop()
    server.routes["PupilCard/GetPupilLessonsAndHomework"] = lambda b, t: _pupil_week(
        weeks.get(b["weekIndex"], [])
    )
    server.routes["PupilCard/GetPreviousHomeworks"] = lambda b, t: histories.get(
        b["studyGroup"], _history()
    )
    return server


WEEKS = {
    1: [  # Sun 4 .. Sat 10 Oct
        (DAY.isoformat(), 1, "Math", 11),  # meets on the brief's day: skipped
        (DAY.isoformat(), 2, "Hebrew", 12),  # meets on the brief's day: skipped
        ("2026-10-05", 1, "Art", 13),
        ("2026-10-06", 1, "Math", 11),
        ("2026-10-07", 3, "Science", 14),
    ],
    2: [("2026-10-11", 2, "Geography", 15), ("2026-10-11", 3, "Hebrew", 12)],  # Sun 11 Oct
}
HISTORIES = {
    13: _history(("2026-09-28", "clay", "bring clay for the model", True)),
    14: _history(("2026-09-29", "cells", None, True)),  # its newest lesson had no homework
    15: _history(("2026-09-30", "maps", "read p.9", True), ("2026-09-23", "x", "old", True)),
}


async def ahead(server=None, **kw):
    server = server or server_for(WEEKS, HISTORIES)
    c = connector(server)
    kid = (await c.list_kids())[0]
    return server, kid, await c.get_homework_ahead(kid, DAY, 7, **kw)


async def test_each_subject_gets_its_latest_homework_due_at_its_next_lesson():
    _, _, got = await ahead()
    assert [(h.subject, h.description, h.due, h.assigned) for h in got] == [
        ("Art", "bring clay for the model", date(2026, 10, 5), date(2026, 9, 28)),
        ("Geography", "read p.9", date(2026, 10, 11), date(2026, 9, 30)),
    ]  # Science: its newest lesson had none; Math and Hebrew meet on the brief's day: not repeated


async def test_it_reads_only_the_weeks_and_subjects_it_needs():
    server, kid, _ = await ahead()
    weeks = sorted(
        b["weekIndex"] for e, b, _ in server.calls if e == "PupilCard/GetPupilLessonsAndHomework"
    )
    # the week of the brief's day (1) and the next one (day + 7 reaches 11 Oct); week 0 is read to see which
    # lessons were held since the oldest pending homework was written; a week is read once (memo)
    assert weeks == [0, 1, 2]
    asked = sorted(
        b["studyGroup"] for e, b, _ in server.calls if e == "PupilCard/GetPreviousHomeworks"
    )
    assert asked == [13, 14, 15]  # 11 and 12 meet on the brief's day: no history request


async def test_off_days_are_skipped_so_the_next_real_lesson_is_the_due_day():
    weeks = {1: [("2026-10-05", 1, "Art", 13), ("2026-10-08", 1, "Art", 13)]}
    server = server_for(weeks, HISTORIES)
    _, _, got = await ahead(server, skip_days=frozenset({date(2026, 10, 5)}))
    assert [(h.subject, h.due) for h in got] == [("Art", date(2026, 10, 8))]
    _, _, none = await ahead(
        server_for({1: [("2026-10-05", 1, "Art", 13)]}, HISTORIES),
        skip_days=frozenset({date(2026, 10, 5)}),
    )
    assert none == []  # the only lesson is on an off day: no due day inside the window


async def test_lessons_not_held_are_not_homework():
    histories = {
        13: _history(
            ("2026-09-29", "cancelled", "must not count", False),
            ("2026-09-28", "clay", "bring clay", True),
        )
    }
    _, _, got = await ahead(server_for({1: [("2026-10-05", 1, "Art", 13)]}, histories))
    assert [h.description for h in got] == ["bring clay"]


async def test_a_lesson_after_today_is_never_the_source():
    histories = {13: _history(("2026-10-02", "future row", "not yet written", True))}
    _, _, got = await ahead(server_for({1: [("2026-10-05", 1, "Art", 13)]}, histories))
    assert got == []  # TODAY is 30 Sep: a row dated 2 Oct cannot be an already given homework


async def test_results_are_ordered_by_due_day_then_hour_and_use_only_whitelisted_reads():
    weeks = {
        1: [
            ("2026-10-07", 2, "Art", 13),
            ("2026-10-06", 4, "Science", 14),
            ("2026-10-06", 1, "Geography", 15),
        ]
    }
    histories = {g: _history(("2026-09-28", "t", f"hw{g}", True)) for g in (13, 14, 15)}
    server, _, got = await ahead(server_for(weeks, histories))
    assert [(h.subject, h.due, h.hour) for h in got] == [
        ("Geography", date(2026, 10, 6), 1),
        ("Science", date(2026, 10, 6), 4),
        ("Art", date(2026, 10, 7), 2),
    ]
    assert set(server.endpoints()) <= READ_ENDPOINTS | {CONTEXT_SWITCH_ENDPOINT}
