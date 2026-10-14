"""Connector interface: the only thing the family service knows about Webtop."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from easytop.models import (
    DaysOff,
    Event,
    Exam,
    FileData,
    HeldLesson,
    Homework,
    Kid,
    Lesson,
    Message,
)


class Connector(Protocol):
    """Read-only access to one parent's Webtop data. Raises SessionExpired / UpstreamError / ParseError."""

    async def check_session(self) -> bool:
        """Is the stored Webtop session still alive? One cheap read call (Webtop's own CheckToken). False when
        Webtop answers that it is not; SessionExpired when it refuses the call outright."""
        ...

    async def list_kids(self) -> list[Kid]: ...

    async def get_schedule(self, kid: Kid, day: date) -> list[Lesson]: ...

    async def get_homework(self, kid: Kid, start: date, end: date) -> list[Homework]: ...

    async def get_homework_for_day(
        self, kid: Kid, day: date, now: datetime | None = None
    ) -> list[Homework]:
        """Homework to prepare for `day`: for each subject taught that day, the homework entered in its
        last class (Erez's definition of "homework for tomorrow"), unless that homework's due date, the
        subject's next lesson, already passed: a lesson of the subject was held after it and its text names
        no later date (Erez 2026-10-13). `now` is the time of the question."""
        ...

    async def get_homework_ahead(
        self,
        kid: Kid,
        day: date,
        horizon: int = 7,
        skip_days: frozenset[date] = frozenset(),
        now: datetime | None = None,
    ) -> list[Homework]:
        """Upcoming homework for the `horizon` days after `day` (Erez 2026-10-13): for each subject whose next
        lesson falls there, its latest written homework with that lesson's date as `due`, unless that due date
        already passed (as in `get_homework_for_day`). Subjects that meet on `day` are left to
        `get_homework_for_day`; `skip_days` (off days) never count as a next lesson."""
        ...

    async def get_held_lessons(self, kid: Kid, since: date, now: datetime) -> list[HeldLesson]:
        """The kid's lessons that took place from `since` through `now`: on school days only, and today's only
        once the school day is over. The model uses it to judge homework a teacher gave in a message."""
        ...

    async def get_messages(
        self, kid: Kid | None, since: datetime | None, unread_only: bool
    ) -> list[Message]: ...

    async def get_message_file(self, msg_hash: str, index: int) -> FileData:
        """One attachment of a message in the parent's own inbox, found by `easytop.files.message_hash` of the
        message id. Raises FileUnavailable (`not_found`, `too_big`, `type_refused`). Bytes in memory only."""
        ...

    async def get_exams(self, kid: Kid, start: date, end: date) -> list[Exam]: ...

    async def get_events(self, kid: Kid, start: date, end: date) -> list[Event]: ...

    async def get_days_off(self, kid: Kid, start: date, end: date) -> DaysOff | None:
        """The kid's school's own off days (weekly pattern + vacations) for start..end, or None when the
        connector has no such data (the family service then uses its default school week)."""
        ...

    async def get_lesson_events(self, kid: Kid, start: date, end: date) -> list[Event]:
        """The teachers' lesson entries on the pupil card (equipment not brought, praise, behaviour, ...)."""
        ...
