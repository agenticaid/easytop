"""WebtopConnector against a fake Webtop server that replays the anonymized parent recordings."""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from easytop.connector import parse
from easytop.connector.http import CONTEXT_SWITCH_ENDPOINT, READ_ENDPOINTS
from easytop.connector.webtop import WebtopConnector
from easytop.errors import KidNotFound, SessionExpired

FIXTURES = Path(__file__).parent.parent / "fixtures" / "webtop"
TODAY = date(2026, 9, 30)  # a Wednesday; its week starts Sunday 2026-09-27


def fx(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeWebtop:
    """Routes by endpoint; the answer can depend on the request body and on the webToken cookie."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []  # (endpoint, body, webToken)
        ctxs = fx("user__GetMultipleUsersForUser__01")["response"]
        self.parent_ctx = next(c for c in ctxs["data"] if c["isTeacher"] == 2)["studentId"]
        self.teacher_ctx = next(c for c in ctxs["data"] if c["isTeacher"] == 1)["studentId"]
        self.valid_tokens = {"login-token", f"token-{self.parent_ctx}"}
        self.routes = {
            "user/GetMultipleUsersForUser": lambda b, t: ctxs,
            "user/ChangeUser": self._change_user,
            "dashboard/InitDashboard": self._dashboard,
            "PupilCard/GetSettingsList": lambda b, t: fx("PupilCard__GetSettingsList__01")[
                "response"
            ],
            "WeeklyScedule/GetEvents": lambda b, t: fx("WeeklyScedule__GetEvents__01")["response"],
            "PupilCard/GetPupilGrades": lambda b, t: fx("PupilCard__GetPupilGrades__01")[
                "response"
            ],
            "PupilCard/GetPupilDiciplineEvents": lambda b, t: fx(
                "PupilCard__GetPupilDiciplineEvents__01"
            )["response"],
            "PupilCard/GetPupilLessonsAndHomework": lambda b, t: fx(
                "PupilCard__GetPupilLessonsAndHomework__01"
            )["response"],
            "PupilCard/GetPreviousHomeworks": lambda b, t: {"status": True, "data": []},
            "WeeklyScedule/InitData": lambda b, t: fx("WeeklyScedule__InitData__01")["response"],
            "messageBox/GetMessagesInbox": lambda b, t: fx("messageBox__GetMessagesInbox__01")[
                "response"
            ],
            "messageBox/GetMessagesInboxData": lambda b, t: fx(
                "messageBox__GetMessagesInboxData__01"
            )["response"],
        }

    def _change_user(self, body, token):
        r = copy.deepcopy(fx("user__ChangeUser__01")["response"])
        r["data"]["token"] = f"token-{body['StudentId']}"
        r["data"]["userType"] = 2 if body["StudentId"] == self.parent_ctx else 1
        self.valid_tokens.add(r["data"]["token"])
        return r

    def _dashboard(self, body, token):
        r = copy.deepcopy(fx("dashboard__InitDashboard__01")["response"])
        if token != f"token-{self.parent_ctx}":
            r["data"]["childrens"] = None
        return r

    def handler(self, request: httpx.Request) -> httpx.Response:
        endpoint = str(request.url).split("/server/api/")[1]
        cookies = dict(p.split("=", 1) for p in request.headers["cookie"].split("; "))
        token = cookies.get("webToken", "")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((endpoint, body, token))
        if token not in self.valid_tokens:
            return httpx.Response(401)
        return httpx.Response(200, json=self.routes[endpoint](body, token))

    def endpoints(self) -> list[str]:
        return [c[0] for c in self.calls]


def connector(server: FakeWebtop, **kw) -> WebtopConnector:
    return WebtopConnector(
        {"uniqueId": "u", "webToken": "login-token"},
        transport=httpx.MockTransport(server.handler),
        clock=lambda: TODAY,
        **kw,
    )


# ---- kids and context switching ----


async def test_list_kids_switches_only_to_the_parent_context():
    server = FakeWebtop()
    kids = await connector(server).list_kids()
    assert [k.name for k in kids] == ["Noa", "Ido", "Maya"]
    assert kids[0].school.name == "School 2" and kids[0].school.id.isdigit()
    assert kids[0].class_name == "א׳2" and kids[2].class_name == "ה׳1"
    switches = [c for c in server.calls if c[0] == CONTEXT_SWITCH_ENDPOINT]
    assert [c[1]["StudentId"] for c in switches] == [server.parent_ctx]  # never the teacher context
    # the contexts list is read with the login token, the dashboard with the parent-context token
    assert server.calls[0][2] == "login-token"
    assert [c for c in server.calls if c[0] == "dashboard/InitDashboard"][0][
        2
    ] == f"token-{server.parent_ctx}"


async def test_context_token_is_reused_not_switched_again():
    server = FakeWebtop()
    c = connector(server)
    kids = await c.list_kids()
    await c.get_schedule(kids[0], date(2026, 10, 4))
    await c.get_schedule(kids[1], date(2026, 10, 4))
    assert server.endpoints().count(CONTEXT_SWITCH_ENDPOINT) == 1


async def test_rejected_context_token_is_refreshed_once_and_the_call_retried():
    server = FakeWebtop()
    c = connector(server)
    kids = await c.list_kids()
    server.valid_tokens.discard(f"token-{server.parent_ctx}")  # server forgot the old context token
    lessons = await c.get_schedule(kids[0], date(2026, 10, 4))
    assert lessons
    assert server.endpoints().count(CONTEXT_SWITCH_ENDPOINT) == 2


async def test_expired_login_session_raises_and_fires_the_hook():
    server = FakeWebtop()
    server.valid_tokens.clear()
    fired = []
    c = connector(server, on_expired=lambda: fired.append(1))
    with pytest.raises(SessionExpired):
        await c.list_kids()
    assert fired == [1]


async def test_unknown_kid_is_refused():
    from easytop.models import Kid, School

    c = connector(FakeWebtop())
    stranger = Kid("nope", "X", "א׳1", School("1", "S"))
    with pytest.raises(KidNotFound):
        await c.get_schedule(stranger, date(2026, 10, 4))


# ---- schedule ----


async def test_schedule_sends_the_recorded_request_shape_and_filters_the_day():
    server = FakeWebtop()
    c = connector(server)
    kid = (await c.list_kids())[0]
    lessons = await c.get_schedule(kid, date(2026, 10, 4))
    body = next(b for e, b, _ in server.calls if e == "WeeklyScedule/GetEvents")
    recorded = fx("WeeklyScedule__GetEvents__01")["request"]
    assert set(body) == set(recorded)
    assert body["ChildId"] == kid.id and body["View"] == "weekly" and body["year"] == 2027
    assert (
        body["ClassCode"] == "1" and body["ClassNum"] == "2"
    )  # strings, as the web app sends them
    assert body["SelectedDate"].startswith("2026-10-04T")
    assert lessons and {x.day for x in lessons} == {date(2026, 10, 4)}
    assert all(x.kid_id == kid.id for x in lessons)


# ---- homework and equipment: the text in the weekly-schedule time boxes ----


def _events_response(boxes):
    """boxes: (date, hour, subject, description, notes) -> a GetEvents answer built on the real shape."""
    base = copy.deepcopy(fx("WeeklyScedule__GetEvents__01")["response"])
    template = base["data"]["data"]["events"][0]
    events = []
    for n, (day, hour, subject, description, notes) in enumerate(boxes):
        events.append(
            {
                **template,
                "id": 5000 + n,
                "date": f"{day}T00:00:00",
                "startTime": hour,
                "title": subject,
                "description": description,
                "notes": notes,
                "typeId": 1,
            }
        )
    base["data"]["data"]["events"] = events
    return base


def _pupil_week(entries):
    """entries: (date, hour, subject, study_group) -> a PupilCard/GetPupilLessonsAndHomework answer."""
    template = copy.deepcopy(
        fx("PupilCard__GetPupilLessonsAndHomework__01")["response"]["data"][0]["hoursData"]
    )
    template = next(e for h in template for e in h["scheduale"])
    days: dict[str, dict] = {}
    for day, hour, subject, group in entries:
        d = days.setdefault(day, {"date": f"{day}T00:00:00", "dayIndex": 1, "hoursData": []})
        entry = {**template, "subject_name": subject, "study_group": group, "hour_no": hour}
        d["hoursData"].append({"hour": hour, "hourName": None, "scheduale": [entry]})
    return {"status": True, "data": list(days.values()), "message": None, "errorId": None}


def _history(*rows):
    """rows: (date, topic, homework, stat_class) newest first, as the homework tab lists them."""
    out = []
    for day, topic, homework, stat in rows:
        out.append(
            {
                "subject_name": "x",
                "subject_level": None,
                "teacher": "T",
                "s_date": f"{day}T00:00:00",
                "day_no": 1,
                "hour_no": 1,
                "hour_name": "1",
                "study_group": 0,
                "descClass": topic,
                "homeWork": homework,
                "statClass": stat,
                "file_name": None,
                "file_name2": None,
                "file_name3": None,
                "files": [],
            }
        )
    return {"status": True, "data": out, "message": None, "errorId": None}


def _homework_server(week, histories):
    server = FakeWebtop()
    server.routes["PupilCard/GetPupilLessonsAndHomework"] = lambda b, t: week
    server.routes["PupilCard/GetPreviousHomeworks"] = lambda b, t: histories.get(
        b["studyGroup"], _history()
    )
    return server


async def test_homework_for_day_is_the_newest_earlier_lesson_of_each_subject_with_its_topic():
    week = _pupil_week(
        [
            ("2026-10-04", 1, "Math", 11),
            ("2026-10-04", 2, "Math", 11),  # double lesson: the history is read once
            ("2026-10-04", 3, "Hebrew", 12),
            ("2026-10-04", 4, "Art", 13),
            ("2026-10-05", 1, "Science", 14),  # another day: not asked
        ]
    )
    server = _homework_server(
        week,
        {
            11: _history(
                ("2026-10-04", "later lesson", "must be ignored", True),  # the day itself
                ("2026-09-27", "angles", "p.6", True),
                ("2026-09-20", "fractions", "p.5", True),
            ),
            12: _history(("2026-09-27", "reading", None, True), ("2026-09-20", "x", "old", True)),
            13: _history(),
        },
    )
    c = connector(server)
    kid = (await c.list_kids())[0]
    got = await c.get_homework_for_day(kid, date(2026, 10, 4))
    assert [(h.subject, h.description, h.topic, h.assigned) for h in got] == [
        ("Math", "p.6", "angles", date(2026, 9, 27))
    ]  # Hebrew: its newest lesson had none; Art: no history; Science: not taught that day
    weeks = [
        b["weekIndex"] for e, b, _ in server.calls if e == "PupilCard/GetPupilLessonsAndHomework"
    ]
    # 2026-10-04 is in the week after TODAY's (1); the two weeks before it are read to see which lessons
    # were held since the oldest pending homework (Hebrew's, written 20 Sep)
    assert sorted(set(weeks)) == [-1, 0, 1]
    asked = [b for e, b, _ in server.calls if e == "PupilCard/GetPreviousHomeworks"]
    assert sorted(b["studyGroup"] for b in asked) == [11, 12, 13]
    assert all(b["studentID"] == kid.id and b["studyYear"] == 2027 for b in asked)


async def test_homework_for_day_skips_lessons_that_were_not_held():
    week = _pupil_week([("2026-10-04", 1, "Math", 11)])
    server = _homework_server(
        week,
        {
            11: _history(
                ("2026-09-29", "cancelled", None, False),
                ("2026-09-27", "angles", "p.6", True),
            )
        },
    )
    c = connector(server)
    kid = (await c.list_kids())[0]
    got = await c.get_homework_for_day(kid, date(2026, 10, 4))
    assert [(h.description, h.assigned) for h in got] == [("p.6", date(2026, 9, 27))]


async def test_homework_for_day_with_nothing_written_is_empty():
    week = _pupil_week([("2026-10-04", 1, "Math", 11)])
    server = _homework_server(week, {11: _history(("2026-09-27", "angles", None, True))})
    c = connector(server)
    kid = (await c.list_kids())[0]
    assert await c.get_homework_for_day(kid, date(2026, 10, 4)) == []


async def test_homework_range_lists_the_lessons_with_text_across_all_subjects():
    week = _pupil_week([("2026-09-30", 1, "Math", 11), ("2026-09-30", 2, "Art", 13)])
    server = _homework_server(
        week,
        {
            11: _history(
                ("2026-10-02", "c", "out of range", True),
                ("2026-09-29", "b", "p.6", True),
                ("2026-09-27", "a", None, True),
            ),
            13: _history(("2026-09-28", "d", "bring clay", True)),
        },
    )
    c = connector(server)
    kid = (await c.list_kids())[0]
    got = await c.get_homework(kid, date(2026, 9, 27), date(2026, 10, 1))
    assert [(h.subject, h.description, h.assigned) for h in got] == [
        ("Art", "bring clay", date(2026, 9, 28)),
        ("Math", "p.6", date(2026, 9, 29)),
    ]


# ---- exams and events ----


async def test_exams_and_events_are_filtered_by_date_range():
    server = FakeWebtop()
    grades = copy.deepcopy(fx("PupilCard__GetPupilGrades__01")["response"])
    for i, g in enumerate(grades["data"]):
        g["date"] = f"2026-10-{10 + i:02d}T00:00:00"
    server.routes["PupilCard/GetPupilGrades"] = lambda b, t: grades
    c = connector(server)
    kid = (await c.list_kids())[0]
    exams = await c.get_exams(kid, date(2026, 10, 1), date(2026, 10, 31))
    assert exams and all(e.kid_id == kid.id for e in exams)
    assert await c.get_exams(kid, date(2026, 11, 1), date(2026, 11, 30)) == []
    events = await c.get_events(kid, date(2026, 9, 1), date(2026, 10, 31))
    kinds = {e.kind for e in events}
    assert kinds == {"grade", "attendance"}  # graded items + discipline events
    body = next(b for e, b, _ in server.calls if e == "PupilCard/GetPupilGrades")
    assert body["moduleID"] == 6
    body = next(b for e, b, _ in server.calls if e == "PupilCard/GetPupilDiciplineEvents")
    assert body["moduleID"] == 4


# ---- messages ----


async def test_messages_are_opened_by_default_which_marks_them_read():
    server = FakeWebtop()
    c = connector(server)
    msgs = await c.get_messages(None, None, False)
    rows = parse.parse_inbox(fx("messageBox__GetMessagesInbox__01")["response"])
    opened = [b["MessageId"] for e, b, _ in server.calls if e == "messageBox/GetMessagesInboxData"]
    assert set(opened) == {r.message_id for r in rows}  # Erez accepted that opening marks read
    assert len(msgs) == len(rows) and all(m.body for m in msgs)
    assert all(m.kid_id is None for m in msgs)  # the inbox is per parent, not per child
    assert msgs == sorted(msgs, key=lambda m: m.sent_at, reverse=True)


async def test_messages_can_be_listed_without_opening_the_unread_ones():
    server = FakeWebtop()
    c = connector(server, open_unread_messages=False)
    msgs = await c.get_messages(None, None, False)
    rows = parse.parse_inbox(fx("messageBox__GetMessagesInbox__01")["response"])
    opened = [b["MessageId"] for e, b, _ in server.calls if e == "messageBox/GetMessagesInboxData"]
    assert set(opened) == {r.message_id for r in rows if not r.unread}
    unread = next(r for r in rows if r.unread)
    assert {m.id: m for m in msgs}[unread.message_id].body == ""


async def test_messages_unread_only_and_since_filters():
    server = FakeWebtop()
    c = connector(server)
    rows = parse.parse_inbox(fx("messageBox__GetMessagesInbox__01")["response"])
    unread = await c.get_messages(None, None, True)
    assert {m.id for m in unread} == {r.message_id for r in rows if r.unread}
    newest = max(r.sent_at for r in rows)
    recent = await c.get_messages(None, newest, False)
    assert {m.sent_at for m in recent} == {newest}


async def test_message_sender_and_attachment_flag():
    server = FakeWebtop()
    c = connector(server)
    msgs = await c.get_messages(None, None, False)
    body = parse.parse_message_body(fx("messageBox__GetMessagesInboxData__01")["response"])
    read = next(m for m in msgs if m.body)
    assert read.sender == body.sender and isinstance(read.sent_at, datetime)
    assert read.has_attachments is body.has_files


# ---- safety ----


async def test_only_whitelisted_reads_plus_the_context_switch_are_ever_called():
    server = FakeWebtop()
    c = connector(server)
    kid = (await c.list_kids())[0]
    await c.get_schedule(kid, date(2026, 10, 4))
    await c.get_homework(kid, date(2026, 10, 4), date(2026, 10, 4))
    await c.get_homework_for_day(kid, date(2026, 10, 4))
    await c.get_homework_ahead(kid, date(2026, 10, 4))
    await c.get_exams(kid, date(2026, 9, 1), date(2026, 10, 31))
    await c.get_events(kid, date(2026, 9, 1), date(2026, 10, 31))
    await c.get_messages(None, None, False)
    assert set(server.endpoints()) <= READ_ENDPOINTS | {CONTEXT_SWITCH_ENDPOINT}


async def test_calendar_boxes_are_events_not_lessons():
    server = FakeWebtop()
    resp = _events_response(
        [("2026-10-04", 1, "Math", "p.5", ""), ("2026-10-04", 2, "Trip", "bring a hat", "")]
    )
    resp["data"]["data"]["events"][1].update(typeId=14, eventTypeName="אחר")
    server.routes["WeeklyScedule/GetEvents"] = lambda b, t: resp
    server.routes["PupilCard/GetPupilGrades"] = lambda b, t: {"status": True, "data": []}
    server.routes["PupilCard/GetPupilDiciplineEvents"] = lambda b, t: {
        "status": True,
        "data": {"diciplineEvents": []},
    }
    c = connector(server)
    kid = (await c.list_kids())[0]
    assert [x.subject for x in await c.get_schedule(kid, date(2026, 10, 4))] == ["Math"]
    events = await c.get_events(kid, date(2026, 10, 1), date(2026, 10, 10))
    assert [(e.kind, e.title, e.detail) for e in events] == [("calendar", "Trip", "bring a hat")]


async def test_homework_for_day_is_listed_in_the_order_of_the_days_lessons_with_the_hour():
    week = _pupil_week(
        [
            ("2026-10-04", 0, "Science", 21),
            ("2026-10-04", 2, "Art", 22),
            ("2026-10-04", 3, "Math", 23),
            ("2026-10-04", 4, "Math", 23),
        ]
    )
    server = _homework_server(
        week,
        {
            21: _history(("2026-09-27", "a", "read ch. 3", True)),
            22: _history(("2026-09-27", "b", "draw a tree", True)),
            23: _history(("2026-09-27", "c", "p.6", True)),
        },
    )
    c = connector(server)
    kid = (await c.list_kids())[0]
    got = await c.get_homework_for_day(kid, date(2026, 10, 4))
    assert [(h.subject, h.hour) for h in got] == [("Science", 0), ("Art", 2), ("Math", 3)]
    # alphabetical would have been Art, Math, Science


async def test_days_off_asks_init_data_once_per_week_and_merges_the_vacations():
    server = FakeWebtop()
    c = connector(server)
    kid = (await c.list_kids())[0]
    off = await c.get_days_off(kid, date(2026, 9, 27), date(2026, 10, 10))
    calls = [
        (b["SelectedDate"][:10], b) for e, b, _ in server.calls if e == "WeeklyScedule/InitData"
    ]
    assert [d for d, _ in calls] == ["2026-09-27", "2026-10-04"]  # the Sundays of the two weeks
    recorded = fx("WeeklyScedule__InitData__01")["request"]
    assert set(calls[0][1]) == set(recorded)
    assert calls[0][1]["ChildId"] == kid.id and calls[0][1]["year"] == 2027
    assert off.inactive_weekdays == frozenset({5})
    assert (
        len(off.vacations) == len(set(off.vacations)) >= 1
    )  # the same vacation twice is kept once


async def test_days_off_range_is_capped():
    c = connector(FakeWebtop())
    kid = (await c.list_kids())[0]
    with pytest.raises(ValueError):
        await c.get_days_off(kid, date(2026, 1, 1), date(2026, 12, 31))


async def test_lesson_events_read_only_the_pupil_card_lesson_events():
    server = FakeWebtop()
    c = connector(server)
    kid = (await c.list_kids())[0]
    events = await c.get_lesson_events(kid, date(2026, 1, 1), date(2026, 12, 31))
    assert events and all(e.kind == "attendance" for e in events)  # remarks only, no grades/boxes
    used = set(server.endpoints()) - {
        CONTEXT_SWITCH_ENDPOINT,
        "user/GetMultipleUsersForUser",
        "dashboard/InitDashboard",
    }
    assert used == {"PupilCard/GetSettingsList", "PupilCard/GetPupilDiciplineEvents"}


async def test_kids_carry_their_grade_from_webtops_class_code():
    kids = await connector(FakeWebtop()).list_kids()
    assert kids[0].grade == 1 and kids[2].grade == 5  # classes א׳2 and ה׳1
