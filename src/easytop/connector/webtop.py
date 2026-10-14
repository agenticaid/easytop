"""Real Webtop connector for a PARENT account (endpoints and shapes verified by the phase 1 spike).

One login holds several school/role contexts (user/GetMultipleUsersForUser). Acting in a context means
calling user/ChangeUser, which answers with that context's token (the `webToken` cookie). The connector
keeps one client, and so one token, per parent context; teacher contexts are never entered.

Request bodies mirror what the web app sent in the recordings. Calls the recordings never made (a week
other than the current one, pages after the first) are marked UNVERIFIED where they are built.
"""

from __future__ import annotations

import asyncio
import inspect
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import PurePosixPath
from typing import Any

import httpx
from pyluach import hebrewcal

from easytop.connector import parse
from easytop.connector.duedates import explicit_due_dates
from easytop.connector.http import ExpiredHook, WebtopHttpClient
from easytop.errors import FileUnavailable, KidNotFound, ParseError, SessionExpired
from easytop.files import (
    MAX_FILE_BYTES,
    content_matches,
    extension_allowed,
    message_hash,
    mime_for,
    safe_filename,
)
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
    MessageFile,
    School,
    Vacation,
)

MAX_MESSAGES = 30  # newest first; each opened message is one more Webtop call
MAX_WEEKS = 12  # one WeeklyScedule/GetEvents request per week
# A homework older than this is not pending any more (it also bounds the weekly lists read to find held lessons).
HOMEWORK_LOOKBACK_WEEKS = 6
# Webtop's lesson list has no clock times, so a lesson of TODAY counts as held once the school day is over.
SCHOOL_OVER = time(14, 0)
WEEK_MEMO_SECONDS = 60.0  # the same week is read by several homework calls of one brief


@dataclass(frozen=True)
class _Known:
    """What the connector remembers about a kid: which context it lives in and its raw class data."""

    ctx: parse.Context
    child: parse.Child


def _study_year(day: date) -> int:
    """School year as Webtop numbers it: the year it ends in (Sept 2026 - June 2027 -> 2027)."""
    return day.year + 1 if day.month >= 9 else day.year


def _file_ext(f: parse.ParsedFile) -> str:
    """The extension Webtop lists for an attachment, else the one in its name."""
    return f.ext or PurePosixPath(f.name).suffix


def _size_kb(size_bytes: int) -> int:
    return max(1, round(size_bytes / 1024)) if size_bytes else 0


def _week_start(day: date) -> date:
    """Sunday on or before `day` (the Israeli school week starts on Sunday)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


class WebtopConnector:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], date] = date.today,
        on_expired: ExpiredHook | None = None,
        open_unread_messages: bool = True,
    ):
        """`cookies` are the stored session cookies, including the login context's `webToken`.

        open_unread_messages: opening a message in Webtop marks it read (observed in the recordings), a
        visible change in the parent's account. Erez accepted that (2026-10-01), so it is on by default;
        set False to list unread messages without their text.
        """
        self._root = WebtopHttpClient(cookies, transport=transport)
        self._clients: dict[
            str, WebtopHttpClient
        ] = {}  # context student_id -> client with its token
        self._settings: dict[str, parse.Settings] = {}
        self._kids: dict[str, _Known] = {}
        self._parent_contexts: list[parse.Context] = []
        self._week_memo: dict[tuple[str, date], tuple[float, list[parse.LessonEntry]]] = {}
        self._clock = clock
        self._on_expired = on_expired
        self._open_unread = open_unread_messages
        self._switch_lock = asyncio.Lock()

    async def aclose(self) -> None:
        for c in (self._root, *self._clients.values()):
            await c.aclose()

    # ---- plumbing ----

    async def _expired(self) -> None:
        if self._on_expired:
            res = self._on_expired()
            if inspect.isawaitable(res):
                await res

    async def _root_call(self, endpoint: str, body: dict[str, Any]) -> Any:
        try:
            return await self._root.call(endpoint, body)
        except SessionExpired:
            await self._expired()
            raise

    async def check_session(self) -> bool:
        """dashboard/CheckToken: {status: true, data: true} while the session lives, data false once it is gone.
        Unlike every other call this does not fire the expiry hook: the health check decides what dead means."""
        answer = await self._root.call(
            "dashboard/CheckToken", {}
        )  # a refusal raises SessionExpired
        return (
            isinstance(answer, dict) and answer.get("status") is True and answer.get("data") is True
        )

    async def _client(
        self, ctx: parse.Context, *, refresh: bool = False
    ) -> tuple[WebtopHttpClient, bool]:
        """The client holding `ctx`'s token, and whether it was just created by a context switch."""
        async with self._switch_lock:
            if refresh or ctx.student_id not in self._clients:
                try:
                    user = await self._root.switch_context(ctx.student_id)
                except SessionExpired:
                    await self._expired()
                    raise
                if not user.is_parent:
                    raise ParseError("ChangeUser: expected a parent context")
                self._clients[ctx.student_id] = self._root.with_token(user.token)
                return self._clients[ctx.student_id], True
            return self._clients[ctx.student_id], False

    async def _call(self, ctx: parse.Context, endpoint: str, body: dict[str, Any]) -> Any:
        client, fresh = await self._client(ctx)
        try:
            return await client.call(endpoint, body)
        except SessionExpired:
            if fresh:
                await self._expired()
                raise
        # an older context token was refused: switch again once and retry
        client, _ = await self._client(ctx, refresh=True)
        try:
            return await client.call(endpoint, body)
        except SessionExpired:
            await self._expired()
            raise

    async def _known(self, kid: Kid) -> _Known:
        if kid.id not in self._kids:
            await self.list_kids()
        try:
            return self._kids[kid.id]
        except KeyError:
            raise KidNotFound(f"kid {kid.id!r} is not in this parent's list") from None

    async def _settings_for(self, ctx: parse.Context) -> parse.Settings:
        if ctx.student_id not in self._settings:
            resp = await self._call(ctx, "PupilCard/GetSettingsList", {})
            self._settings[ctx.student_id] = parse.parse_settings(resp)
        return self._settings[ctx.student_id]

    @staticmethod
    def _division_on(settings: parse.Settings, day: date) -> parse.Division:
        for d in settings.divisions:
            if d.start <= day <= d.end:
                return d
        current = [d for d in settings.divisions if d.current]
        if current:
            return current[0]
        raise ParseError("GetSettingsList: no school period covers that date")

    @staticmethod
    def _card_body(
        k: _Known, s: parse.Settings, division: parse.Division, module: str, week_index: int = 0
    ) -> dict[str, Any]:
        """Body of the PupilCard/* calls, as the web app sends it."""
        if module not in s.modules:
            raise ParseError(f"GetSettingsList: module {module!r} is not offered to this account")
        return {
            "weekIndex": week_index,
            "viewType": 0,
            "studyYear": s.study_year,
            "studyYearName": hebrewcal.Year(s.study_year + 3760).year_string(),
            "studentID": k.child.id,
            "studentName": f"{k.child.first_name} {k.child.last_name}",
            "classCode": k.child.class_code,
            "periodID": division.id,
            "periodName": division.name,
            "moduleID": s.modules[module],
        }

    # ---- Connector interface ----

    async def list_kids(self) -> list[Kid]:
        contexts = parse.parse_contexts(await self._root_call("user/GetMultipleUsersForUser", {}))
        self._parent_contexts = [c for c in contexts if c.is_parent]
        found: list[tuple[parse.Context, parse.Child]] = []
        for ctx in self._parent_contexts:
            found += [
                (ctx, ch)
                for ch in parse.parse_children(await self._call(ctx, "dashboard/InitDashboard", {}))
            ]
        first_names = [ch.first_name for _, ch in found]
        kids, known = [], {}
        for ctx, ch in found:
            name = (
                ch.first_name
                if first_names.count(ch.first_name) == 1
                else f"{ch.first_name} {ch.last_name}"
            )
            kid = Kid(
                id=ch.id,
                name=name,
                class_name=parse.class_label(ch.class_code, ch.class_num),
                school=School(id=str(ctx.institution_code), name=ctx.school_name),
                grade=ch.class_code,
            )
            kids.append(kid)
            known[kid.id] = _Known(ctx, ch)
        self._kids = known
        return kids

    async def _week_boxes(
        self, kid: Kid, start: date, end: date
    ) -> tuple[list[Lesson], list[Event]]:
        """Lesson boxes and calendar boxes dated start..end, one WeeklyScedule/GetEvents request per
        week (the Sunday as the date; past weeks verified). Each box's free text (homework, equipment to
        bring, class topic) is kept as the note/detail."""
        k = await self._known(kid)
        week = _week_start(start)
        if (end - week).days // 7 >= MAX_WEEKS:
            raise ValueError(f"range longer than {MAX_WEEKS} weeks")
        lessons: dict[Lesson, None] = {}
        events: dict[Event, None] = {}
        while week <= end:
            body = {
                "InstitutionCode": k.ctx.institution_code,
                "SelectedDate": f"{week.isoformat()}T09:00:00.000Z",
                "View": "weekly",
                "ClassNum": str(k.child.class_num),
                "ClassCode": str(k.child.class_code),
                "ChildId": k.child.id,
                "SpaceId": 0,
                "language": "he",
                "year": _study_year(week),
                "getBySendDate": True,
                "readFromMaster": False,
            }
            resp = await self._call(k.ctx, "WeeklyScedule/GetEvents", body)
            lessons.update(
                dict.fromkeys(x for x in parse.parse_lessons(resp, kid.id) if start <= x.day <= end)
            )
            events.update(
                dict.fromkeys(
                    x for x in parse.parse_calendar_events(resp, kid.id) if start <= x.day <= end
                )
            )
            week += timedelta(days=7)
        return sorted(lessons, key=lambda x: (x.day, x.period)), sorted(events, key=lambda x: x.day)

    async def _lessons_between(self, kid: Kid, start: date, end: date) -> list[Lesson]:
        return (await self._week_boxes(kid, start, end))[0]

    async def get_schedule(self, kid: Kid, day: date) -> list[Lesson]:
        return await self._lessons_between(kid, day, day)

    # ---- homework: the pupil card's homework tab (PupilCard/GetPupilLessonsAndHomework + GetPreviousHomeworks)

    async def _pupil_week(self, kid: Kid, day: date) -> list[parse.LessonEntry]:
        """The lessons of the week containing `day` with their study groups. weekIndex is the offset from
        the current week (0 this week, 1 next week; -2..1 verified live)."""
        key = (kid.id, _week_start(day))
        memo = self._week_memo.get(key)
        if memo and _time.monotonic() - memo[0] < WEEK_MEMO_SECONDS:
            return memo[1]
        k = await self._known(kid)
        s = await self._settings_for(k.ctx)
        offset = (_week_start(day) - _week_start(self._clock())).days // 7
        body = self._card_body(k, s, self._division_on(s, day), "lessonSubjectHomework", offset)
        resp = await self._call(k.ctx, "PupilCard/GetPupilLessonsAndHomework", body)
        entries = parse.parse_lesson_entries(resp)
        self._week_memo[key] = (_time.monotonic(), entries)
        return entries

    async def _history(self, kid: Kid, study_group: int) -> list[parse.PreviousLesson]:
        """One subject's past lessons, newest first (the order is not relied on)."""
        k = await self._known(kid)
        s = await self._settings_for(k.ctx)
        body = {"studyYear": s.study_year, "studyGroup": study_group, "studentID": k.child.id}
        resp = await self._call(k.ctx, "PupilCard/GetPreviousHomeworks", body)
        return parse.parse_previous_homeworks(resp)

    async def _histories(
        self, kid: Kid, entries: list[parse.LessonEntry]
    ) -> list[tuple[str, int, list[parse.PreviousLesson]]]:
        """(subject, first hour on the entries' days, history) per study group, in lesson order."""
        first = self._first_per_group(entries)
        histories = await asyncio.gather(*(self._history(kid, g) for g in first))
        return [
            (e.subject, e.hour, rows) for e, rows in zip(first.values(), histories, strict=True)
        ]

    @staticmethod
    def _first_per_group(entries: list[parse.LessonEntry]) -> dict[int, parse.LessonEntry]:
        """The earliest listed lesson of each study group, in lesson order."""
        first: dict[int, parse.LessonEntry] = {}
        for e in sorted(entries, key=lambda x: (x.day, x.hour)):
            first.setdefault(e.study_group, e)
        return first

    @staticmethod
    def _held(rows: list[parse.PreviousLesson]) -> list[parse.PreviousLesson]:
        """Lessons that took place. `statClass` False is read as not held (cancelled): never seen with
        homework text; null is kept. UNVERIFIED meaning of the flag."""
        return [r for r in rows if r.stat_class is not False]

    @staticmethod
    def _as_homework(
        kid: Kid, subject: str, r: parse.PreviousLesson, hour: int | None = None
    ) -> Homework:
        return Homework(
            kid.id, subject, r.homework or "", due=None, assigned=r.day, hour=hour, topic=r.topic
        )

    async def get_homework(self, kid: Kid, start: date, end: date) -> list[Homework]:
        """Homework texts written in lessons dated start..end, for the subjects of the week containing
        `start` (Webtop has no due dates)."""
        entries = await self._pupil_week(kid, start)
        out = [
            self._as_homework(kid, subject, r)
            for subject, _hour, rows in await self._histories(kid, entries)
            for r in self._held(rows)
            if start <= r.day <= end and r.homework
        ]
        return sorted(out, key=lambda h: (h.assigned, h.subject))

    async def _held_entries(self, kid: Kid, since: date, now: datetime) -> list[parse.LessonEntry]:
        """The listed lessons from `since` through `now` that took place: on the school's own school days (the
        vacation lessons in the list never happened) and, for today, only once the school day is over (the list
        has no clock times). The homework tab only has rows for lessons a teacher filled in, so this is how a
        lesson without a row is known to have been held."""
        today = now.date()
        weeks, w = [], _week_start(since)
        while w <= today:
            weeks.append(w)
            w += timedelta(days=7)
        off = await self.get_days_off(kid, since, today)
        lists = await asyncio.gather(*(self._pupil_week(kid, w) for w in weeks))
        over = now.time() >= SCHOOL_OVER
        return [
            e
            for week in lists
            for e in week
            if since <= e.day <= today and not off.covers(e.day) and (e.day < today or over)
        ]

    async def get_held_lessons(self, kid: Kid, since: date, now: datetime) -> list[HeldLesson]:
        """Lessons of the kid that took place from `since` through `now` (see `_held_entries`). A lesson that
        was cancelled but is still listed shows here: only the homework tab's rows say so."""
        held = await self._held_entries(kid, since, now)
        unique = {(e.day, e.hour, e.subject) for e in held}
        return [HeldLesson(kid.id, d, h, s) for d, h, s in sorted(unique)]

    async def _pending(
        self,
        kid: Kid,
        pairs: list[tuple[parse.LessonEntry, list[parse.PreviousLesson]]],
        now: datetime,
    ) -> list[tuple[parse.LessonEntry, parse.PreviousLesson]]:
        """For each (the lesson a homework is due at, that subject's history) the homework still waiting for it.

        Webtop has no due dates: homework is written into a lesson and is due at the subject's NEXT lesson. So
        the newest written homework is pending only if no lesson of the subject was held after it, whether the
        homework tab has a row for that lesson or only the weekly list shows it (Erez 2026-10-13: Sunday's
        Math homework, Math held on Monday, asked on Monday afternoon -> not shown). A date the teacher wrote
        in the text itself keeps it alive until that date; a wrong reading of a date can only keep a line."""
        today = now.date()
        oldest = today - timedelta(weeks=HOMEWORK_LOOKBACK_WEEKS)
        found = []
        for entry, rows in pairs:
            usable = [r for r in self._held(rows) if r.day <= today and r.day < entry.day]
            written = [r for r in usable if r.homework and r.day >= oldest]
            if written:
                found.append((entry, rows, usable, max(written, key=lambda r: (r.day, r.hour))))
        if not found:
            return []
        held = await self._held_entries(kid, min(row.day for *_, row in found), now)
        out = []
        for entry, rows, usable, row in found:
            cancelled = {(r.day, r.hour) for r in rows if r.stat_class is False}
            passed = any(r.day > row.day for r in usable) or any(
                e.study_group == entry.study_group
                and e.day > row.day
                and (e.day, e.hour) not in cancelled
                for e in held
            )
            named_later = any(d >= today for d in explicit_due_dates(row.homework or "", row.day))
            if named_later or not passed:
                out.append((entry, row))
        return out

    async def get_homework_for_day(
        self, kid: Kid, day: date, now: datetime | None = None
    ) -> list[Homework]:
        """Homework to prepare for `day` (Erez's rule): for each subject taught that day, the newest written
        homework, if its due date (the subject's next lesson) has not passed (see `_pending`). `now` is the
        time of the question; without it no lesson of today counts as held."""
        now = now or datetime.combine(self._clock(), time.min)
        entries = [e for e in await self._pupil_week(kid, day) if e.day == day]
        first = self._first_per_group(entries)
        histories = await asyncio.gather(*(self._history(kid, g) for g in first))
        pending = await self._pending(kid, list(zip(first.values(), histories, strict=True)), now)
        out = [self._as_homework(kid, e.subject, row, e.hour) for e, row in pending]
        return sorted(out, key=lambda h: (h.hour if h.hour is not None else 99, h.subject))

    async def get_homework_ahead(
        self,
        kid: Kid,
        day: date,
        horizon: int = 7,
        skip_days: frozenset[date] = frozenset(),
        now: datetime | None = None,
    ) -> list[Homework]:
        """Upcoming homework after `day` (Erez 2026-10-13). Homework is written into a lesson and is due at
        that subject's NEXT lesson, so for every subject whose next lesson falls in (day, day + horizon]
        the newest held lesson's homework is returned with that next lesson's date as `due`. A subject
        that meets on `day` is skipped (its pending homework is `get_homework_for_day`'s result), and
        `skip_days` (the school's off days) never count as a next lesson."""
        now = now or datetime.combine(self._clock(), time.min)
        end = day + timedelta(days=horizon)
        weeks, w = [], _week_start(day)
        while w <= end:
            weeks.append(w)
            w += timedelta(days=7)
        entries = [
            e
            for week in await asyncio.gather(*(self._pupil_week(kid, w) for w in weeks))
            for e in week
        ]
        meets_on_day = {e.study_group for e in entries if e.day == day}
        next_lesson: dict[int, parse.LessonEntry] = {}
        for e in sorted(entries, key=lambda x: (x.day, x.hour)):
            if day < e.day <= end and e.day not in skip_days and e.study_group not in meets_on_day:
                next_lesson.setdefault(e.study_group, e)
        histories = await asyncio.gather(*(self._history(kid, g) for g in next_lesson))
        pending = await self._pending(
            kid, list(zip(next_lesson.values(), histories, strict=True)), now
        )
        out = [
            Homework(
                kid.id,
                e.subject,
                row.homework or "",
                due=e.day,
                assigned=row.day,
                hour=e.hour,
                topic=row.topic,
            )
            for e, row in pending
        ]
        return sorted(out, key=lambda h: (h.due, h.hour if h.hour is not None else 99, h.subject))

    async def get_days_off(self, kid: Kid, start: date, end: date) -> DaysOff:
        """The school's own off days from WeeklyScedule/InitData (one request per week; each answer lists
        the vacations overlapping that week plus the weekdays that never have school)."""
        k = await self._known(kid)
        week = _week_start(start)
        if (end - week).days // 7 >= MAX_WEEKS:
            raise ValueError(f"range longer than {MAX_WEEKS} weeks")
        weeks = []
        while week <= end:
            weeks.append(week)
            week += timedelta(days=7)

        async def one(week: date) -> DaysOff:
            body = {
                "InstitutionCode": k.ctx.institution_code,
                "SelectedDate": f"{week.isoformat()}T09:00:00.000Z",
                "View": "weekly",
                "spaceId": 0,
                "language": "he",
                "item": "",
                "ClassNum": str(k.child.class_num),
                "ClassCode": str(k.child.class_code),
                "ChildId": k.child.id,
                "year": _study_year(week),
            }
            return parse.parse_days_off(await self._call(k.ctx, "WeeklyScedule/InitData", body))

        inactive: set[int] = set()
        vacations: dict[Vacation, None] = {}
        for off in await asyncio.gather(*(one(w) for w in weeks)):
            inactive |= off.inactive_weekdays
            vacations.update(dict.fromkeys(off.vacations))
        return DaysOff(
            frozenset(inactive), tuple(sorted(vacations, key=lambda v: (v.start, v.end, v.name)))
        )

    async def get_messages(
        self, kid: Kid | None, since: datetime | None, unread_only: bool
    ) -> list[Message]:
        """Newest first. The inbox belongs to the parent's login in a school, not to a child: Webtop's
        `student_*_name` columns are the sender, so messages carry no kid (kid_id None). With `kid`, only
        that kid's school inbox is read."""
        if not self._kids:
            await self.list_kids()
        contexts = [(await self._known(kid)).ctx] if kid else self._parent_contexts
        inbox_body = {"PageId": 1, "LabelId": 0, "HasRead": None, "SearchQuery": ""}
        out: list[Message] = []
        for ctx in contexts:
            rows = parse.parse_inbox(
                await self._call(ctx, "messageBox/GetMessagesInbox", inbox_body)
            )
            rows = [
                r
                for r in rows
                if (not since or r.sent_at >= since) and (r.unread or not unread_only)
            ]
            rows = sorted(rows, key=lambda r: r.sent_at, reverse=True)[:MAX_MESSAGES]
            out += await asyncio.gather(*(self._message(ctx, r) for r in rows))
        return sorted(out, key=lambda m: m.sent_at, reverse=True)

    async def _open(self, ctx: parse.Context, message_id: str) -> parse.MessageBody:
        """Open one message (this marks it read in Webtop, accepted 2026-10-01)."""
        body_req = {"MessageId": message_id, "FilterId": 0, "IsInbox": True, "hasRead": None}
        return parse.parse_message_body(
            await self._call(ctx, "messageBox/GetMessagesInboxData", body_req)
        )

    async def _message(self, ctx: parse.Context, row: parse.InboxRow) -> Message:
        sender, text, has_files = f"{row.sender_first} {row.sender_last}".strip(), "", row.has_files
        files: tuple[MessageFile, ...] = ()
        if not row.unread or self._open_unread:
            body = await self._open(ctx, row.message_id)
            sender, text, has_files = body.sender, body.text, body.has_files
            files = tuple(
                MessageFile(f.index, f.name, _file_ext(f), _size_kb(f.size_bytes)) for f in body.files
            )
        return Message(
            id=row.message_id,
            kid_id=None,
            sender=sender,
            subject=row.subject,
            body=text,
            sent_at=row.sent_at,
            unread=row.unread,
            has_attachments=has_files,
            files=files,
        )

    async def get_message_file(self, msg_hash: str, index: int) -> FileData:
        """One attachment of a message of the parent's own inbox, found by `message_hash` of its id (the file
        link carries only that hash, so nothing is remembered on the server and a message of another family
        cannot match). Raises FileUnavailable (`not_found`, `too_big`, `type_refused`). The bytes are returned
        for one reply and never kept."""
        if not self._kids:
            await self.list_kids()
        inbox_body = {"PageId": 1, "LabelId": 0, "HasRead": None, "SearchQuery": ""}
        for ctx in self._parent_contexts:
            rows = parse.parse_inbox(await self._call(ctx, "messageBox/GetMessagesInbox", inbox_body))
            row = next((r for r in rows if r.has_files and message_hash(r.message_id) == msg_hash), None)
            if row is None:
                continue
            listed = next((f for f in (await self._open(ctx, row.message_id)).files if f.index == index), None)
            if listed is None:
                raise FileUnavailable("not_found")
            return await self._fetch_file(ctx, listed)
        raise FileUnavailable("not_found")

    async def _fetch_file(self, ctx: parse.Context, f: parse.ParsedFile) -> FileData:
        ext = _file_ext(f)
        if not extension_allowed(ext):
            raise FileUnavailable("type_refused")
        if f.size_bytes > MAX_FILE_BYTES:
            raise FileUnavailable("too_big")
        data = await self._download(ctx, f.url)
        if not content_matches(ext, data[:64]):
            raise FileUnavailable("type_refused")
        return FileData(safe_filename(f.name, ext), data, mime_for(ext))

    async def _download(self, ctx: parse.Context, url: str) -> bytes:
        """Like `_call`: an older context token that is refused is switched once and tried again."""
        client, fresh = await self._client(ctx)
        try:
            return await client.download(url, max_bytes=MAX_FILE_BYTES)
        except SessionExpired:
            if fresh:
                await self._expired()
                raise
        client, _ = await self._client(ctx, refresh=True)
        try:
            return await client.download(url, max_bytes=MAX_FILE_BYTES)
        except SessionExpired:
            await self._expired()
            raise

    async def _grades(self, kid: Kid, start: date, end: date) -> tuple[list[Exam], list[Event]]:
        k = await self._known(kid)
        s = await self._settings_for(k.ctx)
        divisions = [d for d in s.divisions if d.start <= end and d.end >= start] or [
            self._division_on(s, start)
        ]
        exams: list[Exam] = []
        events: list[Event] = []
        for d in divisions:
            resp = await self._call(
                k.ctx, "PupilCard/GetPupilGrades", self._card_body(k, s, d, "grades")
            )
            e, ev = parse.parse_grades(resp, kid.id)
            exams += [x for x in e if start <= x.day <= end]
            events += [x for x in ev if start <= x.day <= end]
        return exams, events

    async def get_exams(self, kid: Kid, start: date, end: date) -> list[Exam]:
        """Evaluations whose type label is an exam or written quiz. The school-wide "changes and exams"
        tab (shotef) is switched off for the recorded account and its shape was never seen."""
        exams, _ = await self._grades(kid, start, end)
        return sorted(dict.fromkeys(exams), key=lambda x: x.day)

    async def get_lesson_events(self, kid: Kid, start: date, end: date) -> list[Event]:
        """The teachers' lesson entries (PupilCard "אירועי שיעור"): equipment not brought, praise,
        behaviour, attendance. Only this endpoint is called."""
        k = await self._known(kid)
        s = await self._settings_for(k.ctx)
        divisions = [d for d in s.divisions if d.start <= end and d.end >= start] or [
            self._division_on(s, start)
        ]
        remarks: list[Event] = []
        for d in divisions:
            resp = await self._call(
                k.ctx, "PupilCard/GetPupilDiciplineEvents", self._card_body(k, s, d, "lessonEvents")
            )
            remarks += [x for x in parse.parse_discipline(resp, kid.id) if start <= x.day <= end]
        return sorted(remarks, key=lambda x: x.day)

    async def get_events(self, kid: Kid, start: date, end: date) -> list[Event]:
        """Graded items (non-exam), behaviour / equipment / attendance remarks, and calendar boxes."""
        _, graded = await self._grades(kid, start, end)
        _, calendar = await self._week_boxes(kid, start, end)
        remarks = await self.get_lesson_events(kid, start, end)
        return sorted([*graded, *remarks, *calendar], key=lambda x: x.day)
