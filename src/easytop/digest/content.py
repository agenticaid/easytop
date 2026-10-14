"""The text of a content reminder, written by code from the family service's brief (Erez 2026-10-13).

No model is involved: the parent asked once for a reminder that carries tomorrow's information, and every
evening a timer reads Webtop, writes these lines and sends them, at no model cost. Hebrew only: the reminder has no
question to mirror the language of (a known MVP limit, like the first-version buttons). The text is never logged.

Levels: "due" is what is due tomorrow (homework, an exam that day); "all" adds the day's other parts exactly as
Webtop holds them (changes, calendar boxes, the teachers' lesson notes verbatim, later homework and exams). The
model's splitting of free text into equipment and topics is NOT done here, so notes are shown as written."""

from __future__ import annotations

from datetime import date
from typing import Any

from easytop.agent.guardrails import MAX_ANSWER_CHARS

EMPTY_DUE = "לא נמצאו שיעורי בית או מבחנים למחר."
EMPTY_ALL = "לא נמצא מידע למחר."
_CUT = "\n… (קוצר — שאלו לפרטים)"
_LIMIT = MAX_ANSWER_CHARS - len(_CUT)
_STATUS = {"cancelled": "בוטל", "substitute": "מחליף/ה", "room_changed": "שינוי חדר"}
# Monday = 0 ... Sunday = 6
_WEEKDAY = ["ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳", "א׳"]


def _one(text: Any) -> str:
    """Teachers' free text on one line: their line breaks would spill a note over several lines."""
    return " ".join(str(text).split())


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day}.{d.month}"


def _weekday(iso: str) -> str:
    return _WEEKDAY[date.fromisoformat(iso).weekday()]


def _kid_lines(kid: dict[str, Any], day: str, level: str) -> list[str]:
    lines: list[str] = []
    if level == "all":
        for c in kid.get("changes", []):
            status = _STATUS.get(c.get("status", ""), c.get("status", ""))
            note = f" — {_one(c['note'])}" if c.get("note") else ""
            lines.append(f"⚠️ {_one(c['subject'])} {status}{note}")
    for h in kid.get("homework_due", []):
        lines.append(f"📚 {_one(h['subject'])} — {_one(h['description'])}")
    for e in kid.get("upcoming_exams", []):
        topic = f" — {_one(e['topic'])}" if e.get("topic") else ""
        if e["date"] == day:
            lines.append(f"📝 מבחן {_one(e['subject'])}{topic}")
        elif level == "all":
            lines.append(f"📝 מבחן {_one(e['subject'])} ב־{_day(e['date'])}")
    if level != "all":
        return lines
    for h in kid.get("homework_upcoming", []):
        due = f" (להגשה {_weekday(h['due'])} {_day(h['due'])})" if h.get("due") else ""
        lines.append(f"📚 {_one(h['subject'])} — {_one(h['description'])}{due}")
    for c in kid.get("calendar", []):
        detail = f" — {_one(c['detail'])}" if c.get("detail") else ""
        lines.append(f"🗓️ {_one(c['title'])}{detail}")
    for x in kid.get("lessons", []):
        if not x.get("note"):
            continue
        if x.get("day_opener"):
            lines.append(f"🎒 פתיחת היום — {_one(x['note'])}")
        else:
            hour = x.get("hours") or x.get("period")
            lines.append(f"🎒 שעה {hour} · {_one(x['subject'])} — {_one(x['note'])}")
    return lines


def render_reminder(brief: dict[str, Any], level: str) -> str | None:
    """The reminder for one parent's brief (`focus` "due" or "digest"), or None for the content-free level."""
    if level not in ("due", "all"):
        return None
    day = brief["date"]
    out = [f"🌙 מחר, יום {_weekday(day)} {_day(day)}"]
    blocks: list[str] = []
    for kid in brief["kids"]:
        name = f"**__{kid['name']}__**"
        if not kid.get("ok", True):
            blocks.append(f"⚠️ {name}: הוובטופ לא ענה.")
            continue
        kid_day = kid.get("date") or day
        lines = _kid_lines(kid, kid_day, level)
        if not lines:
            continue
        own = f" — יום {_weekday(kid_day)} {_day(kid_day)}" if kid_day != day else ""
        blocks.append("\n".join([f"{name} ({kid['class']}){own}", *lines]))
    text = "\n\n".join([out[0], *blocks]) if blocks else f"{out[0]}\n\n{EMPTY_DUE if level == 'due' else EMPTY_ALL}"
    if len(text) <= _LIMIT + len(_CUT):
        return text
    cut = text[:_LIMIT].rsplit("\n", 1)[0] or text[:_LIMIT]
    return cut.rstrip() + _CUT
