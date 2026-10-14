"""The content-free evening nudge (Spec 05): texts, the words that set it, and (below) the timer that sends it.

The nudge says only that tomorrow's summary is ready. It never carries a child's name or any school data, so
nothing shows on a lock screen. v1 sends no answer buttons, so it is plain text and settings are words."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from easytop import analytics
from easytop.adapters.model import Reply
from easytop.digest.content import render_reminder
from easytop.errors import EasytopError, SessionExpired
from easytop.store.base import DigestSetting, Store

log = logging.getLogger("easytop.digest")

ISRAEL = ZoneInfo("Asia/Jerusalem")  # needs the `tzdata` package on a slim container image

OFFER = (
    "רוצים תזכורת קצרה בערב, בלי מידע על הילדים, כשהסיכום למחר מוכן? "
    'כתבו שעה, למשל 19:00, או "לא".\n\n'
    "Want a short evening reminder (nothing about the kids in it) when tomorrow's summary is ready? "
    'Write a time such as 19:00, or "no".'
)
ASK = (
    'באיזו שעה בערב לשלוח את התזכורת? כתבו שעה, למשל 19:00, או "לא".\n\n'
    'At what time in the evening should I send the reminder? Write a time such as 19:00, or "no".'
)
ON = (
    "מעולה, אשלח תזכורת בשעה {time} בערבים שלפני יום לימודים. "
    'לשינוי כתבו "תזכורת 20:00", ולכיבוי "תזכורת כבוי".\n\n'
    "Done, I'll remind you at {time} on evenings before a school day. "
    'To change it write "reminder 20:00", to stop it "reminder off".'
)
OFF = (
    'התזכורת כובתה. אפשר להפעיל שוב בכתיבת "תזכורת 19:00".\n\n'
    'The reminder is off. To turn it on again write "reminder 19:00".'
)
DECLINED = (
    'בסדר, בלי תזכורת. אפשר לשאול אותי "מה מחר?" בכל זמן, ואם תרצו תזכורת בהמשך כתבו "תזכורת 19:00".\n\n'
    'OK, no reminder. Ask me "what\'s tomorrow?" any time, and if you want a reminder later write '
    '"reminder 19:00".'
)
NUDGE = (
    '🌙 הסיכום למחר מוכן. אפשר לכתוב "מה מחר?".\n\n'
    "Tomorrow's summary is ready. Just ask \"what's tomorrow?\"."
)

_TIME = re.compile(r"(\d{1,2})[:.](\d{2})")
_AMPM = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s?(am|pm)", re.IGNORECASE)
_EVENING = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s?בערב")
_COMMAND = re.compile(r"(תזכורת|reminder)\s+(.+)", re.IGNORECASE)
_OFF_WORDS = {"כבוי", "off"}

# A time as a parent writes it inside a request: 19:30, 7.30, 8pm, 8:30 pm, 8 בערב, or a bare hour (which parse_time
# refuses, so the bot asks again instead of guessing am or pm).
_TX = r"\d{1,2}(?:[:.]\d{2})?(?:\s?(?:am|pm|בערב))?"
_HE_PREP = r"(?:ב|ל|בשעה|לשעה)"
_HE_NOUN = r"(?:את )?ה?תזכורת"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Whole-message requests only (fullmatch): "any reminders from school?" or "remind me to bring the ruler" are for the
# agent, because the word "reminder" is common in teachers' messages. Each pattern may end in a time group `t`.
_ON = [
    _rx(
        r"(?:please )?(?:set(?: up)?|create|add|make|change|update|schedule|turn on|enable)"
        r"(?: me| for me)?(?: an?| the| my)?(?: (?:daily|evening|nightly))? reminders?"
        rf"(?: (?:for|at|to|on|around))?(?: (?P<t>{_TX}))?(?: please)?"
    ),
    _rx(
        rf"(?:please )?remind me(?: every (?:evening|night|day))?(?: (?:at|around|by))?"
        rf"(?: (?P<t>{_TX}))?(?: please)?"
    ),
    _rx(
        r"(?:i(?: want| need|'d like| would like)|want|need)(?: to have)?(?: an?| the)?"
        rf"(?: (?:daily|evening|nightly))? reminders?(?: (?:at|for))?(?: (?P<t>{_TX}))?(?: please)?"
    ),
    _rx(rf"reminders? (?:at|for|on) (?P<t>{_TX})"),
    _rx(
        r"(?:תקבעי?|תגדירי?|תוסיף|תוסיפי|תיצור|תיצרי|תפעילי?|תשנה|תשני|תעדכן|תעדכני"
        r"|קבעי?|הגדר|הגדירי|הוסף|הוסיפי|צור|צרי|הפעל|הפעילי|שנה|שני|עדכן|עדכני)"
        rf"(?: לי)? {_HE_NOUN}(?: {_HE_PREP})?(?: (?P<t>{_TX}))?"
    ),
    _rx(rf"(?:תזכירי?|תזכירו)(?: לי)?(?: כל ערב)?(?: {_HE_PREP})?(?: (?P<t>{_TX}))?"),
    _rx(
        rf"(?:אני )?(?:רוצה|רוצים|צריכה?|מעוניין|מעוניינת) תזכורת(?: {_HE_PREP})?(?: (?P<t>{_TX}))?"
    ),
    _rx(rf"תזכורת {_HE_PREP} (?P<t>{_TX})"),
]
_OFF = [
    _rx(
        r"(?:please )?(?:turn off|switch off|stop|disable|cancel|remove|delete)(?: the| my| all)* "
        r"reminders?(?: please)?"
    ),
    _rx(r"no (?:more )?reminders?"),
    _rx(r"stop reminding me"),
    _rx(
        r"(?:תבטלי?|בטלי?|תכבה|תכבי|כבה|כבי|תפסיקי?|הפסק|הפסיקי|תסירי?|הסר|תמחקי?|מחק)"
        r" (?:את )?ה?תזכור(?:ת|ות)"
    ),
    _rx(r"בלי תזכור(?:ת|ות)"),
]


def parse_time(text: str) -> time | None:
    """`19:00`, `7:30`, `19.00`, `8pm`, `8:30 pm` or `8 בערב` -> a time; anything else (a bare hour, text, 25:00)
    -> None. A bare hour is refused on purpose: 8 could be morning or evening."""
    t = text.strip()
    if m := _TIME.fullmatch(t):
        hour, minute = int(m.group(1)), int(m.group(2))
        return time(hour, minute) if hour < 24 and minute < 60 else None
    if m := _AMPM.fullmatch(t):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12 or minute >= 60:
            return None
        return time(hour % 12 + (12 if m.group(3).lower() == "pm" else 0), minute)
    if m := _EVENING.fullmatch(t):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        return time(hour + 12, minute) if 1 <= hour <= 11 and minute < 60 else None
    return None


def _normalise(text: str) -> str:
    """Lower case, Hebrew prefix hyphens (ב-20:00, ל־20:00) and commas as spaces, ends trimmed."""
    t = re.sub(r"[־\-–,]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip(" .!?;")


def parse_command(text: str) -> time | Literal["off", "ask"] | None:
    """What a parent wants done to the reminder, in words:
    - a time (`תזכורת 20:00`, `reminder 20:00`, `set a reminder for 8pm`, `תזכיר לי ב-20:00`),
    - "off" (`תזכורת כבוי`, `reminder off`, `turn off the reminder`, `בטל תזכורת`),
    - "ask" for a request that names no usable time (`create a reminder`, `set a reminder for 8`),
    - None for everything else, which is for the agent."""
    t = _normalise(text)
    if m := _COMMAND.fullmatch(t):
        rest = m.group(2).strip()
        if rest in _OFF_WORDS:
            return "off"
        if (when := parse_time(rest)) is not None:
            return when
    if any(rx.fullmatch(t) for rx in _OFF):
        return "off"
    for rx in _ON:
        if m := rx.fullmatch(t):
            return parse_time(m.group("t") or "") or "ask"
    return None


class StoreReminders:
    """One parent's reminder setting: the agent's `set_reminder` tool and the reminder words both change it here.
    A time turns the evening nudge on at that time, None turns it off. The count-only analytics event is emitted
    here, so the two ways of setting it look the same in the dashboard."""

    def __init__(self, store: Store, store_id: str, parent_id: str):
        self._store, self._store_id, self._parent_id = store, store_id, parent_id

    async def set(self, when: time | None, content: str | None = None) -> str | None:
        """`content` None leaves the current choice alone (a reminder switched on again starts content-free), so
        changing only the time never silently drops what the parent asked the reminder to carry. Returns what the
        reminder now carries ("none", "due" or "all"), None when it was switched off, so the answer can say it."""
        if when is None:
            await self._store.set_digest(self._store_id, DigestSetting(False, None))
            analytics.emit("nudge_setting", self._parent_id, state="off")
            return None
        if content is None:
            current = await self._store.get_digest(self._store_id)
            content = current.content if current.enabled else "none"
        await self._store.set_digest(self._store_id, DigestSetting(True, when, content))
        analytics.emit("nudge_setting", self._parent_id, state="on", hour=when.hour, content=content)
        return content


class NudgeScheduler:
    """Sends the nudge to every parent whose chosen minute it is. Runs inside the bot (one always-on process),
    so there is no separate scheduler service. Times are read in Israel time whatever the server's zone.

    Content-free like the nudge itself: the log line holds counts only, a failure only the exception type."""

    def __init__(
        self,
        store: Store,
        registry: Any,
        notify: Callable[[str, Reply], Awaitable[None]],
        *,
        expired_reply: Callable[[str], Awaitable[Reply]],
        clock: Callable[[], datetime] | None = None,
        tz: ZoneInfo = ISRAEL,
    ):
        """`registry` hands back a parent's agent (None without a live session) and says whether a session
        was found expired; `expired_reply` is the usual reconnect notice with its button."""
        self._store, self._registry, self._notify = store, registry, notify
        self._expired_reply = expired_reply
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sent: set[tuple[str, date]] = set()  # (parent, day) already nudged: one a day at most
        self._sleep = asyncio.sleep

    async def tick(self) -> int:
        """Send what is due this minute; returns how many nudges went out."""
        local = self._clock().astimezone(self._tz)
        today = local.date()
        self._sent = {key for key in self._sent if key[1] == today}
        parents = {
            f"{p.channel}:{p.channel_user_id}": p.id
            for p in await self._store.due_digests(local.time())
        }
        due = [pid for pid in parents if (pid, today) not in self._sent]
        outcomes = await asyncio.gather(*(self._nudge(pid, parents[pid]) for pid in due))
        for pid, outcome in zip(due, outcomes, strict=True):
            if outcome == "sent":
                self._sent.add((pid, today))
        sent = outcomes.count("sent")
        skipped, failed = outcomes.count("skipped"), outcomes.count("failed")
        if due:
            log.info("nudges sent=%d skipped=%d failed=%d", sent, skipped, failed)
        return sent

    async def _nudge(self, pid: str, store_id: str) -> str:
        outcome, result, content = await self._nudge_one(pid, store_id)
        analytics.emit("nudge", pid, result=result, content=content)
        return outcome

    async def _nudge_one(self, pid: str, store_id: str) -> tuple[str, str, str]:
        """(log outcome, analytics result, content level sent): sent, skipped (no school / expired) or failed.
        Nothing here calls the model: the text is fixed, or written by code from Webtop's data."""
        try:
            agent = await self._registry.agent_for(pid)
            if agent is None:  # no live session: the usual expiry notice with the reconnect button
                await self._notify(pid, await self._expired_reply(pid))
                return "skipped", "expired", "none"
            try:
                if not await agent.family.school_tomorrow():
                    return "skipped", "no_school", "none"  # no school tomorrow: stay silent
                reply, content = await self._reply(agent, store_id)
            except SessionExpired:
                await self._registry.take_expired(pid)
                await self._notify(pid, await self._expired_reply(pid))
                return "skipped", "expired", "none"
            await self._notify(pid, reply)
            return "sent", "sent", content
        except Exception as e:  # one parent's problem must never stop the others
            log.warning("nudge failed parent=%s err=%s", pid, type(e).__name__)
            analytics.emit_error("nudge", e)
            return "failed", "failed", "none"

    async def _reply(self, agent: Any, store_id: str) -> tuple[Reply, str]:
        """The fixed nudge, or the content the parent asked for. A brief that cannot be read (the school is down)
        falls back to the fixed text, so the reminder itself still arrives; an expired session is the caller's."""
        level = (await self._store.get_digest(store_id)).content
        if level == "none":
            return Reply(text=NUDGE), "none"
        try:
            brief = await agent.family.get_tomorrow_brief(None, "due" if level == "due" else "digest")
        except SessionExpired:
            raise
        except EasytopError as e:
            log.warning("nudge content unavailable err=%s", type(e).__name__)
            return Reply(text=NUDGE), "none"
        text = render_reminder(brief, level)
        return (Reply(text=text), level) if text else (Reply(text=NUDGE), "none")

    async def run(self) -> None:
        """Tick at the start of every minute until cancelled."""
        while True:
            now = self._clock()
            await self._sleep(60.0 - (now.second + now.microsecond / 1_000_000))
            try:
                await self.tick()
            except Exception as e:  # e.g. the database is briefly down: try again next minute
                log.warning("nudge tick failed err=%s", type(e).__name__)
