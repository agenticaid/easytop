"""Channel-agnostic pipeline: Incoming -> allowlist -> rate limit -> agent -> Reply.

Logs metadata only (tool names, tokens, errors) — never question or answer content. Each question also becomes one
content-free analytics event (`question`): labels derived by code from the tool calls, tokens, cost and time.
"""

from __future__ import annotations

import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from easytop import analytics
from easytop.access.invites import invite_code_of
from easytop.adapters.inbox_note import inbox_note
from easytop.adapters.model import Document, Incoming, Reply
from easytop.adapters.security import Allowlist, DailyCap, RateLimiter
from easytop.agent.core import Agent, AgentResult, Conversation
from easytop.errors import EasytopError, FileUnavailable, SessionExpired
from easytop.files import MAX_FILE_BYTES, parse_file_start

if TYPE_CHECKING:  # the onboarding package imports adapter models, so import it only for typing
    from easytop.access.invites import InviteService
    from easytop.onboarding.flow import OnboardingFlow
    from easytop.onboarding.registry import AgentRegistry

log = logging.getLogger("easytop.gateway")

PRIVATE_BETA = "This bot is in private beta. Access is by invitation."
WELCOME = (
    "שלום! אני קורא את הוובטופ של הילדים ועונה בהודעה אחת, לפי ארבעה נושאים: "
    "📚 שיעורי בית, 🎒 ציוד, 📝 מבחנים, 🗓️ מהלך היום.\n\n"
    "פשוט כותבים לי חופשי, כמו לבן אדם. למשל:\n"
    "• מה הילדים צריכים מחר?\n"
    "• ציוד בלבד לנועה ליום ראשון\n"
    "• יש שיעורי בית בחשבון?\n\n"
    "You can write to me in English too."
)
INVITE_WORDS = {"invite", "הזמנה"}
INVITE_UNAVAILABLE = "אי אפשר ליצור הזמנה כרגע, נסו שוב בעוד רגע.\n\nCannot create an invite right now, try again in a moment."
FOLLOW_UP_WINDOW = 600.0  # seconds: a question this soon after the last one counts as a follow-up
ASK_SOMETHING = "שלחו לי שאלה על בית הספר של הילדים. Send me a question about your kids' school."


def daily_cap_text(limit: int) -> str:
    """The refusal once a parent has used the day's questions (Erez 2026-10-13); the day ends at Israel midnight."""
    return (
        f"הגעתם למגבלת {limit} השאלות ליום. השאלות מתאפסות בחצות (שעון ישראל), ואז אפשר לשאול שוב.\n\n"
        f"You've used your {limit} questions for today. They reset at midnight (Israel time)."
    )


# Answers to a tap on a file link (Spec 14); there is no question text to take a language from, so both languages
FILE_GONE = (
    "הקובץ כבר לא זמין (ההודעה כבר לא ברשימת ההודעות האחרונות). אפשר לבקש אותה לפי שם השולח או התאריך.\n\n"
    "The file is no longer available (its message is not among the latest in the inbox). "
    "Ask for it by the sender or the date."
)
FILE_TOO_BIG = (
    f"הקובץ גדול מדי לשליחה כאן (מעל {MAX_FILE_BYTES // (1024 * 1024)}MB). אפשר לפתוח אותו בוובטופ.\n\n"
    f"The file is too large to send here (over {MAX_FILE_BYTES // (1024 * 1024)} MB). Open it in Webtop."
)
FILE_TYPE = (
    "אי אפשר לשלוח כאן קבצים מסוג זה. אפשר לפתוח אותו בוובטופ.\n\n"
    "This kind of file cannot be sent here. Open it in Webtop."
)
FILE_ERROR = (
    "לא הצלחתי להביא את הקובץ כרגע, נסו שוב בעוד רגע.\n\n"
    "Could not fetch the file right now, try again in a moment."
)
FILE_LIMIT = (
    "הגעתם למגבלת הקבצים לשעה. נסו שוב מאוחר יותר.\n\n"
    "You've reached the hourly file limit. Please try again later."
)
_FILE_REFUSALS = {"too_big": FILE_TOO_BIG, "type_refused": FILE_TYPE, "not_found": FILE_GONE}
FILE_LIMIT_PER_HOUR = 30


class Gateway:
    def __init__(
        self,
        allowlist: Allowlist,
        limiter: RateLimiter,
        agent_for: Callable[[str], Agent | None | Awaitable[Agent | None]],
        *,
        onboarding: OnboardingFlow | None = None,
        registry: AgentRegistry | None = None,
        invites: InviteService | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = datetime.now,
        file_limiter: RateLimiter | None = None,
        daily_cap: DailyCap | None = None,
    ):
        """Phase 4 mode: `agent_for` returns the one agent. Multi-parent mode: `agent_for` is the registry's
        async lookup, `onboarding` handles connecting and confirming kids before the agent, and `registry`
        tells when a session expired during an answer."""
        self._allow, self._limiter, self._agent_for = allowlist, limiter, agent_for
        self._onboarding, self._registry = onboarding, registry
        self._invites = invites
        self._conversations: dict[str, Conversation] = {}
        self._monotonic = monotonic
        self._clock = clock
        # a tap on a file link costs no model call, only an inbox read and a download: its own, separate limit
        self._file_limiter = file_limiter or RateLimiter(FILE_LIMIT_PER_HOUR, clock=clock)
        self._daily_cap = daily_cap  # None = no daily cap (dev, and prod until the setting is on)
        self._last_question: dict[
            str, float
        ] = {}  # parent -> when they last asked (for `follow_up`)

    @property
    def invites(self) -> InviteService | None:
        return self._invites

    async def handle(self, msg: Incoming) -> Reply:
        if self._invites is not None and (code := invite_code_of(msg.text or "")) is not None:
            if not self._allow.is_allowed(msg.parent_id):
                if not await self._invites.redeem(code, msg.parent_id):
                    log.info(
                        "denied parent=%s", msg.parent_id
                    )  # same reply as a stranger: nothing leaks
                    analytics.emit("access_denied", msg.parent_id, reason="bad_invite")
                    return Reply(text=PRIVATE_BETA)
            msg = Incoming(msg.parent_id, text="/start")  # past the door it is an ordinary start
        if not self._allow.is_allowed(msg.parent_id):
            log.info("denied parent=%s", msg.parent_id)
            analytics.emit("access_denied", msg.parent_id, reason="not_allowed")
            return Reply(text=PRIVATE_BETA)
        if (
            self._invites is not None
            and self._invites.is_admin(msg.parent_id)
            and (msg.text or "").strip().strip(".!?").lower() in INVITE_WORDS
        ):
            invite = await self._invites.create(msg.parent_id)
            return Reply(text=self._invites.invite_text(invite) if invite else INVITE_UNAVAILABLE)
        if self._onboarding is not None:
            handled = await self._onboarding.pre_agent(msg)
            if handled is not None:
                return handled
        if (tap := parse_file_start(msg.text or "")) is not None:
            return await self._send_file(msg.parent_id, *tap)
        if msg.text and msg.text.strip().lower().startswith("/start"):
            return Reply(text=WELCOME)
        question = (
            msg.text or ""
        ).strip()  # v1 has no buttons: a stale button press carries no text
        if not question:
            return Reply(text=ASK_SOMETHING)
        now = self._monotonic()
        last = self._last_question.get(msg.parent_id)
        self._last_question[msg.parent_id] = now
        labels = {
            "language": analytics.language_of(question),
            "follow_up": last is not None and now - last <= FOLLOW_UP_WINDOW,
        }
        wait = self._limiter.check(msg.parent_id)
        if wait > 0:
            analytics.emit("question", msg.parent_id, outcome="rate_limited", **labels)
            minutes = max(1, math.ceil(wait / 60))
            return Reply(
                text=f"You've reached the hourly question limit. Please try again in about {minutes} min."
            )
        # The day's cap counts only a question that is about to reach the model; a guest is capped, an admin is not.
        capped = self._daily_cap is not None and not (
            self._invites is not None and self._invites.is_admin(msg.parent_id)
        )
        if capped and not self._daily_cap.take(msg.parent_id):
            log.info("daily cap reached parent=%s cap=%d", msg.parent_id, self._daily_cap.limit)
            analytics.emit("question", msg.parent_id, outcome="rate_limited", **labels)
            return Reply(text=daily_cap_text(self._daily_cap.limit))
        conv = self._conversations.setdefault(msg.parent_id, Conversation())
        agent = self._agent_for(msg.parent_id)
        if inspect.isawaitable(agent):
            agent = await agent
        if agent is None:  # the session disappeared between the onboarding check and now
            if capped:
                self._daily_cap.refund(msg.parent_id)
            return (
                await self._onboarding.expired_reply(msg.parent_id)
                if self._onboarding is not None
                else Reply(text=ASK_SOMETHING)
            )
        result = await agent.ask(conv, question)
        answer_ms = round((self._monotonic() - now) * 1000)
        log.info(
            "answered parent=%s tools=%s in=%d out=%d cost_usd=%s err=%s",
            msg.parent_id,
            result.tool_calls,
            result.input_tokens,
            result.output_tokens,
            f"{result.cost_usd:.4f}" if result.cost_usd is not None else "unknown",
            result.error,
        )
        expired = (
            self._registry is not None
            and self._onboarding is not None
            and await self._registry.take_expired(msg.parent_id)
        )
        _question_event(msg.parent_id, agent, result, answer_ms, labels, expired=expired)
        # a failure on our side does not use up the parent's question
        if capped and (result.error or expired):
            self._daily_cap.refund(msg.parent_id)
        if expired:
            # Whatever the model said about it, the parent gets one clear notice and a reconnect button.
            return await self._onboarding.expired_reply(msg.parent_id)
        note = None if result.error else inbox_note(result.tool_uses, labels["language"], self._clock())
        text = f"{result.text}\n\n{note}" if note else result.text
        return Reply(text=text, offers={} if result.error else result.offers)

    async def _send_file(self, parent_id: str, msg_hash: str, index: int) -> Reply:
        """The parent tapped a file link (`/start file_...`): fetch that file from their own inbox and send it.
        No model is involved. Logs say that a file was sent and its type and size, never a name or a payload."""
        if self._file_limiter.check(parent_id) > 0:
            return Reply(text=FILE_LIMIT)
        agent = self._agent_for(parent_id)
        if inspect.isawaitable(agent):
            agent = await agent
        if agent is None:
            return (
                await self._onboarding.expired_reply(parent_id)
                if self._onboarding is not None
                else Reply(text=ASK_SOMETHING)
            )
        try:
            f = await agent.family.get_file(msg_hash, index)
        except FileUnavailable as e:
            log.info("file refused parent=%s reason=%s", parent_id, e.reason)
            return Reply(text=_FILE_REFUSALS.get(e.reason, FILE_ERROR))
        except SessionExpired:
            log.info("file session_expired parent=%s", parent_id)
            if self._onboarding is None:
                return Reply(text=FILE_ERROR)
            if self._registry is not None:
                await self._registry.take_expired(parent_id)
            return await self._onboarding.expired_reply(parent_id)
        except EasytopError as e:
            log.warning("file failed parent=%s error=%s", parent_id, type(e).__name__)
            return Reply(text=FILE_ERROR)
        log.info(
            "file sent parent=%s ext=%s kb=%d",
            parent_id,
            PurePosixPath(f.name).suffix.lower(),
            len(f.data) // 1024,
        )
        return Reply(documents=[Document(f.name, f.data, f.mime)])


def _outcome(result: AgentResult, category: str, *, expired: bool) -> str:
    if expired:
        return "session_expired"
    if result.error:
        return "error"
    if result.truncated:
        return "truncated"
    if result.hit_tool_limit:
        return "tool_limit"
    return "answered" if category not in ("none", "kids") else "answered_no_data"


def _question_event(
    parent_id: str,
    agent: Agent,
    result: AgentResult,
    answer_ms: int,
    labels: dict,
    *,
    expired: bool,
) -> None:
    category, focus, kids_scope = analytics.labels_for(result.tool_uses)
    model = getattr(agent, "model", None)
    analytics.emit(
        "question",
        parent_id,
        outcome=_outcome(result, category, expired=expired),
        category=category,
        focus=focus,
        kids_scope=kids_scope,
        model=model if model in analytics.MODELS else None,
        answer_ms=answer_ms,
        tool_count=len(result.tool_calls),
        tools=[t for t in result.tool_calls if t in analytics.TOOL_NAMES],
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        cost_usd=result.cost_usd,
        error_type=result.error,
        **labels,
    )
