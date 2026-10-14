"""Fake connector backed by synthetic data (made-up names). Used by tests and the `tomorrow` demo CLI.

Built on synthetic data — needs spike verification against the real parent API.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from easytop.errors import FileUnavailable, SessionExpired, UpstreamError
from easytop.family.calendar import SchoolCalendar
from easytop.files import extension_allowed, message_hash, mime_for, safe_filename
from easytop.models import (
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
)

DATA_FILE = Path(__file__).parent / "synthetic" / "family.json"
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _t(s: str | None) -> time | None:
    return time.fromisoformat(s) if s else None


def load_synthetic(path: Path = DATA_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class FakeConnector:
    def __init__(
        self,
        today: date,
        data: dict[str, Any] | None = None,
        *,
        calendar: SchoolCalendar | None = None,
        fail_kids: set[str] | None = None,
        expired: bool = False,
    ):
        self.today = today
        self.data = data if data is not None else load_synthetic()
        self.cal = calendar or SchoolCalendar()
        self.fail_kids = fail_kids or set()
        self.expired = expired
        self.calls: list[tuple[str, str | None]] = []  # for cache / fan-out assertions

    # -- helpers --
    def _school_day(self, n: int) -> date:
        d = self.today
        for _ in range(n):
            d = self.cal.next_school_day(d).day
        return d

    def _check(self, name: str, kid: Kid | None) -> None:
        self.calls.append((name, kid.id if kid else None))
        if self.expired:
            raise SessionExpired("fake session expired")
        if kid and kid.id in self.fail_kids:
            raise UpstreamError(f"fake school down for {kid.name}")

    # -- Connector protocol --
    async def check_session(self) -> bool:
        return True

    async def list_kids(self) -> list[Kid]:
        self._check("list_kids", None)
        return [
            Kid(
                k["id"],
                k["name"],
                k["class_name"],
                School(k["school"]["id"], k["school"]["name"]),
                k.get("grade"),
            )
            for k in self.data["kids"]
        ]

    async def get_schedule(self, kid: Kid, day: date) -> list[Lesson]:
        self._check("get_schedule", kid)
        rows = self.data["timetable"].get(kid.id, {}).get(_WEEKDAY_KEYS[day.weekday()], [])
        changes = {
            c["period"]: c
            for c in self.data.get("changes", [])
            if c["kid_id"] == kid.id and self._school_day(c["school_days"]) == day
        }
        lessons = []
        for r in rows:
            ch = changes.get(r["period"])
            lessons.append(
                Lesson(
                    kid.id,
                    day,
                    r["period"],
                    _t(r["start"]),
                    _t(r["end"]),
                    r["subject"],
                    r.get("teacher"),
                    (ch or {}).get("room", r.get("room")),
                    (ch or {}).get("status", "normal"),
                    (ch or {}).get("note") or r.get("note"),
                )
            )
        return lessons

    async def get_lesson_events(self, kid: Kid, start: date, end: date) -> list[Event]:
        return [e for e in await self.get_events(kid, start, end) if e.kind == "attendance"]

    async def get_days_off(self, kid: Kid, start: date, end: date):
        return None  # synthetic data uses the default Fri/Sat school week

    async def get_homework(self, kid: Kid, start: date, end: date) -> list[Homework]:
        self._check("get_homework", kid)
        return self._homework_between(kid, start, end)

    def _homework_between(self, kid: Kid, start: date, end: date) -> list[Homework]:
        out = []
        for h in self.data.get("homework", []):
            due = self._school_day(h["due_school_days"])
            if h["kid_id"] == kid.id and start <= due <= end:
                out.append(
                    Homework(
                        kid.id,
                        h["subject"],
                        h["description"],
                        due,
                        self.today - timedelta(days=h["assigned_days_ago"]),
                    )
                )
        return out

    async def get_homework_for_day(
        self, kid: Kid, day: date, now: datetime | None = None
    ) -> list[Homework]:
        # synthetic data lists homework by target day; whether a lesson passed it is the real connector's job
        return await self.get_homework(kid, day, day)

    async def get_homework_ahead(
        self,
        kid: Kid,
        day: date,
        horizon: int = 7,
        skip_days: frozenset[date] = frozenset(),
        now: datetime | None = None,
    ) -> list[Homework]:
        self._check("get_homework_ahead", kid)
        later = self._homework_between(kid, day + timedelta(days=1), day + timedelta(days=horizon))
        return sorted((h for h in later if h.due not in skip_days), key=lambda h: (h.due, h.subject))

    async def get_held_lessons(self, kid: Kid, since: date, now: datetime) -> list[HeldLesson]:
        """From the optional `held_lessons` of the data: {kid_id, days_ago, hour, subject}."""
        self._check("get_held_lessons", kid)
        out = []
        for h in self.data.get("held_lessons", []):
            day = self.today - timedelta(days=h["days_ago"])
            if h["kid_id"] == kid.id and since <= day <= now.date():
                out.append(HeldLesson(kid.id, day, h["hour"], h["subject"]))
        return sorted(out, key=lambda x: (x.day, x.hour))

    async def get_messages(
        self, kid: Kid | None, since: datetime | None, unread_only: bool
    ) -> list[Message]:
        self._check("get_messages", kid)
        out = []
        for m in self.data.get("messages", []):
            sent = datetime.combine(self.today - timedelta(days=m["sent_days_ago"]), time(9, 0))
            if kid and m["kid_id"] not in (None, kid.id):
                continue
            if since and sent < since:
                continue
            if unread_only and not m["unread"]:
                continue
            out.append(
                Message(
                    m["id"],
                    m["kid_id"],
                    m["sender"],
                    m["subject"],
                    m["body"],
                    sent,
                    m["unread"],
                    m["has_attachments"],
                    tuple(
                        MessageFile(i, f["name"], f["ext"], f.get("size_kb", 0))
                        for i, f in enumerate(m.get("files", []))
                    ),
                )
            )
        return out

    async def get_message_file(self, msg_hash: str, index: int) -> FileData:
        """Synthetic `files` entries: {name, ext, size_kb, content (text, optional)}."""
        self._check("get_message_file", None)
        for m in self.data.get("messages", []):
            files = m.get("files") or []
            if not files or message_hash(m["id"]) != msg_hash:
                continue
            if not 0 <= index < len(files):
                raise FileUnavailable("not_found")
            f = files[index]
            if not extension_allowed(f["ext"]):
                raise FileUnavailable("type_refused")
            return FileData(
                safe_filename(f["name"], f["ext"]),
                f.get("content", "%PDF-1.4 synthetic").encode(),
                mime_for(f["ext"]),
            )
        raise FileUnavailable("not_found")

    async def get_exams(self, kid: Kid, start: date, end: date) -> list[Exam]:
        self._check("get_exams", kid)
        out = []
        for e in self.data.get("exams", []):
            day = self._school_day(e["school_days"])
            if e["kid_id"] == kid.id and start <= day <= end:
                out.append(Exam(kid.id, e["subject"], day, e.get("topic")))
        return out

    async def get_events(self, kid: Kid, start: date, end: date) -> list[Event]:
        self._check("get_events", kid)
        out = []
        for e in self.data.get("events", []):
            day = self.today - timedelta(days=e["days_ago"])
            if e["kid_id"] == kid.id and start <= day <= end:
                out.append(Event(kid.id, e["kind"], day, e["title"], e.get("detail")))
        return out
