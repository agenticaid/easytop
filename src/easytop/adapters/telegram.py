"""Telegram adapter. Rendering and update conversion are pure functions (unit-tested, no live bot).
`run_polling` is a thin python-telegram-bot runner — not exercised overnight (needs a real bot token).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from typing import Any

from easytop import analytics
from easytop.adapters.gateway import Gateway
from easytop.adapters.model import Button, Incoming, LinkButton, Reply
from easytop.files import parse_file_start, refs_in, strip_markers
from easytop.models import FileOffer

MAX_LEN = 4096
log = logging.getLogger("easytop.telegram")


@dataclass(frozen=True)
class RenderedMessage:
    text: str  # Telegram HTML
    buttons: list[list[tuple[str, str]]]  # rows of (label, callback_data)
    link: LinkButton | None = None  # sent as a Mini App button


_BOLD = re.compile(r"\*\*(?=\S)([^*\n]*?\S)\*\*")
_UNDERLINE = re.compile(r"(?<![\w_])__(?=\S)([^_\n]*?\S)__(?![\w_])")


def _model_text(text: str) -> str:
    """Escape for Telegram HTML, then turn the model's **bold** into <b> and __underline__ into <u>.
    Escaping first keeps it safe."""
    return _UNDERLINE.sub(r"<u>\1</u>", _BOLD.sub(r"<b>\1</b>", escape(text)))


def _blocks(reply: Reply) -> list[str]:
    blocks = []
    if reply.text:
        blocks.append(_model_text(reply.text))
    for s in reply.sections:
        head = f"<b>{escape(s.name)}</b>" + (f" ({escape(s.class_name)})" if s.class_name else "")
        blocks.append("\n".join([head, *(f"• {escape(line)}" for line in s.lines)]))
    if reply.files:
        blocks.append(
            "\n".join(
                f'📎 <a href="{escape(f.url, quote=True)}">{escape(f.name)}</a>'
                for f in reply.files
            )
        )
    if reply.footer:
        blocks.append(f"<i>{escape(reply.footer)}</i>")
    return blocks


def _split(blocks: list[str], limit: int = MAX_LEN) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for b in blocks:
        while len(b) > limit:  # a single oversized block: hard-split on lines/space
            cut = b.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            piece, b = b[:cut], b[cut:].lstrip("\n")
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if cur and len(cur) + 2 + len(b) > limit:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _size_text(kb: int) -> str:
    if kb <= 0:
        return ""
    return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb} KB"


def _file_line(o: FileOffer, bot_username: str | None) -> str:
    """`📎 <link>name.pdf</link> · 433 KB · Dana Levi, 3.10`. The link is a deep link to the bot: tapping it makes
    the parent's Telegram send `/start <payload>`, and the gateway answers with the file (no model involved)."""
    shown = o.name if not o.ext or o.name.lower().endswith(o.ext.lower()) else o.name + o.ext
    label = escape(shown)
    if bot_username:
        url = escape(f"https://t.me/{bot_username}?start={o.payload}", quote=True)
        label = f'<a href="{url}">{label}</a>'
    parts = [label, _size_text(o.size_kb), escape(f"{o.sender}, {o.sent}")]
    return "📎 " + " · ".join(p for p in parts if p)


def _with_file_links(chunk: str, reply: Reply, bot_username: str | None) -> list[str]:
    """One chat message with the markers of the messages it cites removed, and the files of those messages
    listed at its END (as its own chat message right after when they would not fit)."""
    refs = refs_in(chunk)
    clean = strip_markers(chunk)
    lines = list(
        dict.fromkeys(
            _file_line(o, bot_username) for ref in refs for o in reply.offers.get(ref, ())
        )
    )
    if not lines:
        return [clean]
    block = "\n".join(lines)
    if len(clean) + 2 + len(block) <= MAX_LEN:
        return [f"{clean}\n\n{block}"]
    return [clean, block]


def render(reply: Reply, bot_username: str | None = None) -> list[RenderedMessage]:
    blocks = _blocks(reply)
    if not blocks and reply.documents:
        return []  # a file on its own: nothing to say
    texts = [t for chunk in _split(blocks) for t in _with_file_links(chunk, reply, bot_username)]
    rows = [[(b.label, b.id) for b in reply.buttons]] if reply.buttons else []
    last = len(texts) - 1
    return [
        RenderedMessage(t, rows if i == last else [], reply.link if i == last else None)
        for i, t in enumerate(texts)
    ]


def incoming_from_update(update: Any) -> Incoming | None:
    """Convert a telegram Update (duck-typed) into a neutral Incoming, or None if irrelevant."""
    cq = getattr(update, "callback_query", None)
    if cq is not None:
        return Incoming(parent_id=f"telegram:{cq.from_user.id}", button=cq.data)
    msg = getattr(update, "message", None)
    if msg is not None and getattr(msg, "text", None):
        return Incoming(parent_id=f"telegram:{msg.from_user.id}", text=msg.text)
    return None


def chat_id_of(parent_id: str) -> int:
    return int(parent_id.split(":", 1)[1])


class TelegramAdapter:
    def __init__(self, gateway: Gateway):
        self._gateway = gateway
        self._bot: Any = None
        self.username: str | None = None  # the bot's own username, known once it has started

    async def on_update(self, update: Any, context: Any) -> None:
        incoming = incoming_from_update(update)
        if incoming is None:
            return
        if getattr(update, "callback_query", None) is not None:
            await update.callback_query.answer()
        chat_id = chat_id_of(incoming.parent_id)
        await context.bot.send_chat_action(chat_id, "typing")  # answers take 10-30 s
        reply = await self._gateway.handle(incoming)
        await self.send(context.bot, chat_id, reply)
        if reply.documents and parse_file_start(incoming.text or "") is not None:
            await self._delete_tap(context.bot, chat_id, update)

    async def send(self, bot: Any, chat_id: int, reply: Reply) -> None:
        """Also the proactive path (digest): same rendering, no incoming message needed."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

        for m in render(reply, self.username):
            rows = [
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in m.buttons
            ]
            if m.link is not None:
                rows.append([InlineKeyboardButton(m.link.label, web_app=WebAppInfo(m.link.url))])
            markup = InlineKeyboardMarkup(rows) if rows else None
            await bot.send_message(chat_id, m.text, parse_mode="HTML", reply_markup=markup)
        for d in reply.documents:
            await bot.send_document(chat_id, document=d.data, filename=d.name)

    @staticmethod
    async def _delete_tap(bot: Any, chat_id: int, update: Any) -> None:
        """The parent's own `/start file_...` message, left in the chat by tapping a file link: removed once the
        file is sent. Best effort, a failure changes nothing."""
        message_id = getattr(getattr(update, "message", None), "message_id", None)
        if message_id is None:
            return
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:  # noqa: BLE001 - e.g. too old, or the chat does not allow it
            log.info("could not remove the file tap message: %s", type(e).__name__)

    async def _remember_bot(self, app: Any) -> None:
        self._bot = app.bot
        self.username = getattr(app.bot, "username", None)

    def invite_link(self, payload: str) -> str | None:
        """`t.me/<bot>?start=<payload>`, or None before the bot's username is known."""
        return f"https://t.me/{self.username}?start={payload}" if self.username else None

    async def notify(self, parent_id: str, reply: Reply) -> None:
        """Push a message to a parent outside a request (the login page finished). No-op before start-up."""
        if self._bot is None:
            log.warning("cannot notify before the bot has started")
            return
        await self.send(self._bot, chat_id_of(parent_id), reply)

    async def on_error(self, update: Any, context: Any) -> None:
        """Errors from handlers and from polling (network drops after the Mac wakes). Logged by type only:
        the text of a network error can hold the request URL, which holds the bot token."""
        log.warning("telegram error: %s", type(context.error).__name__)
        analytics.emit_error("telegram", context.error)

    def run_polling(
        self,
        token: str,
        *,
        on_start: Callable[[], Awaitable[None]] | None = None,
        on_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:  # pragma: no cover - needs a real bot token
        from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

        async def post_init(app: Any) -> None:
            await self._remember_bot(app)
            if on_start is not None:
                await on_start()

        async def post_shutdown(app: Any) -> None:
            if on_stop is not None:
                await on_stop()

        app = (
            Application.builder()
            .token(token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
        app.add_handler(MessageHandler(filters.TEXT, self.on_update))
        app.add_handler(CallbackQueryHandler(self.on_update))
        app.add_error_handler(self.on_error)
        # bootstrap_retries=-1: keep retrying the first connection instead of exiting when the network is
        # not back yet (launch at login, wake from sleep).
        app.run_polling(bootstrap_retries=-1)


__all__ = [
    "Button",
    "RenderedMessage",
    "TelegramAdapter",
    "chat_id_of",
    "incoming_from_update",
    "render",
]
