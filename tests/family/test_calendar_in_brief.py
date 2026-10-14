"""Calendar boxes of the brief's day (trips, 'other' entries in the weekly schedule) are material for the
Equipment, Exams and Day flow categories; they used to be reachable only through get_events."""

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.family.service import FamilyService
from tests.family.test_service import NOW


def service():
    data = load_synthetic()
    data["events"] += [
        {"kid_id": "k1", "kind": "calendar", "days_ago": -1, "title": "Zoo trip", "detail": "hat"},
        {"kid_id": "k1", "kind": "calendar", "days_ago": -2, "title": "Other day", "detail": None},
        {"kid_id": "k1", "kind": "attendance", "days_ago": -1, "title": "Late", "detail": None},
    ]
    return FamilyService(FakeConnector(NOW.date(), data), clock=lambda: NOW)


@pytest.mark.parametrize(
    ("focus", "present"),
    [(None, True), ("equipment", True), ("exams", True), ("flow", True), ("homework", False)],
)
async def test_the_calendar_box_of_the_day_is_in_the_categories_that_need_it(focus, present):
    noa = (await service().get_tomorrow_brief("Noa", focus=focus))["kids"][0]
    assert ("calendar" in noa) is present
    if present:
        assert [(e["title"], e["detail"]) for e in noa["calendar"]] == [("Zoo trip", "hat")]


async def test_only_calendar_boxes_of_that_day_not_grades_or_attendance():
    noa = (await service().get_tomorrow_brief("Noa"))["kids"][0]
    assert {e["title"] for e in noa["calendar"]} == {"Zoo trip"}


async def test_a_kid_without_calendar_boxes_gets_an_empty_list():
    ido = (await service().get_tomorrow_brief("Ido", focus="flow"))["kids"][0]
    assert ido["calendar"] == []
