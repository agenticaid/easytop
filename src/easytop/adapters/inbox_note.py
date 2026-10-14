"""The one fixed line that ends an answer which used the inbox (Erez 2026-10-13): how far back the messages
were read, in as few words as possible (Erez 2026-10-13; older messages are simply asked for). Added by code,
so it costs no prompt tokens and is never forgotten or reworded by the model."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from easytop.family.service import MESSAGE_WINDOW_DAYS

NOTE_HE = "נבדקו הודעות מ־7 הימים אחרונים."
NOTE_EN = "Messages checked: last 7 days."


def inbox_note(tool_uses: list[dict[str, Any]], language: str, now: datetime) -> str | None:
    """The note for an answer whose tool calls were `tool_uses` (name + input each), or None when the inbox was
    not read, or when some call reached further back than the 7-day window (a note saying 7 days would then be
    wrong). `language` is the question's script (analytics.language_of): Hebrew gets Hebrew, everyone else English
    (the known MVP limit for other languages)."""
    window_start = datetime.combine(
        now.date() - timedelta(days=MESSAGE_WINDOW_DAYS), datetime.min.time()
    )
    read_inbox = False
    for use in tool_uses:
        name, args = use.get("name"), use.get("input") or {}
        if name == "get_tomorrow_brief":
            read_inbox = True
        elif name == "get_messages":
            if args.get("unread"):
                return None
            if (since := args.get("since")) is not None:
                try:
                    when = datetime.fromisoformat(since).replace(tzinfo=None)
                except (TypeError, ValueError):
                    return None  # cannot tell how far back it went: do not claim 7 days
                if when < window_start:
                    return None
            read_inbox = True
    if not read_inbox:
        return None
    return NOTE_HE if language == "he" else NOTE_EN
