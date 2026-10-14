"""Explicit due dates written in a teacher's homework text (Erez 2026-10-13).

Webtop has no due dates: homework is written into a lesson and is due at that subject's next lesson, so once that
lesson was held it is past. Only a date the teacher wrote in the text itself (a calendar date or a weekday) can keep it
alive, and a wrong reading must only ever keep an item visible: a missed homework is worse than an extra line.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_ROLLOVER_DAYS = 150  # a day.month this much earlier than the lesson is read as next year's

_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)")

# Python weekday numbers, Monday 0 .. Sunday 6
_HEBREW_DAYS = {
    "ראשון": 6,
    "שני": 0,
    "שלישי": 1,
    "רביעי": 2,
    "חמישי": 3,
    "שישי": 4,
    "שבת": 5,
    "א": 6,
    "ב": 0,
    "ג": 1,
    "ד": 2,
    "ה": 3,
    "ו": 4,
}
_HEBREW_DAY = re.compile(r"יום\s*(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת|[אבגדהו])(?![א-ת])")
_ENGLISH_DAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_ENGLISH_DAY = re.compile(r"\b(" + "|".join(_ENGLISH_DAYS) + r")\b", re.IGNORECASE)


def _first_after(assigned: date, weekday: int) -> date:
    """The first day strictly after `assigned` that falls on `weekday`."""
    return assigned + timedelta(days=(weekday - assigned.weekday() - 1) % 7 + 1)


def explicit_due_dates(text: str, assigned: date) -> list[date]:
    """Dates the homework text names, sorted. A day.month without a year is read in `assigned`'s year (next
    year when it is months earlier); a weekday is its first occurrence after `assigned`."""
    found: set[date] = set()
    for m in _DATE.finditer(text):
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            if year:
                found.add(date(int(year) + (2000 if len(year) == 2 else 0), month, day))
                continue
            d = date(assigned.year, month, day)
            # months earlier in the year mean next year (a message in December naming 5.1); a few days
            # earlier is simply a date that has passed
            found.add(
                d
                if d >= assigned - timedelta(days=_ROLLOVER_DAYS)
                else date(assigned.year + 1, month, day)
            )
        except ValueError:  # 31.2, 7.13: not a date (a page or exercise number)
            continue
    for m in _HEBREW_DAY.finditer(text):
        found.add(_first_after(assigned, _HEBREW_DAYS[m.group(1)]))
    for m in _ENGLISH_DAY.finditer(text):
        found.add(_first_after(assigned, _ENGLISH_DAYS[m.group(1).lower()]))
    return sorted(found)
