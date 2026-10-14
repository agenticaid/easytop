"""Plain-text lines for one kid's tomorrow brief. Shared by the CLI and the channel adapters."""

from __future__ import annotations

from typing import Any

EXPIRED_LINE = "⚠️ Webtop connection expired — reconnect needed."
DOWN_LINE = "⚠️ that school's Webtop didn't respond. Try again in a few minutes."
NOTHING_LINE = "Nothing special found."


def _homework_line(h: dict[str, Any]) -> str:
    text = f"{h['subject']}: {h['description']}"
    if h.get("due"):
        return f"{text} (due)"
    if h.get("assigned"):
        topic = f"class topic: {h['topic']}; " if h.get("topic") else ""
        return f"{text} ({topic}from the lesson of {h['assigned']})"
    return text


def kid_lines(k: dict[str, Any]) -> list[str]:
    if not k["ok"]:
        return [EXPIRED_LINE if k.get("error") == "session_expired" else DOWN_LINE]
    items: list[str] = []
    for c in k.get("changes", []):
        items.append(
            f"⚠️ {c['subject']} {c['status']}" + (f" — {c['note']}" if c.get("note") else "")
        )
    items += [_homework_line(h) for h in k.get("homework_due", [])]
    items += [f"exam {e['subject']} on {e['date']}" for e in k.get("upcoming_exams", [])]
    if k.get("ends_at"):
        items.append(f"school {k['starts_at']}–{k['ends_at']}")
    return items or [NOTHING_LINE]
