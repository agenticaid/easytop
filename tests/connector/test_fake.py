from datetime import date, datetime, timedelta

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import SessionExpired, UpstreamError

TODAY = date(2026, 10, 5)  # Monday, no holiday -> next school day Tue 6 Oct


async def test_kids_are_synthetic():
    kids = await FakeConnector(TODAY).list_kids()
    assert [k.name for k in kids] == ["Noa", "Ido"]
    assert kids[0].school.id != kids[1].school.id


async def test_schedule_applies_changes_on_next_school_day():
    c = FakeConnector(TODAY)
    noa, ido = await c.list_kids()
    tue = date(2026, 10, 6)
    noa_l = await c.get_schedule(noa, tue)
    assert [x.status for x in noa_l][-1] == "cancelled"
    assert noa_l[-1].note
    ido_l = await c.get_schedule(ido, tue)
    assert ido_l[1].status == "room_changed" and ido_l[1].room == "Lab 1"


async def test_homework_exams_messages_windows():
    c = FakeConnector(TODAY)
    noa, ido = await c.list_kids()
    hw = await c.get_homework(noa, date(2026, 10, 6), date(2026, 10, 6))
    assert [h.description for h in hw] == ["Workbook page 42"]
    assert await c.get_homework(noa, date(2026, 10, 7), date(2026, 10, 7)) == []
    assert len(await c.get_exams(ido, TODAY, date(2026, 10, 30))) == 1
    unread = await c.get_messages(None, None, True)
    assert [m.id for m in unread] == ["m1"]
    assert await c.get_messages(noa, datetime(2026, 10, 4), False) == []


async def test_failure_injection():
    c = FakeConnector(TODAY, fail_kids={"k2"})
    noa, ido = await c.list_kids()
    await c.get_schedule(noa, TODAY)
    with pytest.raises(UpstreamError):
        await c.get_schedule(ido, TODAY)
    with pytest.raises(SessionExpired):
        await FakeConnector(TODAY, expired=True).list_kids()


async def test_timetable_rows_may_carry_lesson_box_text():
    import copy

    data = copy.deepcopy(load_synthetic())
    data["timetable"]["k1"]["tue"][0]["note"] = "Topic: fractions. Bring a ruler."
    c = FakeConnector(TODAY, data)
    noa, _ = await c.list_kids()
    first = (await c.get_schedule(noa, date(2026, 10, 6)))[0]
    assert first.note == "Topic: fractions. Bring a ruler."


async def test_lesson_events_are_the_attendance_remarks_in_range():
    c = FakeConnector(TODAY)
    noa, ido = await c.list_kids()
    got = await c.get_lesson_events(ido, TODAY - timedelta(days=14), TODAY)
    assert [e.title for e in got] == ["Late arrival"]
    assert (
        await c.get_lesson_events(noa, TODAY - timedelta(days=14), TODAY) == []
    )  # Noa has a grade only
