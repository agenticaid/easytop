"""Normalized models. The rest of the system never sees raw Webtop JSON."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

LessonStatus = Literal["normal", "cancelled", "substitute", "room_changed"]
EventKind = Literal["grade", "attendance", "calendar"]


@dataclass(frozen=True)
class School:
    id: str
    name: str


@dataclass(frozen=True)
class Kid:
    id: str
    name: str
    class_name: str
    school: School
    grade: int | None = None  # 1-12 (Webtop's class code); None when unknown


@dataclass(frozen=True)
class Lesson:
    kid_id: str
    day: date
    period: int
    start: time | None
    end: time | None
    subject: str
    teacher: str | None = None
    room: str | None = None
    status: LessonStatus = "normal"
    note: str | None = None
    last_period: int | None = None  # set when consecutive boxes were merged into one double lesson


@dataclass(frozen=True)
class Homework:
    kid_id: str
    subject: str
    description: str
    # Webtop has no due dates: homework is written into the lesson's entry. Set only for UPCOMING homework, where it
    # is derived: the date of the subject's next lesson (get_homework_ahead).
    due: date | None = None
    assigned: date | None = None
    topic: str | None = None  # what the class was about, as the teacher wrote it next to it
    hour: int | None = None  # the lesson hour it belongs to on the day asked about (for ordering)


@dataclass(frozen=True)
class MessageFile:
    """An attachment as the parent sees it in a list: name, type and size. The download address stays inside
    the connector (it holds a token); a file is fetched again by the hash of its message id (easytop.files)."""

    index: int  # position in the message's own list (what the file link carries)
    name: str
    ext: str  # ".pdf"; "" when Webtop lists none
    size_kb: int  # 0 when unknown


@dataclass(frozen=True)
class FileData:
    """A downloaded attachment, held in memory for one reply only: never cached, stored or logged."""

    name: str  # safe to hand to a chat app
    data: bytes
    mime: str


@dataclass(frozen=True)
class FileOffer:
    """What the reply needs to list one file of a message the answer cites, as a link (Spec 14). Code-made from
    the tool result; the model never sees it."""

    name: str
    ext: str
    size_kb: int
    sender: str
    sent: str  # day.month of the message, e.g. "4.10"
    payload: str  # the start parameter of the file link (easytop.files.file_payload)


@dataclass(frozen=True)
class Message:
    id: str
    kid_id: str | None
    sender: str
    subject: str
    body: str
    sent_at: datetime
    unread: bool = True
    has_attachments: bool = False
    files: tuple[MessageFile, ...] = ()


@dataclass(frozen=True)
class Exam:
    kid_id: str
    subject: str
    day: date
    topic: str | None = None


@dataclass(frozen=True)
class Event:
    kid_id: str
    kind: EventKind
    day: date
    title: str
    detail: str | None = None


@dataclass(frozen=True)
class Vacation:
    start: date
    end: date
    name: str


@dataclass(frozen=True)
class DaysOff:
    """A school's own off days as Webtop lists them: weekdays that never have school (Python weekday
    numbers, Monday 0 .. Sunday 6) and vacation ranges (holidays included)."""

    inactive_weekdays: frozenset[int]
    vacations: tuple[Vacation, ...]

    def covers(self, day: date) -> bool:
        """Is `day` an off day by the school's own data (a weekday without school, or inside a vacation)?"""
        return day.weekday() in self.inactive_weekdays or any(
            v.start <= day <= v.end for v in self.vacations
        )


@dataclass(frozen=True)
class HeldLesson:
    """A lesson that already took place (the homework tab's hour numbers equal the timetable's periods)."""

    kid_id: str
    day: date
    hour: int
    subject: str
