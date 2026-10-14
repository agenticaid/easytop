"""Homework without a due date is due at the subject's next lesson; once that lesson was held it is past.
Only an explicit later date in the teacher's own text keeps it alive (Erez 2026-10-13)."""

from datetime import date

import pytest

from easytop.connector.duedates import explicit_due_dates
from easytop.models import DaysOff, Vacation

SUNDAY = date(2026, 10, 4)  # the lesson the homework was written in


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("לסיים עד 15.10", [date(2026, 10, 15)]),
        ("להגיש ב-15/10", [date(2026, 10, 15)]),
        ("Hand in by 15.10.26", [date(2026, 10, 15)]),
        ("due 3/11/2026 at the latest", [date(2026, 11, 3)]),
        ("עד 5.1", [date(2027, 1, 5)]),  # months before the lesson: next calendar year (Dec -> Jan)
        ("להגיש עד 2.10", [date(2026, 10, 2)]),  # a few days before the lesson: simply a past date
        ("עמ׳ 17", []),
        ("workbook page 42", []),
        ("", []),
    ],
)
def test_explicit_dates_are_read_from_the_homework_text(text, expected):
    assert explicit_due_dates(text, SUNDAY) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("להגיש עד יום ה׳", [date(2026, 10, 8)]),  # the first Thursday after the lesson
        ("להגיש עד יום חמישי", [date(2026, 10, 8)]),
        ("for Thursday", [date(2026, 10, 8)]),
        (
            "עד יום ראשון",
            [date(2026, 10, 11)],
        ),  # a Sunday lesson: the NEXT Sunday, not the same day
    ],
)
def test_a_weekday_means_its_first_occurrence_after_the_lesson(text, expected):
    assert explicit_due_dates(text, SUNDAY) == expected


def test_page_numbers_with_a_dot_are_not_mistaken_for_dates_outside_a_valid_calendar_day():
    assert explicit_due_dates("עמ׳ 31.2", SUNDAY) == []  # no 31 February
    assert explicit_due_dates("exercise 7.13", SUNDAY) == []  # no month 13


def test_days_off_cover_inactive_weekdays_and_vacation_ranges():
    off = DaysOff(frozenset({4}), (Vacation(date(2026, 9, 25), date(2026, 10, 3), "Sukkot"),))
    assert off.covers(date(2026, 10, 2))  # inside the vacation (also a Friday)
    assert off.covers(date(2026, 10, 9))  # a Friday: weekday 4 never has school here
    assert not off.covers(date(2026, 10, 4))  # Sunday after the vacation
    assert not off.covers(date(2026, 9, 24))  # the day before the vacation
