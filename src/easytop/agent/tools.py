"""Tool schemas shown to the model and the dispatcher onto FamilyService.

The parent / session is never a tool argument: the FamilyService instance is bound to the parent by the
caller (resolved from the channel identity). Kid ids are validated inside FamilyService against the
parent's own list.
"""

from __future__ import annotations

from datetime import time
from typing import Any, Protocol

from easytop.agent.guardrails import ToolArgError, validate_args
from easytop.digest.nudge import parse_time
from easytop.errors import KidNotFound
from easytop.family.service import FamilyService

_KID = {"type": "string", "description": "Kid id or name from list_kids. Omit for all children."}
_KID_REQ = {"type": "string", "description": "Kid id or name from list_kids."}
_DATE = {"type": "string", "description": "ISO date YYYY-MM-DD."}


def _tool(
    name: str, description: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


TOOLS: list[dict[str, Any]] = [
    _tool("list_kids", "List the parent's children with school and class.", {}, []),
    _tool(
        "get_tomorrow_brief",
        "Next school day (skips Fri/Sat/holidays), per child: lessons, changes, calendar boxes, homework "
        "due on that day and upcoming homework (each with its due date), exams; plus, once for the "
        "family, every inbox message of the last 7 days (messages can hold homework, equipment, exam "
        "dates or other items for any category; lessons_held_since on a message lists the children and "
        "subjects it names where a lesson of the subject came after the message). Always use this to "
        "resolve 'tomorrow'. Set focus when "
        "the parent asks for one category only: homework, equipment (what to bring), exams or flow "
        "(the day's topics and changes).",
        {
            "kid": _KID,
            "focus": {
                "type": "string",
                "enum": ["homework", "equipment", "exams", "flow"],
                "description": "Only this answer category. Omit for everything.",
            },
        },
        [],
    ),
    _tool(
        "get_schedule",
        "Timetable plus changes for one kid on one date.",
        {"kid": _KID_REQ, "date": _DATE},
        ["kid", "date"],
    ),
    _tool(
        "get_homework",
        "Homework texts written in lessons dated between two dates.",
        {"kid": _KID_REQ, "from": _DATE, "to": _DATE},
        ["kid", "from", "to"],
    ),
    _tool(
        "get_messages",
        "Inbox messages (text capped). Default window: the last 7 days; pass since to reach older "
        "messages when the parent asks. Omit kid for all children.",
        {
            "kid": _KID,
            "since": {
                "type": "string",
                "description": "ISO datetime; only newer messages. Default: 7 days ago.",
            },
            "unread": {"type": "boolean", "description": "Only unread messages."},
        },
        [],
    ),
    _tool(
        "get_exams",
        "Exams between two dates.",
        {"kid": _KID_REQ, "from": _DATE, "to": _DATE},
        ["kid", "from", "to"],
    ),
    _tool(
        "get_events",
        "Grades, attendance and calendar events between two dates.",
        {"kid": _KID_REQ, "from": _DATE, "to": _DATE},
        ["kid", "from", "to"],
    ),
    _tool(
        "set_reminder",
        "Set or stop the bot's own daily evening reminder: one fixed short message, with nothing about the "
        "children in it, sent every evening before a school day at the time chosen (Israel time). action "
        "'on' needs time as 24-hour HH:MM: when they gave none, ask only for the time, in one short line. "
        "It cannot make "
        "one-off reminders, reminders about a task, or reminders at a relative time such as 'in 5 minutes'. "
        "Confirm in one short line: the time and what it carries (the fixed message, tomorrow's "
        "homework, or everything for tomorrow), nothing more, e.g. 'Reminder at 20:00, fixed message.' "
        "or 'Reminder at 17:03 with tomorrow's homework.'",
        {
            "action": {"type": "string", "enum": ["on", "off"]},
            "time": {"type": "string", "description": "24-hour HH:MM, required when action is on."},
            "content": {
                "type": "string",
                "enum": ["none", "due", "all"],
                "description": "none: the fixed message (what a new reminder starts as). due: only when "
                "the parent asks the reminder to include tomorrow's homework or info: it then lists what "
                "is due tomorrow. all: only when they ask for everything for tomorrow in the reminder. "
                "Omit to leave the current choice as it is.",
            },
        },
        ["action"],
    ),
]
_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}


class ReminderSetting(Protocol):
    """The parent's own reminder setting, bound to them by the caller like the family service is."""

    async def set(self, when: time | None, content: str | None = None) -> str | None: ...


async def _set_reminder(reminders: ReminderSetting | None, a: dict[str, Any]) -> dict[str, Any]:
    if reminders is None:
        return {"error": "reminders unavailable here"}
    if a["action"] == "off":
        await reminders.set(None)
        return {"reminder": "off"}
    when = parse_time(a.get("time") or "")
    if when is None:
        return {"error": "bad value: action on needs time as 24-hour HH:MM; ask the parent for it"}
    carries = await reminders.set(when, a.get("content"))
    return {"reminder": "on", "time": when.strftime("%H:%M"), "content": carries or "none"}


async def dispatch(
    service: FamilyService,
    name: str,
    args: Any,
    reminders: ReminderSetting | None = None,
) -> dict[str, Any]:
    """Run a tool. Errors become small structured results for the model, never stack traces."""
    if name not in _SCHEMAS:
        return {"error": f"unknown tool {name!r}"}
    try:
        a = validate_args(_SCHEMAS[name], args)
        match name:
            case "list_kids":
                return await service.list_kids()
            case "get_tomorrow_brief":
                return await service.get_tomorrow_brief(a.get("kid"), a.get("focus"))
            case "get_schedule":
                return await service.get_schedule(a["kid"], a["date"])
            case "get_homework":
                return await service.get_homework(a["kid"], a["from"], a["to"])
            case "get_messages":
                return await service.get_messages(
                    a.get("kid"), a.get("since"), a.get("unread", False)
                )
            case "get_exams":
                return await service.get_exams(a["kid"], a["from"], a["to"])
            case "get_events":
                return await service.get_events(a["kid"], a["from"], a["to"])
            case "set_reminder":
                return await _set_reminder(reminders, a)
    except ToolArgError as e:
        return {"error": f"bad arguments: {e}"}
    except KidNotFound:
        return {"error": "unknown kid; use list_kids to see this parent's children"}
    except ValueError as e:  # bad date formats
        return {"error": f"bad value: {e}"}
    return {"error": "unhandled tool"}
