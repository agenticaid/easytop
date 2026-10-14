"""Content-free usage analytics (task 6.11, Spec 07 "Usage analytics").

One flat JSON event per fact, written as a log line with an `event` key. On Cloud Run the line lands in Cloud Logging;
a log sink copies only lines with an `event` key to BigQuery (tools/ops/analytics_setup.sh), where the views in
docs/analytics/ feed a Looker Studio dashboard.

Rules this module enforces, so the kids' data cannot leak in by accident:
- every event and field is declared in SCHEMA; strings are only enum values or exception class names, never free text;
- a parent appears only as `parent_ref`, an HMAC pseudonym keyed by the environment's session key, never a Telegram id;
- a refused event raises in tests (strict) and is dropped with a warning in production (the bot never breaks over it).

Labels such as the question category are derived by code from the tool calls (`labels_for`), with no extra model call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("easytop.analytics")

SCHEMA_VERSION = 1
PSEUDONYM_LABEL = b"easytop analytics pseudonym v1"


class AnalyticsSchemaError(ValueError):
    """An event that does not match SCHEMA. Its message names the event and field, never the value."""


@dataclass(frozen=True)
class Kind:
    family: str  # int / float / bool / string / list (one per field name across all events: BigQuery needs it)
    values: frozenset[str] | None = None  # enum values (string) or allowed items (list)
    pattern: re.Pattern | None = None


INT = Kind("int")
FLOAT = Kind("float")
BOOL = Kind("bool")
CLASS_NAME = Kind(
    "string", pattern=re.compile(r"[A-Z][A-Za-z0-9]{0,63}")
)  # filled only from type(e).__name__


def enum(*values: str) -> Kind:
    return Kind("string", frozenset(values))


def list_of(*values: str) -> Kind:
    return Kind("list", frozenset(values))


TOOL_NAMES = (
    "list_kids",
    "get_tomorrow_brief",
    "get_schedule",
    "get_homework",
    "get_messages",
    "get_exams",
    "get_events",
    "set_reminder",
)
# a tool that changes the bot's own setting, not a question about the children: its own category, no kid scope
SETTING_TOOLS = {"set_reminder": "reminder"}
DATA_TOOLS = {
    "get_tomorrow_brief": "tomorrow",
    "get_schedule": "schedule",
    "get_homework": "homework",
    "get_messages": "messages",
    "get_exams": "exams",
    "get_events": "events",
}
FOCUSES = ("homework", "equipment", "exams", "flow")
MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-fable-5-1")
OUTCOMES = (
    "answered",
    "answered_no_data",
    "rate_limited",
    "session_expired",
    "error",
    "truncated",
    "tool_limit",
)

_QUESTION = {
    "outcome": enum(*OUTCOMES),
    "category": enum(
        *DATA_TOOLS.values(), *SETTING_TOOLS.values(), "mixed", "kids", "none", "unknown"
    ),
    "focus": enum(*FOCUSES, "all", "none"),
    "kids_scope": enum("one", "several", "all", "none"),
    "language": enum("he", "en", "ru", "ar", "other"),
    "follow_up": BOOL,
    "model": enum(*MODELS),
    "answer_ms": INT,
    "tool_count": INT,
    "tools": list_of(*TOOL_NAMES),
    "input_tokens": INT,
    "output_tokens": INT,
    "cache_read_tokens": INT,
    "cache_write_tokens": INT,
    "cost_usd": FLOAT,
    "error_type": CLASS_NAME,
}
SCHEMA: dict[str, dict[str, Kind]] = {
    "question": _QUESTION,
    "access_denied": {"reason": enum("not_allowed", "bad_invite")},
    "invite_created": {"ttl_hours": INT},
    "invite_redeemed": {},
    "connect_offered": {"reason": enum("first", "expired", "no_kids", "wrong_account")},
    "connect_opened": {},
    "connect_abandoned": {
        "reason": enum("left", "timeout", "failed", "link_expired"),
        "login_seconds": FLOAT,
    },
    "login_captured": {
        "route": enum("ministry", "code", "password", "unknown"),
        "login_seconds": FLOAT,
        "cookie_hours": FLOAT,
        "taps": INT,
        "frames_dropped": INT,
    },
    "kids_confirmed": {"kid_count": INT, "school_count": INT, "reconnect": BOOL},
    "kids_rejected": {"kid_count": INT},
    "nudge_setting": {
        "state": enum("on", "off", "declined"),
        "hour": INT,
        "content": enum("none", "due", "all"),
    },
    "nudge": {
        "result": enum("sent", "no_school", "expired", "failed"),
        "content": enum("none", "due", "all"),
    },
    "session_check": {"status": enum("ok", "dead", "unknown"), "age_h": FLOAT},
    "session_expired": {},
    "error": {
        "where": enum("agent", "onboarding", "login", "nudge", "health", "telegram", "bot"),
        "error_type": CLASS_NAME,
    },
}
REQUIRED: dict[str, set[str]] = {
    "question": {"outcome"},
    "access_denied": {"reason"},
    "connect_offered": {"reason"},
    "connect_abandoned": {"reason"},
    "login_captured": {"route"},
    "nudge_setting": {"state"},
    "nudge": {"result"},
    "session_check": {"status"},
    "error": {"where", "error_type"},
}
COMMON = ("event", "schema", "env", "ts", "synthetic", "parent_ref")
ENVS = ("dev", "prod", "local", "test")


def type_family(kind: Kind) -> str:
    return kind.family


def _check(event: str, name: str, kind: Kind, value: Any) -> Any:
    """The value as it is written, or AnalyticsSchemaError. Never puts the value in the error message."""
    bad = AnalyticsSchemaError(f"bad value for {event}.{name}")
    match kind.family:
        case "bool":
            if not isinstance(value, bool):
                raise bad
            return value
        case "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise bad
            return value
        case "float":
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise bad
            return round(float(value), 6)
        case "string":
            if not isinstance(value, str):
                raise bad
            if kind.values is not None and value not in kind.values:
                raise bad
            if kind.pattern is not None and not kind.pattern.fullmatch(value):
                raise bad
            return value
        case "list":
            if not isinstance(value, list | tuple) or any(v not in kind.values for v in value):
                raise bad
            return list(value)
    raise bad


def validate(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    if event not in SCHEMA:
        raise AnalyticsSchemaError(f"unknown event {event}")
    allowed = SCHEMA[event]
    for name in fields:
        if name not in allowed:
            raise AnalyticsSchemaError(f"unknown field {event}.{name}")
    missing = REQUIRED.get(event, set()) - {k for k, v in fields.items() if v is not None}
    if missing:
        raise AnalyticsSchemaError(f"missing field {event}.{sorted(missing)[0]}")
    return {
        name: _check(event, name, allowed[name], value)
        for name, value in fields.items()
        if value is not None
    }


class Emitter:
    def __init__(
        self,
        *,
        env: str,
        key: bytes | None,
        clock: Callable[[], datetime] | None = None,
        strict: bool = False,
        synthetic: bool = False,
    ):
        """`key` is the environment's session encryption key: the pseudonym salt is derived from it, so refs are
        stable across restarts and differ between environments. No key: a random salt for this process."""
        if env not in ENVS:
            env = "local"
        self.env, self.strict, self.synthetic = env, strict, synthetic
        self._salt = (
            hmac.new(key, PSEUDONYM_LABEL, hashlib.sha256).digest()
            if key
            else secrets.token_bytes(32)
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def ref(self, parent_id: str) -> str:
        return "p_" + hmac.new(self._salt, parent_id.encode(), hashlib.sha256).hexdigest()[:16]

    def build(self, event: str, parent: str | None = None, **fields: Any) -> dict[str, Any]:
        body = validate(event, fields)
        ts = self._clock().astimezone(UTC)
        out: dict[str, Any] = {
            "event": event,
            "schema": SCHEMA_VERSION,
            "env": self.env,
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "synthetic": self.synthetic,
        }
        if parent is not None:
            out["parent_ref"] = self.ref(parent)
        out.update(body)
        return out

    def record(self, event: str, parent: str | None = None, **fields: Any) -> logging.LogRecord:
        data = self.build(event, parent, **fields)
        shown = {k: v for k, v in data.items() if k not in COMMON or k == "parent_ref"}
        return log.makeRecord(
            log.name,
            logging.INFO,
            __file__,
            0,
            "event %s %s",
            (event, json.dumps(shown, ensure_ascii=False, separators=(",", ":"))),
            None,
            extra={"analytics": data},
        )

    def emit(self, event: str, parent: str | None = None, **fields: Any) -> None:
        try:
            record = self.record(event, parent, **fields)
        except AnalyticsSchemaError as e:
            if self.strict:
                raise
            log.warning(
                "analytics event refused event=%s why=%s", event if event in SCHEMA else "?", e
            )
            return
        if log.isEnabledFor(logging.INFO):
            log.handle(record)


_current = Emitter(env=os.environ.get("EASYTOP_ENV", "local"), key=None)


def configure(
    *,
    env: str | None = None,
    key: bytes | str | None = None,
    clock: Callable[[], datetime] | None = None,
    strict: bool = False,
    synthetic: bool = False,
) -> Emitter:
    """Set the process-wide emitter (the bot at start-up, the tests, the synthetic generator)."""
    global _current
    if isinstance(key, str):
        key = key.encode()
    _current = Emitter(
        env=env or os.environ.get("EASYTOP_ENV", "local"),
        key=key,
        clock=clock,
        strict=strict,
        synthetic=synthetic,
    )
    return _current


def current() -> Emitter:
    return _current


def set_current(emitter: Emitter) -> None:
    """Put back an emitter saved with `current()` (the synthetic generator restores the bot's own)."""
    global _current
    _current = emitter


def emit(event: str, parent: str | None = None, **fields: Any) -> None:
    _current.emit(event, parent, **fields)


def emit_error(where: str, exc: BaseException) -> None:
    _current.emit("error", where=where, error_type=type(exc).__name__)


# ---- labels derived by code from what the agent did; inputs are read here and never written anywhere ----


def labels_for(tool_uses: Iterable[dict[str, Any]]) -> tuple[str, str, str]:
    """(category, focus, kids_scope) from the tool calls of one answer: {"name": ..., "input": {...}} each."""
    uses = list(tool_uses)
    if any(u["name"] not in TOOL_NAMES for u in uses):
        return "unknown", "none", "none"
    data = [u for u in uses if u["name"] in DATA_TOOLS]
    settings = {SETTING_TOOLS[u["name"]] for u in uses if u["name"] in SETTING_TOOLS}
    if not data and not settings:
        return ("kids" if uses else "none"), "none", "none"
    categories = {DATA_TOOLS[u["name"]] for u in data} | settings
    category = categories.pop() if len(categories) == 1 else "mixed"
    if not data:
        return category, "none", "none"
    focus = "none"
    if category == "tomorrow":
        focuses = {(u.get("input") or {}).get("focus") for u in data}
        only = focuses.pop() if len(focuses) == 1 else None
        focus = only if only in FOCUSES else "all"
    kids = [(u.get("input") or {}).get("kid") for u in data]
    if any(not k for k in kids):
        scope = "all"
    else:
        scope = "one" if len({str(k).strip().lower() for k in kids}) == 1 else "several"
    return category, focus, scope


_SCRIPTS = (
    ("he", re.compile(r"[֐-׿]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("en", re.compile(r"[A-Za-z]")),
)


def language_of(text: str) -> str:
    """The question's script, by letter count (the majority wins): he / ar / ru / en, else other."""
    counts = {lang: len(p.findall(text)) for lang, p in _SCRIPTS}
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n else "other"
