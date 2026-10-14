from datetime import date, datetime

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import KidNotFound, UpstreamError
from easytop.family.service import MAX_MESSAGE_CHARS, FamilyService

NOW = datetime(2026, 10, 5, 18, 42)  # Monday evening


def make(conn=None, clock=None):
    conn = conn or FakeConnector(NOW.date())
    return conn, FamilyService(conn, clock=clock or (lambda: NOW))


async def test_list_kids():
    _, svc = make()
    out = await svc.list_kids()
    assert [k["name"] for k in out["kids"]] == ["Noa", "Ido"]


async def test_tomorrow_brief_all_kids_grouped():
    _, svc = make()
    out = await svc.get_tomorrow_brief()
    assert out["date"] == "2026-10-06" and out["weekday"] == "Tuesday"
    assert out["skipped"] == [] and out["session_expired"] is False
    noa, ido = out["kids"]
    assert noa["name"] == "Noa" and noa["ok"]
    assert noa["homework_due"] == [
        {"subject": "Math", "description": "Workbook page 42", "due": "2026-10-06"}
    ]
    assert len(noa["lessons"]) == 5
    assert ido["changes"][0]["status"] == "room_changed"
    assert [m["id"] for m in out["messages"]] == ["m1", "m2"]  # the whole 7-day inbox, once
    assert "messages_mentioning_day" not in ido and "messages" not in ido
    assert ido["upcoming_exams"][0]["subject"] == "Science"


async def test_cancelled_last_lesson_moves_end_time():
    _, svc = make()
    noa = (await svc.get_tomorrow_brief("Noa"))["kids"][0]
    assert noa["ends_at"] == "11:45"  # period 5 (12:45) cancelled
    assert noa["starts_at"] == "08:00"
    assert noa["changes"][0]["status"] == "cancelled"


async def test_thursday_evening_skips_weekend():
    now = datetime(2026, 10, 8, 19, 0)
    _, svc = make(FakeConnector(now.date()), clock=lambda: now)
    out = await svc.get_tomorrow_brief("k1")
    assert out["date"] == "2026-10-11" and out["weekday"] == "Sunday"
    assert [s["reason"] for s in out["skipped"]] == ["Friday", "Saturday"]


async def test_kid_scoping_by_id_or_name_and_unknown_rejected():
    _, svc = make()
    assert (await svc.get_tomorrow_brief("k2"))["kids"][0]["name"] == "Ido"
    assert (await svc.get_tomorrow_brief(" noa "))["kids"][0]["id"] == "k1"
    with pytest.raises(KidNotFound):
        await svc.get_tomorrow_brief("someone-elses-kid")
    with pytest.raises(KidNotFound):
        await svc.get_schedule("k99", "2026-10-06")


async def test_partial_failure_is_per_kid():
    conn, svc = make(FakeConnector(NOW.date(), fail_kids={"k2"}))
    out = await svc.get_tomorrow_brief()
    noa, ido = out["kids"]
    assert noa["ok"] is True
    assert ido["ok"] is False and ido["error"] == "unavailable" and ido["name"] == "Ido"
    assert out["session_expired"] is False


async def test_expired_session_flagged():
    conn = FakeConnector(NOW.date())
    _, svc = make(conn)
    await svc.list_kids()  # kids discovered while valid
    conn.expired = True
    out = await svc.get_schedule("k1", "2026-10-06")
    assert out["session_expired"] is True and out["kids"][0]["error"] == "session_expired"


async def test_cache_hits_within_five_minutes_and_expires():
    clock = {"t": NOW}
    conn, svc = make(clock=lambda: clock["t"])
    await svc.get_schedule("k1", "2026-10-06")
    await svc.get_schedule("k1", "2026-10-06")
    assert [c for c in conn.calls if c[0] == "get_schedule"] == [("get_schedule", "k1")]
    clock["t"] = datetime(2026, 10, 5, 18, 48)  # +6 min
    await svc.get_schedule("k1", "2026-10-06")
    assert len([c for c in conn.calls if c[0] == "get_schedule"]) == 2


async def test_failures_are_not_cached():
    conn, svc = make(FakeConnector(NOW.date(), fail_kids={"k1"}))
    await svc.get_schedule("k1", "2026-10-06")
    conn.fail_kids.clear()
    out = await svc.get_schedule("k1", "2026-10-06")
    assert out["kids"][0]["ok"] is True


async def test_fanout_runs_in_parallel():
    import asyncio

    active = {"now": 0, "max": 0}

    class Slow(FakeConnector):
        async def get_schedule(self, kid, day):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return await super().get_schedule(kid, day)

    _, svc = make(Slow(NOW.date()))
    await svc.get_tomorrow_brief()
    assert active["max"] == 2


async def test_messages_capped_and_named():
    data = load_synthetic()
    data["messages"].append(
        {
            "id": "m9",
            "kid_id": "k1",
            "sender": "X",
            "subject": "Long",
            "body": "a" * 5000,
            "sent_days_ago": 1,
            "unread": True,
            "has_attachments": False,
        }
    )
    _, svc = make(FakeConnector(NOW.date(), data))
    out = await svc.get_messages(kid="Noa", unread=True)
    m = out["messages"][0]
    assert m["kid"] == "Noa" and m["truncated"] is True and len(m["text"]) <= MAX_MESSAGE_CHARS + 1


async def test_homework_exams_events_tools():
    _, svc = make()
    hw = await svc.get_homework("k1", date(2026, 10, 6), date(2026, 10, 12))
    assert len(hw["kids"][0]["homework"]) == 2
    ex = await svc.get_exams("k2", "2026-10-05", "2026-10-30")
    assert ex["kids"][0]["exams"][0]["topic"] == "Forces and motion"
    ev = await svc.get_events("k2", "2026-09-01", "2026-10-05")
    assert ev["kids"][0]["events"][0]["kind"] == "attendance"
    assert "raw" not in str(ev).lower()


def test_homework_without_a_due_date_is_rendered_with_its_assigned_date():
    from datetime import date

    from easytop.family.service import homework_out
    from easytop.models import Homework

    out = homework_out(Homework("k", "Math", "Page 5", due=None, assigned=date(2026, 10, 4)))
    assert out == {"subject": "Math", "description": "Page 5", "assigned": "2026-10-04"}
    out = homework_out(Homework("k", "Math", "Page 5", due=date(2026, 10, 5)))
    assert out["due"] == "2026-10-05" and "assigned" not in out


async def test_tomorrow_brief_asks_the_connector_for_homework_of_that_day_not_a_range():
    from easytop.models import Homework

    calls = []

    class Spy(FakeConnector):
        async def get_homework_for_day(self, kid, day, now=None):
            calls.append((kid.id, day))
            return [Homework(kid.id, "Math", "Page 5", due=None, assigned=day)]

        async def get_homework(self, kid, start, end):
            raise AssertionError("tomorrow's homework must not be a date-range query")

    svc = FamilyService(Spy(NOW.date()), clock=lambda: NOW)
    out = await svc.get_tomorrow_brief()
    assert calls and len({d for _, d in calls}) == 1
    assert out["kids"][0]["homework_due"][0]["description"] == "Page 5"


def test_homework_out_carries_the_class_topic():
    from easytop.family.service import homework_out
    from easytop.models import Homework

    out = homework_out(Homework("k", "Math", "p.6", assigned=date(2026, 9, 27), topic="angles"))
    assert out["topic"] == "angles"
    assert "topic" not in homework_out(Homework("k", "Math", "p.6", assigned=date(2026, 9, 27)))


# ---- answer categories: focus on the tomorrow brief ----


async def test_focus_homework_returns_homework_and_the_inbox_but_no_lessons_or_exams():
    class Recording(FakeConnector):
        seen: list[str] = []

        async def get_schedule(self, kid, day):
            self.seen.append("schedule")
            return await super().get_schedule(kid, day)

        async def get_exams(self, kid, start, end):
            self.seen.append("exams")
            return await super().get_exams(kid, start, end)

    conn, svc = make(Recording(NOW.date()))
    out = await svc.get_tomorrow_brief("Noa", focus="homework")
    noa = out["kids"][0]
    assert out["focus"] == "homework"
    assert noa["homework_due"][0]["subject"] == "Math"
    assert not {"lessons", "changes", "upcoming_exams", "messages"} & set(noa)
    assert [m["id"] for m in out["messages"]] == [
        "m1",
        "m2",
    ]  # the inbox is material for every category
    assert conn.seen == []


async def test_focus_equipment_has_lessons_and_the_inbox_but_no_homework_or_exams():
    _, svc = make()
    out = await svc.get_tomorrow_brief("Ido", focus="equipment")
    ido = out["kids"][0]
    assert ido["lessons"] and [m["id"] for m in out["messages"]] == ["m1", "m2"]
    assert not {"homework_due", "homework_upcoming", "upcoming_exams"} & set(ido)


async def test_focus_flow_has_lessons_changes_and_times_but_no_homework_or_exams():
    _, svc = make()
    out = await svc.get_tomorrow_brief("Ido", focus="flow")
    ido = out["kids"][0]
    assert ido["lessons"] and ido["changes"] and ido["ends_at"]
    assert not {"homework_due", "homework_upcoming", "upcoming_exams"} & set(ido)
    assert out["messages"]


async def test_focus_exams_is_its_own_category_with_exams_and_the_inbox():
    _, svc = make()
    out = await svc.get_tomorrow_brief("Ido", focus="exams")
    ido = out["kids"][0]
    assert out["focus"] == "exams"
    assert ido["upcoming_exams"][0]["subject"] == "Science"
    assert out["messages"]
    assert not {"lessons", "changes", "homework_due", "homework_upcoming"} & set(ido)


async def test_no_focus_is_everything_and_unknown_focus_is_refused():
    _, svc = make()
    out = await svc.get_tomorrow_brief("Noa")
    assert {"lessons", "homework_due", "upcoming_exams"} <= set(out["kids"][0]) and out["messages"]
    with pytest.raises(ValueError, match="focus"):
        await svc.get_tomorrow_brief("Noa", focus="gossip")


# ---- double lessons: two boxes in consecutive hours become one entry ----


def _tue_lessons(*rows):
    """Synthetic Noa with Tuesday's timetable replaced by (period, subject, teacher, note) rows."""
    import copy

    data = copy.deepcopy(load_synthetic())
    data["changes"] = []
    data["timetable"]["k1"]["tue"] = [
        {
            "period": p,
            "start": f"{7 + p:02d}:00",
            "end": f"{7 + p:02d}:45",
            "subject": s,
            "teacher": t,
            "room": "1",
            **({"note": n} if n else {}),
        }
        for p, s, t, n in rows
    ]
    return FamilyService(FakeConnector(NOW.date(), data), clock=lambda: NOW)


async def test_consecutive_boxes_of_one_subject_and_teacher_are_one_double_lesson():
    svc = _tue_lessons(
        (1, "Hebrew", "Ms. A", "ציוד: מחברת"),
        (2, "Hebrew", "Ms. A", "ציוד: מחברת"),
        (3, "Math", "Ms. B", None),
    )
    lessons = (await svc.get_tomorrow_brief("Noa", focus="equipment"))["kids"][0]["lessons"]
    assert [(x["subject"], x["period"], x.get("hours")) for x in lessons] == [
        ("Hebrew", 1, "1-2"),
        ("Math", 3, None),
    ]
    assert lessons[0]["note"] == "ציוד: מחברת"  # the same text in both boxes is kept once


async def test_different_notes_of_a_double_lesson_are_both_kept():
    svc = _tue_lessons((1, "Art", "Ms. A", "bring clay"), (2, "Art", "Ms. A", "wear old shirt"))
    (art,) = (await svc.get_schedule("Noa", "2026-10-06"))["kids"][0]["lessons"]
    assert art["hours"] == "1-2" and art["note"] == "bring clay | wear old shirt"


async def test_not_merged_when_hours_are_apart_or_the_teacher_differs():
    svc = _tue_lessons(
        (1, "Math", "Ms. A", None),
        (3, "Math", "Ms. A", None),  # a gap
        (4, "Math", "Mr. C", None),  # other teacher
        (5, "Math", "Mr. C", None),  # merges with 4
    )
    lessons = (await svc.get_schedule("Noa", "2026-10-06"))["kids"][0]["lessons"]
    assert [(x["period"], x.get("hours")) for x in lessons] == [(1, None), (3, None), (4, "4-5")]


# ---- the next school day comes from the school's own days off, per kid ----


def _off(inactive):
    from easytop.models import DaysOff

    return DaysOff(frozenset(inactive), ())


async def test_a_school_with_friday_lessons_makes_friday_the_next_day_and_other_kids_keep_theirs():
    class WithOffDays(FakeConnector):
        async def get_days_off(self, kid, start, end):
            return _off({5}) if kid.id == "k1" else None  # Noa's school: only Saturday is off

    thursday = datetime(2026, 10, 8, 19, 0)
    _, svc = make(WithOffDays(thursday.date()), clock=lambda: thursday)
    out = await svc.get_tomorrow_brief()
    noa, ido = out["kids"]
    assert out["date"] == "2026-10-09" and out["weekday"] == "Friday"  # the earliest day
    assert "date" not in noa  # same as the envelope
    assert ido["date"] == "2026-10-11"  # Ido's school (default week) is next in school on Sunday
    assert out["skipped"] == []


async def test_a_failing_days_off_lookup_is_a_per_kid_failure_not_a_silent_default():
    from easytop.errors import UpstreamError

    class Broken(FakeConnector):
        async def get_days_off(self, kid, start, end):
            if kid.id == "k2":
                raise UpstreamError("InitData failed")
            return None

    _, svc = make(Broken(NOW.date()))
    noa, ido = (await svc.get_tomorrow_brief())["kids"]
    assert noa["ok"] and ido["ok"] is False and ido["error"] == "unavailable"


# ---- the pupil card's lesson events (missing equipment / homework) reach the brief ----


async def test_recent_lesson_events_are_in_the_homework_and_equipment_focus_only():
    _, svc = make()
    for focus, expected in (("homework", True), ("equipment", True), ("flow", False), (None, True)):
        ido = (await svc.get_tomorrow_brief("Ido", focus=focus))["kids"][0]
        assert ("recent_lesson_events" in ido) is expected, focus
    ido = (await svc.get_tomorrow_brief("Ido", focus="homework"))["kids"][0]
    assert ido["recent_lesson_events"] == [
        {"date": "2026-10-02", "type": "Late arrival", "detail": "08:40"}
    ]


async def test_lesson_events_older_than_two_weeks_are_left_out():
    import copy

    data = copy.deepcopy(load_synthetic())
    data["events"].append(
        {"kid_id": "k2", "kind": "attendance", "days_ago": 20, "title": "Old entry", "detail": None}
    )
    _, svc = make(FakeConnector(NOW.date(), data))
    ido = (await svc.get_tomorrow_brief("Ido", focus="equipment"))["kids"][0]
    assert [e["type"] for e in ido["recent_lesson_events"]] == ["Late arrival"]


# ---- hour 0 in elementary school is a day opener, not a lesson ----


def _with_hour_zero(grade):
    import copy

    data = copy.deepcopy(load_synthetic())
    data["changes"] = []
    data["kids"][0]["grade"] = grade
    data["timetable"]["k1"]["tue"] = [
        {
            "period": 0,
            "start": "07:40",
            "end": "07:55",
            "subject": "פתיחת בוקר",
            "teacher": "Ms. A",
            "room": "1",
            "note": "מעגל שיח, להביא סוודר",
        },
        {
            "period": 1,
            "start": "08:00",
            "end": "08:45",
            "subject": "Math",
            "teacher": "Ms. A",
            "room": "1",
        },
        {
            "period": 2,
            "start": "08:50",
            "end": "09:35",
            "subject": "Hebrew",
            "teacher": "Ms. B",
            "room": "2",
        },
    ]
    return FamilyService(FakeConnector(NOW.date(), data), clock=lambda: NOW)


async def test_elementary_hour_zero_is_a_day_opener_and_the_next_lesson_follows_it():
    lessons = (await _with_hour_zero(3).get_schedule("Noa", "2026-10-06"))["kids"][0]["lessons"]
    opener, first, second = lessons
    assert opener["day_opener"] is True and "period" not in opener
    assert opener["subject"] == "פתיחת בוקר" and opener["note"] == "מעגל שיח, להביא סוודר"
    assert first["period"] == 1 and first["after_day_opener"] is True
    assert "after_day_opener" not in second and "day_opener" not in first


async def test_the_brief_marks_the_opener_too():
    brief = await _with_hour_zero(3).get_tomorrow_brief("Noa", focus="equipment")
    assert brief["kids"][0]["lessons"][0]["day_opener"] is True


async def test_older_grades_or_an_unknown_grade_keep_hour_zero_as_a_real_lesson():
    for grade in (7, 10, None):
        first = (await _with_hour_zero(grade).get_schedule("Noa", "2026-10-06"))["kids"][0][
            "lessons"
        ][0]
        assert first["period"] == 0 and "day_opener" not in first, grade


async def test_no_hour_zero_means_no_markers():
    _, svc = make()
    lessons = (await svc.get_schedule("Noa", "2026-10-06"))["kids"][0]["lessons"]
    assert not any("day_opener" in x or "after_day_opener" in x for x in lessons)


async def test_school_tomorrow_is_false_when_the_next_day_is_a_day_off():
    now = datetime(2026, 10, 8, 19, 0)  # Thursday evening, Friday is off in the default week
    _, svc = make(FakeConnector(now.date()), clock=lambda: now)
    assert await svc.school_tomorrow() is False


async def test_school_tomorrow_is_true_on_a_sunday_evening():
    now = datetime(2026, 10, 4, 19, 0)  # Sunday evening, Monday is a school day
    _, svc = make(FakeConnector(now.date()), clock=lambda: now)
    assert await svc.school_tomorrow() is True


async def test_school_tomorrow_is_true_if_any_kid_has_school_and_raises_if_none_can_be_checked():
    now = datetime(2026, 10, 4, 19, 0)

    class OneKidFails(FakeConnector):
        async def get_days_off(self, kid, start, end):
            if kid.id == "k1":
                raise UpstreamError("down")
            return await super().get_days_off(kid, start, end)

    _, svc = make(OneKidFails(now.date()), clock=lambda: now)
    assert await svc.school_tomorrow() is True

    class AllFail(FakeConnector):
        async def get_days_off(self, kid, start, end):
            raise UpstreamError("down")

    _, svc = make(AllFail(now.date()), clock=lambda: now)
    with pytest.raises(UpstreamError):
        await svc.school_tomorrow()


async def test_school_tomorrow_fetches_no_schedule_homework_or_messages():
    now = datetime(2026, 10, 4, 19, 0)

    class Recording(FakeConnector):
        calls: list[str] = []

        async def get_schedule(self, *a, **k):
            self.calls.append("schedule")
            return await super().get_schedule(*a, **k)

        async def get_messages(self, *a, **k):
            self.calls.append("messages")
            return await super().get_messages(*a, **k)

        async def get_homework_for_day(self, *a, **k):
            self.calls.append("homework")
            return await super().get_homework_for_day(*a, **k)

    conn, svc = make(Recording(now.date()), clock=lambda: now)
    await svc.school_tomorrow()
    assert Recording.calls == []
