"""Strict parsers, tested against anonymized recordings of the real parent API (tests/fixtures/webtop)."""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from easytop.connector import parse
from easytop.errors import ParseError, UpstreamError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "webtop"


def fx(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def resp(name: str) -> dict:
    return fx(name)["response"]


# ---- envelope ----


def test_unwrap_returns_data_of_a_successful_envelope():
    assert parse.unwrap({"status": True, "data": [1]}) == [1]


def test_unwrap_rejects_non_envelopes():
    for bad in (None, [], "x", {"data": 1}, {"status": True}):
        with pytest.raises(ParseError):
            parse.unwrap(bad)


def test_unwrap_status_false_is_an_upstream_error():
    with pytest.raises(UpstreamError):
        parse.unwrap({"status": False, "data": None, "errorId": 7})


# ---- contexts and kids ----


def test_contexts_separate_parent_from_teacher():
    ctxs = parse.parse_contexts(resp("user__GetMultipleUsersForUser__01"))
    assert [c.is_parent for c in ctxs] == [False, True]
    parent = ctxs[1]
    assert parent.school_name == "School 2"
    assert isinstance(parent.institution_code, int) and parent.student_id


def test_contexts_missing_field_is_a_parse_error():
    r = resp("user__GetMultipleUsersForUser__01")
    del r["data"][0]["studentId"]
    with pytest.raises(ParseError, match="studentId"):
        parse.parse_contexts(r)


def test_children_from_dashboard():
    kids = parse.parse_children(resp("dashboard__InitDashboard__01"))
    assert len(kids) == 3
    assert all(isinstance(k.class_code, int) and isinstance(k.class_num, int) for k in kids)
    assert kids[0].first_name and kids[0].last_name and kids[0].id


def test_children_null_in_a_teacher_context_means_none():
    r = copy.deepcopy(resp("dashboard__InitDashboard__01"))
    r["data"]["childrens"] = None
    assert parse.parse_children(r) == []


def test_children_wrong_type_is_a_parse_error():
    r = copy.deepcopy(resp("dashboard__InitDashboard__01"))
    r["data"]["childrens"][0]["classCode"] = "א"
    with pytest.raises(ParseError, match="classCode"):
        parse.parse_children(r)


def test_grade_letter_format():
    assert parse.class_label(1, 2) == "א׳2"
    assert parse.class_label(5, 1) == "ה׳1"
    assert parse.class_label(12, 3) == "יב׳3"


def test_class_label_refuses_unknown_grade():
    with pytest.raises(ParseError):
        parse.class_label(0, 1)


# ---- settings (module ids, divisions) ----


def test_settings_modules_by_universal_title_and_current_division():
    s = parse.parse_settings(resp("PupilCard__GetSettingsList__01"))
    assert s.modules["scheduale"] == 10 and s.modules["lessonSubjectHomework"] == 11
    assert s.modules["lessonEvents"] == 4 and s.modules["grades"] == 6
    assert s.study_year == 2027
    cur = [d for d in s.divisions if d.current]
    assert len(cur) == 1 and cur[0].start < cur[0].end and isinstance(cur[0].start, date)


# ---- lessons (WeeklyScedule/GetEvents) ----


def test_lessons_from_week_events():
    r = resp("WeeklyScedule__GetEvents__01")
    events = r["data"]["data"]["events"]
    lessons = parse.parse_lessons(r, "kid-1")
    assert len(lessons) == len(events)
    first = lessons[0]
    assert first.kid_id == "kid-1" and first.status == "normal"
    assert first.day == date.fromisoformat(events[0]["date"][:10])
    assert first.period == events[0]["startTime"]
    assert first.start is None and first.end is None  # the parent API gives no clock times


def test_lessons_keep_teacher_and_lesson_text_as_note():
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    ev = r["data"]["data"]["events"][0]
    ev.update(teacher="Ms Real", description="Fractions", notes="Bring ruler", title="Math")
    lesson = parse.parse_lessons(r, "k")[0]
    assert lesson.subject == "Math" and lesson.teacher == "Ms Real"
    assert lesson.note == "Fractions | Bring ruler"


def test_lessons_unknown_event_type_is_not_silently_dropped():
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    r["data"]["data"]["events"][0]["typeId"] = 999
    with pytest.raises(ParseError, match="typeId"):
        parse.parse_lessons(r, "k")


def test_lessons_missing_events_list_is_a_parse_error():
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    del r["data"]["data"]["events"]
    with pytest.raises(ParseError, match="events"):
        parse.parse_lessons(r, "k")


# ---- homework ----


def test_homework_entries_with_text_become_homework():
    rows = resp("PupilCard__GetPreviousHomeworks__01")["data"]
    with_text = [x for x in rows if x["homeWork"]]
    assert with_text, "fixture should contain a homework row"
    hw = parse.parse_homework_entry(with_text[0], "k", date(2026, 9, 18))
    assert hw is not None
    assert hw.kid_id == "k" and hw.description == with_text[0]["homeWork"]
    assert hw.subject == with_text[0]["subject_name"]
    assert hw.assigned == date(2026, 9, 18) and hw.due is None


def test_homework_entry_without_text_is_none():
    row = {"subject_name": "Math", "homeWork": None, "descClass": "x"}
    assert parse.parse_homework_entry(row, "k", date(2026, 1, 1)) is None
    assert parse.parse_homework_entry({**row, "homeWork": "  "}, "k", date(2026, 1, 1)) is None


def test_week_homework_walks_days_and_hours():
    r = copy.deepcopy(resp("PupilCard__GetPupilLessonsAndHomework__01"))
    assert parse.parse_homework(r, "k") == []  # real recording: nothing assigned
    day = r["data"][1]
    day["hoursData"][1]["scheduale"][0].update(homeWork="Page 5", subject_name="Math")
    (hw,) = parse.parse_homework(r, "k")
    assert hw.description == "Page 5" and hw.subject == "Math"
    assert hw.assigned == date.fromisoformat(day["date"][:10])


def test_week_homework_missing_keys_is_a_parse_error():
    r = copy.deepcopy(resp("PupilCard__GetPupilLessonsAndHomework__01"))
    del r["data"][0]["hoursData"][1]["scheduale"][0]["subject_name"]
    with pytest.raises(ParseError, match="subject_name"):
        parse.parse_homework(r, "k")


# ---- messages ----


def test_inbox_rows():
    rows = parse.parse_inbox(resp("messageBox__GetMessagesInbox__01"))
    assert len(rows) >= 3
    r = rows[0]
    assert r.message_id and isinstance(r.sent_at, datetime)
    assert isinstance(r.unread, bool) and isinstance(r.has_files, bool)
    assert r.sender_first and r.sender_last  # the `student_*_name` columns hold the sender
    raw = resp("messageBox__GetMessagesInbox__01")["data"]
    assert [x.unread for x in rows] == [m["hasRead"] == 0 for m in raw]


def test_inbox_bad_date_is_a_parse_error():
    r = copy.deepcopy(resp("messageBox__GetMessagesInbox__01"))
    r["data"][0]["sendingDate"] = "yesterday"
    with pytest.raises(ParseError, match="sendingDate"):
        parse.parse_inbox(r)


def test_message_body():
    r = copy.deepcopy(resp("messageBox__GetMessagesInboxData__01"))
    md = r["data"]["messageData"]
    md.update(
        privateName="Dana", lastName="Levi", messageContent="<p>Hello&nbsp;<b>all</b></p><p>Bye</p>"
    )
    body = parse.parse_message_body(r)
    assert body.sender == "Dana Levi"
    assert body.text == "Hello all\nBye"
    assert body.has_files is True
    assert isinstance(body.sent_at, datetime)


def test_message_body_missing_content_is_a_parse_error():
    r = copy.deepcopy(resp("messageBox__GetMessagesInboxData__01"))
    del r["data"]["messageData"]["messageContent"]
    with pytest.raises(ParseError, match="messageContent"):
        parse.parse_message_body(r)


# ---- exams, grades, discipline ----


def test_grades_split_exams_from_other_graded_items():
    r = resp("PupilCard__GetPupilGrades__01")
    exams, events = parse.parse_grades(r, "k")
    labels = [x["type"].lstrip("*") for x in r["data"]]
    n_exam = sum(1 for t in labels if t in parse.EXAM_TYPE_LABELS)
    assert len(exams) == n_exam > 0
    assert len(events) == len(labels) - n_exam
    assert all(e.kind == "grade" for e in events)
    assert exams[0].kid_id == "k" and isinstance(exams[0].day, date)


def test_grade_event_shows_the_grade_only_when_there_is_one():
    r = copy.deepcopy(resp("PupilCard__GetPupilGrades__01"))
    item = next(x for x in r["data"] if x["type"].lstrip("*") not in parse.EXAM_TYPE_LABELS)
    item["grade"] = 92
    _, events = parse.parse_grades(r, "k")
    assert any(e.detail and "92" in e.detail for e in events)
    _, recorded = parse.parse_grades(resp("PupilCard__GetPupilGrades__01"), "k")
    assert all(e.detail is None or "grade" not in e.detail.lower() for e in recorded)


def test_discipline_events_become_attendance_events():
    r = resp("PupilCard__GetPupilDiciplineEvents__01")
    events = parse.parse_discipline(r, "k")
    assert len(events) == len(r["data"]["diciplineEvents"]) > 0
    e = events[0]
    assert e.kind == "attendance" and e.kid_id == "k" and isinstance(e.day, date)
    assert e.title == r["data"]["diciplineEvents"][0]["eventType"]


def test_discipline_missing_list_is_a_parse_error():
    r = copy.deepcopy(resp("PupilCard__GetPupilDiciplineEvents__01"))
    del r["data"]["diciplineEvents"]
    with pytest.raises(ParseError, match="diciplineEvents"):
        parse.parse_discipline(r, "k")


def test_lesson_entries_include_lessons_without_homework():
    r = resp("PupilCard__GetPupilLessonsAndHomework__01")
    entries = parse.parse_lesson_entries(r)
    n = sum(len(h["scheduale"]) for d in r["data"] for h in d["hoursData"])
    assert len(entries) == n > 0
    e = entries[0]
    assert isinstance(e.day, date) and isinstance(e.hour, int) and e.subject
    assert all(x.homework is None for x in entries)  # nothing assigned in the real recording


def test_lesson_entry_needs_its_hour_number():
    r = copy.deepcopy(resp("PupilCard__GetPupilLessonsAndHomework__01"))
    del r["data"][0]["hoursData"][1]["scheduale"][0]["hour_no"]
    with pytest.raises(ParseError, match="hour_no"):
        parse.parse_lesson_entries(r)


# ---- calendar events share the weekly-schedule answer with lessons ----


def _with_other_event():
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    ev = r["data"]["data"]["events"]
    ev[0].update(
        typeId=14, eventTypeName="אחר", title="School trip", description="Bring a hat", notes=""
    )
    return r, ev


def test_other_type_events_are_calendar_events_not_lessons():
    r, ev = _with_other_event()
    lessons = parse.parse_lessons(r, "k")
    assert len(lessons) == len(ev) - 1  # the typeId 14 box is not a lesson
    events = parse.parse_calendar_events(r, "k")
    assert len(events) == 1
    e = events[0]
    assert e.kind == "calendar" and e.title == "School trip" and e.detail == "Bring a hat"
    assert e.day == date.fromisoformat(ev[0]["date"][:10]) and e.kid_id == "k"


def test_calendar_event_detail_joins_description_and_notes():
    r, ev = _with_other_event()
    ev[0]["notes"] = "Wear sneakers"
    assert parse.parse_calendar_events(r, "k")[0].detail == "Bring a hat | Wear sneakers"


def test_a_never_seen_event_type_is_still_refused_by_both_parsers():
    r, ev = _with_other_event()
    ev[0]["typeId"] = 77
    for fn in (parse.parse_lessons, parse.parse_calendar_events):
        with pytest.raises(ParseError, match="typeId 77"):
            fn(r, "k")


def test_a_week_with_events_null_is_an_empty_week():
    """Seen live: Webtop answers `events: null` (not []) for a week with nothing in it."""
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    r["data"]["data"]["events"] = None
    assert parse.parse_lessons(r, "k") == []
    assert parse.parse_calendar_events(r, "k") == []


def test_a_week_without_the_events_key_is_still_an_error():
    r = copy.deepcopy(resp("WeeklyScedule__GetEvents__01"))
    del r["data"]["data"]["events"]
    with pytest.raises(ParseError, match="events"):
        parse.parse_lessons(r, "k")


# ---- PupilCard/GetPreviousHomeworks: one subject's lesson history (shape seen live in the Android app) ----


def _prev_row(**over):
    row = {
        "subject_name": "Math",
        "subject_level": None,
        "teacher": "T. Teacher",
        "s_date": "2026-09-20T00:00:00",
        "day_no": 1,
        "hour_no": 3,
        "hour_name": "3",
        "study_group": 77,
        "descClass": "fractions",
        "homeWork": "workbook p.42",
        "statClass": True,
        "file_name": None,
        "file_name2": None,
        "file_name3": None,
        "files": [],
    }
    return {**row, **over}


def _prev(*rows):
    return {"status": True, "data": list(rows), "message": None, "errorId": None}


def test_previous_homeworks_keep_topic_text_date_and_hour():
    rows = parse.parse_previous_homeworks(
        _prev(
            _prev_row(),
            _prev_row(s_date="2026-09-13T00:00:00", homeWork="  ", descClass="intro", hour_no=2),
        )
    )
    assert rows[0] == parse.PreviousLesson(
        subject="Math",
        teacher="T. Teacher",
        day=date(2026, 9, 20),
        hour=3,
        topic="fractions",
        homework="workbook p.42",
        stat_class=True,
    )
    assert (
        rows[1].homework is None and rows[1].topic == "intro" and rows[1].day == date(2026, 9, 13)
    )


def test_previous_homeworks_empty_history_is_an_empty_list():
    assert parse.parse_previous_homeworks(_prev()) == []


def test_previous_homework_row_needs_its_text_field():
    bad = _prev_row()
    del bad["homeWork"]
    with pytest.raises(ParseError, match="homeWork"):
        parse.parse_previous_homeworks(_prev(bad))


def test_lesson_entries_carry_study_group_and_teacher():
    entries = parse.parse_lesson_entries(resp("PupilCard__GetPupilLessonsAndHomework__01"))
    assert all(isinstance(x.study_group, int) for x in entries)
    assert all(isinstance(x.teacher, str) for x in entries)


def test_previous_homework_stat_class_may_be_null():
    (row,) = parse.parse_previous_homeworks(_prev(_prev_row(statClass=None)))
    assert row.stat_class is None


# ---- WeeklyScedule/InitData: the school's off days (vacations + inactive weekdays) ----


def test_days_off_from_the_recorded_init_data():
    off = parse.parse_days_off(resp("WeeklyScedule__InitData__01"))
    assert off.inactive_weekdays == frozenset({5})  # Webtop day 7 = Saturday = Python weekday 5
    assert off.vacations  # the recording lists a vacation overlapping that week
    v = off.vacations[0]
    assert v.start <= v.end and v.name


def test_webtop_day_numbers_map_sunday_first():
    r = copy.deepcopy(resp("WeeklyScedule__InitData__01"))
    r["data"]["data"]["inactiveDays"] = [1, 6, 7]  # Sunday, Friday, Saturday
    assert parse.parse_days_off(r).inactive_weekdays == frozenset({6, 4, 5})


def test_days_off_without_vacations_or_with_null_lists_is_fine():
    r = copy.deepcopy(resp("WeeklyScedule__InitData__01"))
    r["data"]["data"]["events"]["vactionns"] = None
    r["data"]["data"]["inactiveDays"] = None
    off = parse.parse_days_off(r)
    assert off.vacations == () and off.inactive_weekdays == frozenset()


def test_a_vacation_needs_its_dates():
    r = copy.deepcopy(resp("WeeklyScedule__InitData__01"))
    del r["data"]["data"]["events"]["vactionns"][0]["endDate"]
    with pytest.raises(ParseError, match="endDate"):
        parse.parse_days_off(r)
