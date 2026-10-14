"""Family service: the seven tools the agent calls. Talks only to a Connector.

Returns trimmed, JSON-serializable dicts (never raw Webtop JSON). One kid or school failing never
blocks the others: failures are reported per kid. Built on synthetic data — needs spike verification.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from easytop.connector.base import Connector
from easytop.connector.duedates import explicit_due_dates
from easytop.errors import EasytopError, KidNotFound, SessionExpired
from easytop.family.cache import TtlCache
from easytop.family.calendar import SchoolCalendar, SchoolDay
from easytop.files import file_payload
from easytop.models import (
    Event,
    Exam,
    FileData,
    HeldLesson,
    Homework,
    Kid,
    Lesson,
    Message,
)

# measured 2026-10-13 on a real inbox: median 113 chars, max 1070 (a date in the tail counts)
MAX_MESSAGE_CHARS = 1500
MESSAGE_WINDOW_DAYS = 7  # how far back the inbox is read; every answer says so (Spec 13)
LESSON_EVENT_DAYS = 14  # how far back the teachers' lesson entries are shown
# upcoming homework: subjects whose next lesson is within this many days after the brief's day
HOMEWORK_AHEAD_DAYS = 7
# Answer categories (Erez 2026-10-13: Homework, Equipment, Exams, Day flow) -> the per-kid brief parts each one
# needs. The inbox is NOT a part: it is material for every category, so it is in every brief (Spec 13).
FOCUS: dict[str, set[str]] = {
    "homework": {"homework", "homework_upcoming", "lesson_events"},
    "equipment": {"lessons", "calendar", "lesson_events"},
    "exams": {"exams", "calendar"},
    "flow": {"lessons", "changes", "calendar"},
}
# Two focuses for the evening reminder's content, asked by code only (the tool schema does not offer them): what is
# due tomorrow, and every part of the day. Neither reads the inbox: opening a message marks it read in Webtop, and
# a timer must not do that behind the parent's back (Erez 2026-10-13).
FOCUS["due"] = {"homework", "exams"}
FOCUS["digest"] = set().union(*FOCUS.values())
NO_INBOX_FOCUS = frozenset({"due", "digest"})
_WEEKDAY_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Hebrew day words as written in Israeli school messages (Sunday first = "ראשון").
_WEEKDAY_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def _d(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def merge_double_lessons(lessons: list[Lesson]) -> list[Lesson]:
    """Webtop lists a two-hour lesson as two boxes in consecutive hours. Merge boxes of the same subject,
    teacher and status in consecutive hours into one lesson (`last_period` set, distinct notes joined)."""
    out: list[Lesson] = []
    for x in sorted(lessons, key=lambda y: y.period):
        prev = out[-1] if out else None
        if (
            prev
            and prev.subject == x.subject
            and prev.teacher == x.teacher
            and prev.status == x.status
            and x.period == (prev.last_period or prev.period) + 1
        ):
            notes = [n for n in (prev.note, x.note) if n]
            note = " | ".join(dict.fromkeys(notes)) or None
            out[-1] = replace(prev, last_period=x.period, end=x.end or prev.end, note=note)
        else:
            out.append(x)
    return out


def lesson_out(x: Lesson) -> dict[str, Any]:
    return _clean(
        {
            "period": x.period,
            "hours": f"{x.period}-{x.last_period}" if x.last_period else None,
            "start": x.start.strftime("%H:%M") if x.start else None,
            "end": x.end.strftime("%H:%M") if x.end else None,
            "subject": x.subject,
            "teacher": x.teacher,
            "room": x.room,
            "status": x.status,
            "note": x.note,
        }
    )


ELEMENTARY_GRADES = range(1, 7)  # א׳-ו׳


def lessons_out(kid: Kid, lessons: list[Lesson]) -> list[dict[str, Any]]:
    """The day's lessons for the model. In elementary school (grades 1-6) hour 0 is not a lesson: teachers
    use it for a general note, which in practice opens the day with a short activity before the first
    lesson. It is returned as a `day_opener` (no period number) and the first lesson after it is marked
    `after_day_opener`. For older kids, or an unknown grade, hour 0 stays a normal lesson."""
    elementary = kid.grade in ELEMENTARY_GRADES
    out: list[dict[str, Any]] = []
    marked = False
    for x in lessons:
        item = lesson_out(x)
        if elementary and x.period == 0:
            item.pop("period", None)
            item = {"day_opener": True, **item}
        elif elementary and not marked and any(y.period == 0 for y in lessons):
            item["after_day_opener"] = True
            marked = True
        out.append(item)
    return out


def homework_out(x: Homework) -> dict[str, Any]:
    return _clean(
        {
            "subject": x.subject,
            "description": x.description,
            "topic": x.topic,
            "hour": x.hour,
            "due": x.due.isoformat() if x.due else None,
            "assigned": x.assigned.isoformat() if x.assigned and not x.due else None,
        }
    )


def upcoming_out(x: Homework) -> dict[str, Any]:
    """Homework due on a later day: `due` is the date of the subject's next lesson, `assigned` the lesson it
    was written in."""
    return _clean(
        {
            "subject": x.subject,
            "description": x.description,
            "topic": x.topic,
            "hour": x.hour,
            "due": x.due.isoformat() if x.due else None,
            "assigned": x.assigned.isoformat() if x.assigned else None,
        }
    )


def exam_out(x: Exam) -> dict[str, Any]:
    return _clean({"subject": x.subject, "date": x.day.isoformat(), "topic": x.topic})


def event_out(x: Event) -> dict[str, Any]:
    return _clean({"kind": x.kind, "date": x.day.isoformat(), "title": x.title, "detail": x.detail})


def mentions_day(text: str, day: date, today: date, sent: date) -> bool:
    """Does a message refer to `day`? Heuristic: d.m / d/m, weekday names, 'tomorrow' if sent today."""
    low = text.lower()
    if re.search(rf"(?<!\d){day.day}[./]{day.month}(?!\d)", low):
        return True
    if _WEEKDAY_EN[day.weekday()] in low:
        return True
    if re.search(rf"יום\s*{_WEEKDAY_HE[day.weekday()]}", text):
        return True
    return (
        sent == today and day == today + timedelta(days=1) and ("tomorrow" in low or "מחר" in text)
    )


class FamilyService:
    def __init__(
        self,
        connector: Connector,
        *,
        clock: Callable[[], datetime] = datetime.now,
        calendar: SchoolCalendar | None = None,
        cache_ttl: float = 300.0,
    ):
        self._c = connector
        self._clock = clock
        self._cal = calendar or SchoolCalendar()
        self._cache = TtlCache(clock, cache_ttl)
        self._kids: list[Kid] | None = None
        self._refs: dict[str, str] = {}  # message id -> the short ref the model cites (m1, m2, ...)

    # ---- kid handling (kid ids only ever come from the parent's own list) ----
    async def _own_kids(self) -> list[Kid]:
        if self._kids is None:
            self._kids = await self._cache.get_or_fetch(("kids",), self._c.list_kids)
        return self._kids

    async def _resolve(self, kid: str | None) -> list[Kid]:
        kids = await self._own_kids()
        if kid is None:
            return kids
        for k in kids:
            if kid == k.id or kid.strip().casefold() == k.name.casefold():
                return [k]
        raise KidNotFound(f"unknown kid: {kid!r}")

    @staticmethod
    def _ident(k: Kid) -> dict[str, Any]:
        return {"id": k.id, "name": k.name, "class": k.class_name, "school": k.school.name}

    def _envelope(self, kids: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "source": "Webtop",
            "checked_at": self._clock().isoformat(timespec="minutes"),
            "session_expired": any(k.get("error") == "session_expired" for k in kids),
            "kids": kids,
            **extra,
        }

    async def _fanout(
        self, kids: list[Kid], fn: Callable[[Kid], Awaitable[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        async def one(k: Kid) -> dict[str, Any]:
            try:
                return {**self._ident(k), "ok": True, **await fn(k)}
            except SessionExpired:
                return {**self._ident(k), "ok": False, "error": "session_expired"}
            except EasytopError as e:
                return {**self._ident(k), "ok": False, "error": "unavailable", "detail": str(e)}

        return list(await asyncio.gather(*(one(k) for k in kids)))

    # ---- cached connector reads ----
    async def _schedule(self, k: Kid, day: date) -> list[Lesson]:
        async def fetch() -> list[Lesson]:
            return merge_double_lessons(await self._c.get_schedule(k, day))

        return await self._cache.get_or_fetch(("sched", k.id, day), fetch)

    async def _homework(self, k: Kid, a: date, b: date) -> list[Homework]:
        return await self._cache.get_or_fetch(
            ("hw", k.id, a, b), lambda: self._c.get_homework(k, a, b)
        )

    async def _exams(self, k: Kid, a: date, b: date) -> list[Exam]:
        return await self._cache.get_or_fetch(
            ("ex", k.id, a, b), lambda: self._c.get_exams(k, a, b)
        )

    async def _events(self, k: Kid, a: date, b: date) -> list[Event]:
        return await self._cache.get_or_fetch(
            ("ev", k.id, a, b), lambda: self._c.get_events(k, a, b)
        )

    async def _lesson_events(self, k: Kid, a: date, b: date) -> list[Event]:
        return await self._cache.get_or_fetch(
            ("lev", k.id, a, b), lambda: self._c.get_lesson_events(k, a, b)
        )

    async def _days_off(self, k: Kid, today: date):
        return await self._cache.get_or_fetch(
            ("daysoff", k.id, today),
            lambda: self._c.get_days_off(k, today, today + timedelta(days=28)),
        )

    async def _homework_for_day(self, k: Kid, day: date) -> list[Homework]:
        # the question time lets the connector tell which lessons were held since a homework was given
        return await self._cache.get_or_fetch(
            ("hwday", k.id, day), lambda: self._c.get_homework_for_day(k, day, self._clock())
        )

    async def _homework_ahead(self, k: Kid, day: date, skip: frozenset[date]) -> list[Homework]:
        return await self._cache.get_or_fetch(
            ("hwahead", k.id, day),
            lambda: self._c.get_homework_ahead(k, day, HOMEWORK_AHEAD_DAYS, skip, self._clock()),
        )

    async def _held_lessons(self, k: Kid, since: date) -> list[HeldLesson]:
        return await self._cache.get_or_fetch(
            ("held", k.id, since), lambda: self._c.get_held_lessons(k, since, self._clock())
        )

    async def _messages(self, k: Kid | None, since: datetime | None, unread: bool) -> list[Message]:
        key = ("msg", k.id if k else None, since, unread)
        return await self._cache.get_or_fetch(key, lambda: self._c.get_messages(k, since, unread))

    def _window_start(self) -> datetime:
        """Midnight at the start of the 7-day inbox window."""
        return datetime.combine(
            self._clock().date() - timedelta(days=MESSAGE_WINDOW_DAYS), datetime.min.time()
        )

    async def _inbox(self) -> tuple[list[Message], str | None]:
        """The whole family's inbox for the window, once (siblings in one school share it). A failure here is
        reported beside the kids and never blocks them."""
        try:
            return await self._messages(None, self._window_start(), False), None
        except SessionExpired:
            return [], "session_expired"
        except EasytopError:
            return [], "unavailable"

    # ---- the seven tools ----
    async def list_kids(self) -> dict[str, Any]:
        return {"kids": [self._ident(k) for k in await self._own_kids()]}

    async def school_tomorrow(self) -> bool:
        """True if any kid has school tomorrow (the nudge is skipped otherwise). Reads only the kids and
        their days off: no schedule, homework or messages. A kid whose days off cannot be read does not
        count; if none can be read at all the error is raised."""
        today = self._clock().date()
        tomorrow = today + timedelta(days=1)

        async def has_school(k: Kid) -> bool:
            return self._cal.next_school_day(today, await self._days_off(k, today)).day == tomorrow

        results = await asyncio.gather(
            *(has_school(k) for k in await self._own_kids()), return_exceptions=True
        )
        if any(r is True for r in results):
            return True
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        return False

    async def get_tomorrow_brief(
        self, kid: str | None = None, focus: str | None = None
    ) -> dict[str, Any]:
        """Next school day for each kid. `focus` limits it to one answer category and skips the fetches
        the others need: homework (the homework tab), equipment (lesson box text + messages), flow
        (lessons, changes, times, exams + messages). None = everything."""
        if focus is not None and focus not in FOCUS:
            raise ValueError(f"focus must be one of {list(FOCUS)}")
        kids = await self._resolve(kid)
        today = self._clock().date()
        want = FOCUS[focus] if focus else set().union(*FOCUS.values())
        school_days: dict[str, SchoolDay] = {}  # kid id -> that kid's next school day

        async def brief(k: Kid) -> dict[str, Any]:
            async def none() -> list:
                return []

            # each kid's school has its own week and vacations (a failure here is this kid's failure)
            off = await self._days_off(k, today)
            nxt = self._cal.next_school_day(today, off)
            school_days[k.id] = nxt
            day = nxt.day

            async def upcoming() -> tuple[list[Homework], str | None]:
                """Later days' homework. Its failure never hides tomorrow's (only an expired session does)."""
                if "homework_upcoming" not in want:
                    return [], None
                later = (day + timedelta(days=i) for i in range(1, HOMEWORK_AHEAD_DAYS + 1))
                skip = frozenset(d for d in later if not self._cal.is_school_day(d, off))
                try:
                    return await self._homework_ahead(k, day, skip), None
                except SessionExpired:
                    raise
                except EasytopError:
                    return [], "unavailable"

            (
                lessons,
                homework,
                exams,
                lesson_events,
                (ahead, ahead_error),
                events,
            ) = await asyncio.gather(
                self._schedule(k, day) if "lessons" in want else none(),
                self._homework_for_day(k, day) if "homework" in want else none(),
                self._exams(k, day, day + timedelta(days=7)) if "exams" in want else none(),
                self._lesson_events(k, today - timedelta(days=LESSON_EVENT_DAYS), today)
                if "lesson_events" in want
                else none(),
                upcoming(),
                self._events(k, day, day) if "calendar" in want else none(),
            )
            active = [x for x in lessons if x.status != "cancelled" and x.end]
            out: dict[str, Any] = {}
            if "lessons" in want:
                out["lessons"] = lessons_out(k, lessons)
            if "changes" in want:
                out["changes"] = [lesson_out(x) for x in lessons if x.status != "normal"]
                out["starts_at"] = (
                    active[0].start.strftime("%H:%M") if active and active[0].start else None
                )
                out["ends_at"] = max(x.end for x in active).strftime("%H:%M") if active else None
            if "homework" in want:
                out["homework_due"] = [homework_out(x) for x in homework]
            if "homework_upcoming" in want:
                if ahead_error:
                    out["homework_upcoming_error"] = ahead_error
                else:
                    out["homework_upcoming"] = [upcoming_out(x) for x in ahead]
            if "exams" in want:
                out["upcoming_exams"] = [exam_out(x) for x in exams]
            if "calendar" in want:  # trips and 'other' boxes of the day: not lessons, not grades
                out["calendar"] = [event_out(x) for x in events if x.kind == "calendar"]
            if "lesson_events" in want:  # newest first
                out["recent_lesson_events"] = [
                    {
                        "date": x.day.isoformat(),
                        "type": x.title,
                        **({"detail": x.detail} if x.detail else {}),
                    }
                    for x in sorted(lesson_events, key=lambda e: e.day, reverse=True)
                ]
            return _clean(out)

        # messages can assign homework or equipment only in these categories: only then are the lessons
        # held since each message worked out (see `_inbox_out`)
        judge_messages = focus in (None, "homework", "equipment")
        read_inbox = focus not in NO_INBOX_FOCUS
        results, (inbox, inbox_error), held = await asyncio.gather(
            self._fanout(kids, brief),
            self._inbox() if read_inbox else self._no_inbox(),
            self._held_by_kid(kids) if judge_messages else self._nothing(),
        )
        first = min(school_days.values(), key=lambda d: d.day, default=None)
        if first is None:  # every kid failed before a day was known: the default school week
            first = self._cal.next_school_day(today)
        for r in results:  # a kid whose next school day differs from the earliest one says so
            nxt = school_days.get(r["id"])
            if nxt is not None and nxt.day != first.day:
                r["date"], r["weekday"] = nxt.day.isoformat(), nxt.day.strftime("%A")
        messages = self._inbox_out(inbox, first.day, today, kids, held)
        extra: dict[str, Any] = (
            {"messages": messages, **self._offers_entry(messages, inbox)} if read_inbox else {}
        )
        if inbox_error:
            extra["messages_error"] = inbox_error
            extra["session_expired"] = inbox_error == "session_expired" or any(
                k.get("error") == "session_expired" for k in results
            )
        return self._envelope(
            results,
            date=first.day.isoformat(),
            weekday=first.day.strftime("%A"),
            skipped=[{"date": d.isoformat(), "reason": r} for d, r in first.skipped],
            **({"focus": focus} if focus else {}),
            **extra,
        )

    @staticmethod
    async def _no_inbox() -> tuple[list[Message], str | None]:
        return [], None

    @staticmethod
    async def _nothing() -> dict[str, list[HeldLesson]]:
        return {}

    async def _held_by_kid(self, kids: list[Kid]) -> dict[str, list[HeldLesson]]:
        """The lessons each child had in the message window. A lookup that fails only means the messages
        carry no `lessons_held_since`: the items then show, which errs towards showing."""
        since = self._clock().date() - timedelta(days=MESSAGE_WINDOW_DAYS)

        async def one(k: Kid) -> tuple[str, list[HeldLesson]]:
            try:
                return k.id, await self._held_lessons(k, since)
            except EasytopError:
                return k.id, []

        return dict(await asyncio.gather(*(one(k) for k in kids)))

    def _inbox_out(
        self,
        msgs: list[Message],
        day: date,
        today: date,
        kids: list[Kid] | None = None,
        held: dict[str, list[HeldLesson]] | None = None,
    ) -> list[dict[str, Any]]:
        """The inbox for the model: identical messages collapsed (same sender, subject and text; the newest
        stays), messages that name the brief's day first and then the newest. Nothing is dropped by a guess
        about what a message is about: the model files every item under a category (Spec 13).

        A message that names a subject, and gives no date still ahead, gets `lessons_held_since`: per child
        the subjects it names that had a lesson on a later day than the message. Homework or equipment it
        gives for such a subject is past (due at the subject's next lesson, Erez 2026-10-13)."""
        names = {k.id: k.name for k in self._kids or []}
        seen: set[tuple[str, str, str]] = set()
        kept: list[Message] = []
        for m in sorted(msgs, key=lambda x: x.sent_at, reverse=True):
            key = (m.sender, m.subject, " ".join(m.body.split()).casefold())
            if key not in seen:
                seen.add(key)
                kept.append(m)
        kept.sort(
            key=lambda m: (
                not mentions_day(f"{m.subject} {m.body}", day, today, m.sent_at.date()),
                -m.sent_at.timestamp(),
            )
        )
        out = []
        for m in kept:
            item = self._msg_out(m, names)
            since = self._lessons_held_since(m, kids or [], held or {}, today)
            if since:
                item["lessons_held_since"] = since
            out.append(item)
        return out

    @staticmethod
    def _lessons_held_since(
        m: Message, kids: list[Kid], held: dict[str, list[HeldLesson]], today: date
    ) -> dict[str, list[str]]:
        text = f"{m.subject} {m.body}"
        sent = m.sent_at.date()
        if any(d >= today for d in explicit_due_dates(text, sent)):
            return {}  # the message names a date that is still ahead: it is not past
        found = {}
        for k in kids:
            subjects = {x.subject for x in held.get(k.id, []) if x.day > sent}
            named = sorted(s for s in subjects if s.casefold() in text.casefold())
            if named:
                found[k.name] = named
        return found

    async def get_schedule(self, kid: str, day: date | str) -> dict[str, Any]:
        kids, d = await self._resolve(kid), _d(day)

        async def f(k: Kid) -> dict[str, Any]:
            return {"lessons": lessons_out(k, await self._schedule(k, d))}

        return self._envelope(await self._fanout(kids, f), date=d.isoformat())

    async def get_homework(self, kid: str, start: date | str, end: date | str) -> dict[str, Any]:
        kids, a, b = await self._resolve(kid), _d(start), _d(end)

        async def f(k: Kid) -> dict[str, Any]:
            return {"homework": [homework_out(x) for x in await self._homework(k, a, b)]}

        return self._envelope(
            await self._fanout(kids, f), **{"from": a.isoformat(), "to": b.isoformat()}
        )

    async def get_exams(self, kid: str, start: date | str, end: date | str) -> dict[str, Any]:
        kids, a, b = await self._resolve(kid), _d(start), _d(end)

        async def f(k: Kid) -> dict[str, Any]:
            return {"exams": [exam_out(x) for x in await self._exams(k, a, b)]}

        return self._envelope(
            await self._fanout(kids, f), **{"from": a.isoformat(), "to": b.isoformat()}
        )

    async def get_events(self, kid: str, start: date | str, end: date | str) -> dict[str, Any]:
        kids, a, b = await self._resolve(kid), _d(start), _d(end)

        async def f(k: Kid) -> dict[str, Any]:
            return {"events": [event_out(x) for x in await self._events(k, a, b)]}

        return self._envelope(
            await self._fanout(kids, f), **{"from": a.isoformat(), "to": b.isoformat()}
        )

    async def get_messages(
        self, kid: str | None = None, since: datetime | str | None = None, unread: bool = False
    ) -> dict[str, Any]:
        kids = await self._own_kids()
        names = {k.id: k.name for k in kids}
        target = (await self._resolve(kid))[0] if kid is not None else None
        since_dt = datetime.fromisoformat(since) if isinstance(since, str) else since
        if since_dt is not None:  # message times are the school's local wall clock, without a zone
            since_dt = since_dt.replace(tzinfo=None)
        if since_dt is None and not unread:  # the default window; unread messages have no limit
            since_dt = self._window_start()
        try:
            msgs = await self._messages(target, since_dt, unread)
        except SessionExpired:
            return {
                **self._envelope([]),
                "session_expired": True,
                "messages": [],
                "error": "session_expired",
            }
        except EasytopError as e:
            return {**self._envelope([]), "messages": [], "error": "unavailable", "detail": str(e)}
        out = [self._msg_out(m, names) for m in msgs]
        return {**self._envelope([]), "messages": out, **self._offers_entry(out, msgs)}

    async def get_file(self, msg_hash: str, index: int) -> FileData:
        """One attachment of a message in the parent's own inbox (the file link carries the hash of the message
        id). Not a model tool: the gateway calls it when the parent taps a file link (Spec 14)."""
        return await self._c.get_message_file(msg_hash, index)

    def _ref_of(self, message_id: str) -> str:
        return self._refs.setdefault(message_id, f"m{len(self._refs) + 1}")

    def _offers_entry(self, out: list[dict[str, Any]], msgs: list[Message]) -> dict[str, Any]:
        """The private `_offers` of a tool result: per ref, the details of each file for the reply (name, size,
        who sent it and when, the start parameter of its link). The agent core takes this out before the result
        goes to the model, which only sees `ref` and a count."""
        by_id = {m.id: m for m in msgs}
        offers: dict[str, list[dict[str, Any]]] = {}
        for item in out:
            m = by_id.get(item.get("id"))
            if m is None or not m.files or "ref" not in item:
                continue
            sent = f"{m.sent_at.day}.{m.sent_at.month}"
            offers[item["ref"]] = [
                {
                    "name": f.name,
                    "ext": f.ext,
                    "size_kb": f.size_kb,
                    "sender": m.sender,
                    "sent": sent,
                    "payload": file_payload(m.id, f.index),
                }
                for f in m.files
            ]
        return {"_offers": offers} if offers else {}

    def _msg_out(self, m: Message, names: dict[str, str]) -> dict[str, Any]:
        text = m.body if len(m.body) <= MAX_MESSAGE_CHARS else m.body[:MAX_MESSAGE_CHARS] + "…"
        return _clean(
            {
                "id": m.id,
                "kid": names.get(m.kid_id) if m.kid_id else None,
                "sender": m.sender,
                "subject": m.subject,
                "text": text,
                "truncated": True if len(m.body) > MAX_MESSAGE_CHARS else None,
                "sent_at": m.sent_at.isoformat(timespec="minutes"),
                "unread": m.unread,
                "has_attachments": m.has_attachments or None,
                # a message with listed files gets a short ref to cite and a count; nothing else about the files
                "ref": self._ref_of(m.id) if m.files else None,
                "files": len(m.files) if m.files else None,
            }
        )
