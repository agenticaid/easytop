"""One JSON object per log line, in the shape Cloud Logging reads (`severity`, `message`).

Content-free like every other log line here: an exception is reduced to its type, because the text of an
HTTP error can hold a request URL and a Telegram URL holds the bot token."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            data["exception_type"] = record.exc_info[0].__name__
        event = getattr(
            record, "analytics", None
        )  # an analytics event: its checked fields go to the top level
        if isinstance(event, dict):
            data.update({k: v for k, v in event.items() if k not in data})
        return json.dumps(data, ensure_ascii=False)
