"""The brief's Homework has two parts (Erez 2026-10-13): `homework_due` (what is due on the brief's day) and
`homework_upcoming` (what is due on later days: each subject's latest homework, due at its next lesson)."""

from datetime import date

import pytest

from easytop.connector.fake import FakeConnector
from easytop.errors import UpstreamError
from easytop.family.service import FamilyService
from tests.family.test_service import NOW, make


async def test_the_brief_lists_homework_due_later_with_its_due_day():
    _, svc = make()
    noa, ido = (await svc.get_tomorrow_brief())["kids"]
    assert [(h["subject"], h["due"]) for h in noa["homework_upcoming"]] == [
        ("Hebrew", "2026-10-08")
    ]
    assert [(h["subject"], h["due"]) for h in ido["homework_upcoming"]] == [
        ("History", "2026-10-07")
    ]
    assert noa["homework_upcoming"][0]["description"] == "Read chapter 3"
    assert [h["subject"] for h in noa["homework_due"]] == [
        "Math"
    ]  # tomorrow's item is not repeated


@pytest.mark.parametrize(
    ("focus", "present"),
    [(None, True), ("homework", True), ("equipment", False), ("exams", False), ("flow", False)],
)
async def test_upcoming_homework_belongs_to_the_homework_category(focus, present):
    _, svc = make()
    noa = (await svc.get_tomorrow_brief("Noa", focus=focus))["kids"][0]
    assert ("homework_upcoming" in noa) is present


async def test_the_schools_off_days_are_passed_so_they_never_count_as_a_next_lesson():
    seen = {}

    class Spy(FakeConnector):
        async def get_homework_ahead(self, kid, day, horizon=7, skip_days=frozenset(), now=None):
            seen["args"] = (day, horizon, skip_days)
            return await super().get_homework_ahead(kid, day, horizon, skip_days, now)

    _, svc = make(Spy(NOW.date()))
    await svc.get_tomorrow_brief("Noa", focus="homework")
    day, horizon, skip = seen["args"]
    assert (day, horizon) == (date(2026, 10, 6), 7)
    assert skip == {date(2026, 10, 9), date(2026, 10, 10)}  # Friday and Saturday


async def test_a_failing_look_ahead_never_hides_tomorrows_homework():
    class NoAhead(FakeConnector):
        async def get_homework_ahead(self, kid, day, horizon=7, skip_days=frozenset(), now=None):
            raise UpstreamError("history down")

    _, svc = make(NoAhead(NOW.date()))
    noa = (await svc.get_tomorrow_brief("Noa", focus="homework"))["kids"][0]
    assert noa["ok"] is True and noa["homework_due"]
    assert "homework_upcoming" not in noa and noa["homework_upcoming_error"] == "unavailable"


async def test_the_look_ahead_is_cached_for_five_minutes():
    conn, svc = make()
    await svc.get_tomorrow_brief("Noa", focus="homework")
    await svc.get_tomorrow_brief("Noa", focus="homework")
    assert len([c for c in conn.calls if c[0] == "get_homework_ahead"]) == 1


async def test_a_subject_that_meets_tomorrow_is_not_listed_twice():
    svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
    noa = (await svc.get_tomorrow_brief("Noa", focus="homework"))["kids"][0]
    due_tomorrow = {h["subject"] for h in noa["homework_due"]}
    assert not due_tomorrow & {h["subject"] for h in noa["homework_upcoming"]}
