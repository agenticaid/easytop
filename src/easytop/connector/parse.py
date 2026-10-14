"""Strict parsers for the Webtop PARENT API. Pure functions: JSON in, normalized models out.

Shapes come from the anonymized recordings in tests/fixtures/webtop (phase 1 spike, Erez's account).
A field we rely on that is missing or has the wrong type raises ParseError, so an API change fails
loudly instead of producing a silently wrong answer. Extra fields are ignored.

What the recordings could NOT show (every list was empty, so no shape is guessed):
- schedule changes / cancellations, and the school "changes and exams" tab (shotef, permissions off);
- event types other than a lesson (typeId 1): they are refused, not skipped;
- a non-empty numeric grade.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from easytop.errors import ParseError, UpstreamError
from easytop.models import DaysOff, Event, Exam, Homework, Lesson, Vacation

# user/GetMultipleUsersForUser `isTeacher` (also `userType` after ChangeUser): 1 teacher, 2 parent.
USER_TYPE_PARENT = 2
# WeeklyScedule/GetEvents `typeId`: 1 = lesson (label "שיעור"); 14 = calendar event labelled "אחר"
# ("other"), same keys as a lesson. Any other value is refused, never silently dropped.
LESSON_TYPE_ID = 1
CALENDAR_TYPE_IDS = frozenset({14})
# PupilCard/GetPupilGrades `type` labels (a leading "*" is stripped) treated as exams.
EXAM_TYPE_LABELS = frozenset({"מבחן", "בוחן בכתב"})

_GRADES = ["א", "ב", "ג", "ד", "ה", "ו", "ז", "ח", "ט", "י", "יא", "יב"]


# ---- helpers ----


def _get(
    d: Any, key: str, typ: type | tuple[type, ...], where: str, *, nullable: bool = False
) -> Any:
    if not isinstance(d, dict):
        raise ParseError(f"{where}: expected an object, got {type(d).__name__}")
    if key not in d:
        raise ParseError(f"{where}: missing '{key}'")
    v = d[key]
    if v is None and nullable:
        return None
    types = typ if isinstance(typ, tuple) else (typ,)
    if isinstance(v, bool) and bool not in types:
        raise ParseError(f"{where}: '{key}' should be {_names(types)}, got bool")
    if not isinstance(v, types):
        raise ParseError(f"{where}: '{key}' should be {_names(types)}, got {type(v).__name__}")
    return v


def _names(types: tuple[type, ...]) -> str:
    return "/".join(t.__name__ for t in types)


def _list(d: Any, key: str, where: str, *, nullable: bool = False) -> list[Any]:
    v = _get(d, key, list, where, nullable=nullable)
    return [] if v is None else v


def _as_list(v: Any, where: str) -> list[Any]:
    if not isinstance(v, list):
        raise ParseError(f"{where}: expected a list, got {type(v).__name__}")
    return v


def _when(d: Any, key: str, where: str) -> datetime:
    raw = _get(d, key, str, where)
    try:
        return datetime.fromisoformat(raw)
    except ValueError as e:
        raise ParseError(f"{where}: '{key}' is not an ISO date-time") from e


def _day(d: Any, key: str, where: str) -> date:
    return _when(d, key, where).date()


def unwrap(resp: Any) -> Any:
    """Webtop wraps answers as {status, data, message, errorId, ...}. Return `data`."""
    if not isinstance(resp, dict) or "status" not in resp or "data" not in resp:
        raise ParseError("not a Webtop envelope (expected status + data)")
    if resp["status"] is not True:
        raise UpstreamError(
            f"Webtop answered status={resp['status']!r} (errorId={resp.get('errorId')})"
        )
    return resp["data"]


def html_to_text(s: str) -> str:
    """Message bodies are HTML (assumed from the field name; the recorded text was replaced)."""
    s = re.sub(r"(?i)</(p|div|li|tr)>|<br\s*/?>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n"))
    return "\n".join(line for line in lines if line)


def class_label(class_code: int, class_num: int) -> str:
    """Grade + class number as parents write it, e.g. (1, 2) -> 'א׳2'. classCode is the grade 1-12."""
    if not 1 <= class_code <= len(_GRADES):
        raise ParseError(f"class_label: unknown grade code {class_code}")
    return f"{_GRADES[class_code - 1]}׳{class_num}"


# ---- contexts and kids ----


@dataclass(frozen=True)
class SwitchedUser:
    """user/ChangeUser answer: the context we now act as, and its token (the new `webToken`)."""

    token: str
    is_parent: bool
    institution_code: int


def parse_change_user(resp: Any) -> SwitchedUser:
    data = unwrap(resp)
    w = "ChangeUser"
    return SwitchedUser(
        token=_get(data, "token", str, w),
        is_parent=_get(data, "userType", int, w) == USER_TYPE_PARENT,
        institution_code=_get(data, "institutionCode", int, w),
    )


@dataclass(frozen=True)
class Context:
    """One school/role the login can act as (user/GetMultipleUsersForUser)."""

    student_id: str  # what user/ChangeUser takes as StudentId
    institution_code: int
    school_name: str
    is_parent: bool


@dataclass(frozen=True)
class Child:
    id: str
    first_name: str
    last_name: str
    class_code: int
    class_num: int


def parse_contexts(resp: Any) -> list[Context]:
    data = _as_list(unwrap(resp), "GetMultipleUsersForUser")
    out = []
    for i, c in enumerate(data):
        w = f"GetMultipleUsersForUser[{i}]"
        out.append(
            Context(
                student_id=_get(c, "studentId", str, w),
                institution_code=_get(c, "institutionCode", int, w),
                school_name=_get(c, "school_name", str, w),
                is_parent=_get(c, "isTeacher", int, w) == USER_TYPE_PARENT,
            )
        )
    return out


def parse_children(resp: Any) -> list[Child]:
    """dashboard/InitDashboard in a parent context. `childrens` is null in a teacher context."""
    data = unwrap(resp)
    out = []
    for i, c in enumerate(_list(data, "childrens", "InitDashboard", nullable=True)):
        w = f"InitDashboard.childrens[{i}]"
        out.append(
            Child(
                id=_get(c, "id", str, w),
                first_name=_get(c, "firstName", str, w),
                last_name=_get(c, "lastName", str, w),
                class_code=_get(c, "classCode", int, w),
                class_num=_get(c, "classNum", int, w),
            )
        )
    return out


@dataclass(frozen=True)
class Division:
    id: int
    name: str
    start: date
    end: date
    current: bool


@dataclass(frozen=True)
class Settings:
    study_year: int
    modules: dict[str, int]  # universalTitle -> module id (e.g. "scheduale" -> 10)
    divisions: list[Division]


def parse_settings(resp: Any) -> Settings:
    """PupilCard/GetSettingsList: the module ids and school periods the other PupilCard calls need."""
    data = unwrap(resp)
    modules = {}
    for i, m in enumerate(_list(data, "modulesList", "GetSettingsList")):
        w = f"GetSettingsList.modulesList[{i}]"
        modules[_get(m, "universalTitle", str, w)] = _get(m, "id", int, w)
    divisions = []
    for i, d in enumerate(_list(data, "divisionsList", "GetSettingsList")):
        w = f"GetSettingsList.divisionsList[{i}]"
        divisions.append(
            Division(
                id=_get(d, "division_id", int, w),
                name=_get(d, "division_name", str, w),
                start=_day(d, "from_date", w),
                end=_day(d, "to_date", w),
                current=_get(d, "isCurrent", int, w) == 1,
            )
        )
    return Settings(_get(data, "currentStudyYear", int, "GetSettingsList"), modules, divisions)


# ---- lessons ----


def _week_events(resp: Any) -> list[Any]:
    data = unwrap(resp)
    # Webtop answers `events: null` for a week with nothing in it (seen live)
    return _list(_get(data, "data", dict, "GetEvents"), "events", "GetEvents.data", nullable=True)


def _text(e: Any, w: str) -> str | None:
    parts = [
        t.strip()
        for t in (
            _get(e, "description", str, w, nullable=True),
            _get(e, "notes", str, w, nullable=True),
        )
        if t and t.strip()
    ]
    return " | ".join(parts) or None


def _type_id(e: Any, w: str) -> int:
    type_id = _get(e, "typeId", int, w)
    if type_id != LESSON_TYPE_ID and type_id not in CALENDAR_TYPE_IDS:
        raise ParseError(f"{w}: unknown typeId {type_id} (recorded: 1=lesson, 14=other)")
    return type_id


def parse_lessons(resp: Any, kid_id: str) -> list[Lesson]:
    """WeeklyScedule/GetEvents for one child and one week: the lesson boxes. The box text (homework,
    equipment to bring, class topic) becomes Lesson.note. Clock times are not in this API."""
    out = []
    for i, e in enumerate(_week_events(resp)):
        w = f"GetEvents.events[{i}]"
        if _type_id(e, w) != LESSON_TYPE_ID:
            continue
        out.append(
            Lesson(
                kid_id=kid_id,
                day=_day(e, "date", w),
                period=_get(e, "startTime", int, w),
                start=None,
                end=None,
                subject=_get(e, "title", str, w),
                teacher=(_get(e, "teacher", str, w, nullable=True) or "").strip() or None,
                note=_text(e, w),
            )
        )
    return out


def parse_calendar_events(resp: Any, kid_id: str) -> list[Event]:
    """The non-lesson boxes of the same answer (typeId 14, "other": trips, tests, notices...)."""
    out = []
    for i, e in enumerate(_week_events(resp)):
        w = f"GetEvents.events[{i}]"
        if _type_id(e, w) in CALENDAR_TYPE_IDS:
            out.append(
                Event(
                    kid_id=kid_id,
                    kind="calendar",
                    day=_day(e, "date", w),
                    title=_get(e, "title", str, w),
                    detail=_text(e, w),
                )
            )
    return out


# ---- homework ----


def parse_homework_entry(entry: Any, kid_id: str, day: date) -> Homework | None:
    """One lesson entry (PupilCard lessons-and-homework / previous-homeworks). None if no homework text.

    Webtop attaches homework to the lesson it was written in; there is no due date in the data.
    """
    w = "homework entry"
    subject = _get(entry, "subject_name", str, w)
    text = _get(entry, "homeWork", str, w, nullable=True)
    if not text or not text.strip():
        return None
    return Homework(
        kid_id=kid_id, subject=subject, description=text.strip(), due=None, assigned=day
    )


@dataclass(frozen=True)
class LessonEntry:
    """One lesson occurrence from PupilCard/GetPupilLessonsAndHomework: which subject, teacher and study
    group (the key of GetPreviousHomeworks) is taught in which hour. `homework` is empty in the weekly
    list for the recorded school; the real text comes from GetPreviousHomeworks (parse_previous_homeworks)."""

    day: date
    hour: int
    subject: str
    teacher: str
    study_group: int
    homework: str | None


def parse_lesson_entries(resp: Any) -> list[LessonEntry]:
    """A week of days -> hours -> lesson entries. Every lesson is listed, homework or not."""
    out = []
    for i, d in enumerate(_as_list(unwrap(resp), "LessonsAndHomework")):
        w = f"LessonsAndHomework[{i}]"
        day = _day(d, "date", w)
        for h in _list(d, "hoursData", w):
            for entry in _list(h, "scheduale", f"{w}.hoursData"):
                text = _get(entry, "homeWork", str, w, nullable=True)
                out.append(
                    LessonEntry(
                        day=day,
                        hour=_get(entry, "hour_no", int, w),
                        subject=_get(entry, "subject_name", str, w),
                        teacher=(_get(entry, "teacher", str, w, nullable=True) or "").strip(),
                        study_group=_get(entry, "study_group", int, w),
                        homework=text.strip() if text and text.strip() else None,
                    )
                )
    return out


@dataclass(frozen=True)
class PreviousLesson:
    """One past lesson of a subject (PupilCard/GetPreviousHomeworks): class topic and homework text.
    `stat_class` is Webtop's `statClass` flag; its exact meaning (lesson held?) is unverified."""

    subject: str
    teacher: str
    day: date
    hour: int
    topic: str | None
    homework: str | None
    stat_class: bool | None


def _text_or_none(e: Any, key: str, w: str) -> str | None:
    v = _get(e, key, str, w, nullable=True)
    return v.strip() if v and v.strip() else None


def parse_previous_homeworks(resp: Any) -> list[PreviousLesson]:
    """The lesson history of ONE subject / study group, as the homework tab lists it (newest first in the
    app; callers must sort by date, not rely on the order)."""
    out = []
    for i, e in enumerate(_as_list(unwrap(resp), "GetPreviousHomeworks")):
        w = f"GetPreviousHomeworks[{i}]"
        stat = _get(e, "statClass", (bool, int), w, nullable=True)  # null seen live
        out.append(
            PreviousLesson(
                subject=_get(e, "subject_name", str, w),
                teacher=(_get(e, "teacher", str, w, nullable=True) or "").strip(),
                day=_day(e, "s_date", w),
                hour=_get(e, "hour_no", int, w),
                topic=_text_or_none(e, "descClass", w),
                homework=_text_or_none(e, "homeWork", w),
                stat_class=None if stat is None else bool(stat),
            )
        )
    return out


def parse_homework(resp: Any, kid_id: str) -> list[Homework]:
    """Entries that carry homework text, as Homework (assigned = the lesson's date, no due date)."""
    return [
        Homework(kid_id=kid_id, subject=e.subject, description=e.homework, due=None, assigned=e.day)
        for e in parse_lesson_entries(resp)
        if e.homework
    ]


# ---- messages ----


@dataclass(frozen=True)
class InboxRow:
    message_id: str
    subject: str
    sent_at: datetime
    unread: bool
    has_files: bool
    sender_first: str  # Webtop names these columns student_F_name / student_L_name, but they hold
    sender_last: str  # the SENDER (equal to the message body's sender in the recording)


MAX_FILES_PER_MESSAGE = 10  # a start parameter carries a one-digit file index; the most seen is 5
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


@dataclass(frozen=True)
class ParsedFile:
    """One attachment as the opened message lists it. `url` is the download address exactly as listed (checked
    only when downloading) and must never reach the model or a log."""

    index: int  # position in the message's own list
    name: str
    ext: str  # as listed, e.g. ".pdf"; "" when none
    size_bytes: int  # 0 when unknown
    url: str


@dataclass(frozen=True)
class MessageBody:
    sender: str
    subject: str
    text: str
    sent_at: datetime
    has_files: bool
    files: tuple[ParsedFile, ...] = ()


def parse_inbox(resp: Any) -> list[InboxRow]:
    out = []
    for i, r in enumerate(_as_list(unwrap(resp), "GetMessagesInbox")):
        w = f"GetMessagesInbox[{i}]"
        out.append(
            InboxRow(
                message_id=_get(r, "messageId", str, w),
                subject=_get(r, "subject", str, w),
                sent_at=_when(r, "sendingDate", w),
                unread=_get(r, "hasRead", int, w) == 0,
                has_files=_get(r, "filesWereAttached", int, w) > 0,
                sender_first=_get(r, "student_F_name", str, w),
                sender_last=_get(r, "student_L_name", str, w),
            )
        )
    return out


def parse_message_body(resp: Any) -> MessageBody:
    md = _get(unwrap(resp), "messageData", dict, "GetMessagesInboxData")
    w = "GetMessagesInboxData.messageData"
    sender = f"{_get(md, 'privateName', str, w)} {_get(md, 'lastName', str, w)}".strip()
    return MessageBody(
        sender=sender,
        subject=_get(md, "subject", str, w),
        text=html_to_text(_get(md, "messageContent", str, w)),
        sent_at=_when(md, "sendingDate", w),
        has_files=_get(md, "filesWereAttached", int, w) > 0,
        files=_parse_files(md),
    )


def _parse_files(md: dict[str, Any]) -> tuple[ParsedFile, ...]:
    """`messageData.filesList`. Lenient on purpose: an odd entry costs only that file, never the message text."""
    raw = md.get("filesList")
    if not isinstance(raw, list):
        return ()
    out = []
    for i, f in enumerate(raw[:MAX_FILES_PER_MESSAGE]):
        if not isinstance(f, dict):
            continue
        name, url = f.get("fileName"), f.get("fileUrl")
        if not isinstance(name, str) or not isinstance(url, str) or not url:
            continue
        ext = f.get("fileExtantion")  # (sic) Webtop's spelling
        size = f.get("fileSize")
        size = size if isinstance(size, dict) else {}
        amount, unit = size.get("size"), _SIZE_UNITS.get(size.get("sizeName"))
        known = isinstance(amount, int | float) and not isinstance(amount, bool) and unit
        out.append(
            ParsedFile(
                index=i,
                name=name,
                ext=ext if isinstance(ext, str) else "",
                size_bytes=round(amount * unit) if known else 0,
                url=url,
            )
        )
    return tuple(out)


# ---- exams, grades, discipline ----


def parse_grades(resp: Any, kid_id: str) -> tuple[list[Exam], list[Event]]:
    """PupilCard/GetPupilGrades: dated evaluations. Exam-type labels become Exams, the rest Events."""
    exams: list[Exam] = []
    events: list[Event] = []
    for i, g in enumerate(_as_list(unwrap(resp), "GetPupilGrades")):
        w = f"GetPupilGrades[{i}]"
        if _get(g, "isDeleted", int, w):
            continue
        label = _get(g, "type", str, w).lstrip("*").strip()
        day = _day(g, "date", w)
        subject = _get(g, "subject", str, w)
        title = (_get(g, "title", str, w, nullable=True) or "").strip() or None
        if label in EXAM_TYPE_LABELS:
            exams.append(Exam(kid_id=kid_id, subject=subject, day=day, topic=title))
            continue
        grade = _get(g, "grade", (int, float, str), w, nullable=True)
        remark = (_get(g, "remark", str, w, nullable=True) or "").strip() or None
        detail = "; ".join(
            x for x in (title, f"grade {grade}" if grade is not None else None, remark) if x
        )
        events.append(
            Event(
                kid_id=kid_id,
                kind="grade",
                day=day,
                title=f"{subject}: {label}",
                detail=detail or None,
            )
        )
    return exams, events


def parse_discipline(resp: Any, kid_id: str) -> list[Event]:
    """PupilCard/GetPupilDiciplineEvents: behaviour / equipment / attendance remarks."""
    out = []
    for i, e in enumerate(_list(unwrap(resp), "diciplineEvents", "GetPupilDiciplineEvents")):
        w = f"GetPupilDiciplineEvents[{i}]"
        parts = [
            (_get(e, "subjectName", str, w, nullable=True) or "").strip(),
            (_get(e, "remark", str, w, nullable=True) or "").strip(),
        ]
        out.append(
            Event(
                kid_id=kid_id,
                kind="attendance",
                day=_day(e, "eventDate", w),
                title=_get(e, "eventType", str, w),
                detail="; ".join(p for p in parts if p) or None,
            )
        )
    return out


# ---- the school's off days: WeeklyScedule/InitData ----


def parse_days_off(resp: Any) -> DaysOff:
    """`inactiveDays` are Webtop weekday numbers, Sunday = 1 .. Saturday = 7 (seen live: [7] for a school
    with Friday lessons). `events.vactionns` lists the vacations that overlap the requested week."""
    inner = _get(unwrap(resp), "data", dict, "InitData")
    numbers = _list(inner, "inactiveDays", "InitData.data", nullable=True)
    weekdays = set()
    for n in numbers:
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 7:
            raise ParseError(f"InitData.inactiveDays: expected 1..7, got {n!r}")
        weekdays.add((n - 2) % 7)  # Sunday 1 -> Python 6, Saturday 7 -> 5
    events = _get(inner, "events", dict, "InitData.data")
    vacations = []
    for i, v in enumerate(_list(events, "vactionns", "InitData.data.events", nullable=True)):
        w = f"InitData.vactionns[{i}]"
        vacations.append(
            Vacation(
                _day(v, "startDate", w),
                _day(v, "endDate", w),
                (_get(v, "name", str, w, nullable=True) or "").strip() or "Vacation",
            )
        )
    return DaysOff(frozenset(weekdays), tuple(vacations))
