"""Onboarding and reconnecting, as a conversation inside the chat (Spec 04). Channel-agnostic.

States come from the store, not from memory, so a restart loses nothing:
  no stored session                       -> not connected: the connect button (an expired session is the same
                                             state, shown with the "expired" wording when kids were confirmed)
  session, no confirmed kids              -> discover the kids, ask "Found: ... Correct?", wait for yes / no
  session and confirmed kids              -> ready: the message goes to the agent

The login itself happens elsewhere (a streamed browser inside a Mini App). It reports back through
`on_connected(parent_id, cookies)`, which stores the session, finds the kids and pushes the question to the
parent with `notify`. Replies carry the connect button as a `LinkButton`; v1 still sends no answer buttons,
so the confirmation is answered in words.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from easytop import analytics
from easytop.adapters.model import Incoming, LinkButton, Reply
from easytop.connector.base import Connector
from easytop.digest import nudge
from easytop.errors import EasytopError
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry, ConnectorFactory, store_parent
from easytop.onboarding.vault import SessionVault
from easytop.store.base import Store, StoredKid

log = logging.getLogger("easytop.onboarding")

Notifier = Callable[[str, Reply], Awaitable[None]]

BUTTON = "התחברות לוובטופ"
CONNECT_INTRO = (
    "שלום! כדי שאוכל לקרוא את הוובטופ של הילדים, צריך להתחבר פעם אחת. "
    "לוחצים על הכפתור ונכנסים לוובטופ כרגיל, כאן בתוך הצ'אט, עם הזדהות משרד החינוך. "
    "אני לא שומר את הסיסמה. אפשר גם להחליף בלשוניות שמעל לקוד בהודעה או לסיסמה.\n\n"
    "Hi! To read your kids' Webtop, sign in once: tap the button and log in right here in the chat, "
    "with your Ministry of Education account. The tabs on top switch to a message code or a password."
)
EXPIRED = (
    "החיבור לוובטופ פג. לוחצים על הכפתור כדי להתחבר מחדש.\n\n"
    "Your Webtop connection expired. Tap the button to reconnect."
)
NO_LOGIN_PAGE = (
    "ההתחברות לא זמינה כרגע. נסו שוב בעוד כמה דקות.\n\n"
    "Connecting is not available right now. Please try again in a few minutes."
)
LOOKUP_FAILED = (
    "התחברתי, אבל לא הצלחתי לקרוא את הוובטופ כרגע. שלחו הודעה בעוד כמה דקות ואנסה שוב.\n\n"
    "You are connected, but I could not read Webtop just now. Message me again in a few minutes."
)
NO_KIDS = (
    "לא מצאתי ילדים בחשבון הזה (אולי נכנסתם כמורים). אפשר להתחבר מחדש עם חשבון הורה.\n\n"
    "I found no children in this account. Tap the button to connect with a parent account."
)
WRONG_ACCOUNT = (
    "בסדר, ניתקתי את החיבור. אפשר להתחבר מחדש עם החשבון הנכון.\n\n"
    "OK, I disconnected. Tap the button to connect with the right account."
)
RECONNECTED = "החיבור לוובטופ חודש. אפשר לשאול.\n\nReconnected to Webtop. Go ahead and ask."

_YES = {"כן", "yes", "y", "yep", "yeah", "נכון", "correct", "ok", "okay", "אוקיי", "בטח", "סבבה"}
_NO = {"לא", "no", "n", "nope", "לא נכון", "wrong", "incorrect"}
_PUNCT = re.compile(r"[\s.!?,;:\"'()\-]+")


def _normalise(text: str) -> str:
    return _PUNCT.sub(" ", text.lower()).strip()


def _to_stored(kid) -> StoredKid:
    return StoredKid(
        webtop_kid_id=kid.id,
        name=kid.name,
        class_name=kid.class_name,
        school_id=kid.school.id,
        school_name=kid.school.name,
    )


def _question(kids: list[StoredKid]) -> str:
    multi_school = len({k.school_id for k in kids}) > 1
    lines = [
        f"**__{k.name}__** ({k.class_name})" + (f", {k.school_name}" if multi_school else "")
        for k in kids
    ]
    return (
        "מצאתי את הילדים האלה בוובטופ:\n"
        + "\n".join(lines)
        + '\n\nזה נכון? אפשר לענות "כן" או "לא".\n\n'
        + 'I found these kids in Webtop. Correct? Answer "yes" or "no".'
    )


async def _noop() -> None:
    return None


class OnboardingFlow:
    def __init__(
        self,
        store: Store,
        vault: SessionVault,
        registry: AgentRegistry,
        links: ConnectLinks,
        *,
        connector_factory: ConnectorFactory,
        connect_url: Callable[[str], str | None],
        welcome: str = "",
    ):
        self._store, self._vault, self._registry, self._links = store, vault, registry, links
        self._connector_factory = connector_factory
        self._connect_url = connect_url
        self._welcome = welcome
        self._pending: dict[str, list[StoredKid]] = {}  # parent_id -> kids waiting for "yes"
        self._offering: set[str] = (
            set()
        )  # parents just asked about the nudge: their next message answers it
        self.notify: Notifier | None = None  # set by the adapter: pushes a Reply to a parent

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def links(self) -> ConnectLinks:
        return self._links

    # ---- called for every message, before the agent ----

    async def pre_agent(self, msg: Incoming) -> Reply | None:
        """A Reply when onboarding handles this message, or None when the parent is ready for the agent."""
        pid = msg.parent_id
        sp = await store_parent(self._store, pid)
        if await self._vault.load(sp.id) is None:
            had_kids = bool(await self._store.list_kids(sp.id))
            if had_kids:
                return self._connect_reply(pid, EXPIRED, reason="expired")
            return self._connect_reply(pid, CONNECT_INTRO, reason="first")
        if pid in self._pending:
            return await self._answer_confirmation(pid, sp.id, msg.text or "")
        if await self._store.list_kids(sp.id):
            return await self._nudge_words(pid, sp.id, msg.text or "")
        return await self._discover_and_ask(pid, sp.id)

    async def expired_reply(self, parent_id: str) -> Reply:
        return self._connect_reply(parent_id, EXPIRED, reason="expired")

    # ---- called by the login page when a parent finished signing in to Webtop ----

    async def on_connected(self, parent_id: str, cookies: dict[str, str]) -> None:
        sp = await store_parent(self._store, parent_id)
        await self._vault.save(sp.id, cookies)
        await self._registry.invalidate(parent_id)
        self._pending.pop(parent_id, None)
        self._offering.discard(parent_id)
        reply = await self._discover_and_ask(parent_id, sp.id, reconnect=True)
        if self.notify is not None:
            await self.notify(parent_id, reply)

    # ---- internals ----

    def _connect_reply(self, parent_id: str, text: str, *, reason: str) -> Reply:
        url = self._connect_url(self._links.issue(parent_id))
        if url is None:
            log.warning("no login page available parent=%s", parent_id)
            return Reply(text=NO_LOGIN_PAGE)
        analytics.emit("connect_offered", parent_id, reason=reason)
        return Reply(text=text, link=LinkButton(BUTTON, url))

    async def _discover(self, parent_id: str) -> list[StoredKid]:
        sp = await store_parent(self._store, parent_id)
        cookies = await self._vault.load(sp.id)
        if cookies is None:
            raise EasytopError("no session")
        connector: Connector = self._connector_factory(cookies, _noop)
        try:
            return [_to_stored(k) for k in await connector.list_kids()]
        finally:
            close = getattr(connector, "aclose", None)
            if close is not None:
                await close()

    async def _discover_and_ask(self, pid: str, store_id: str, *, reconnect: bool = False) -> Reply:
        try:
            found = await self._discover(pid)
        except EasytopError as e:
            log.warning("kid lookup failed parent=%s err=%s", pid, type(e).__name__)
            analytics.emit_error("onboarding", e)
            return Reply(text=LOOKUP_FAILED)
        if not found:
            await self._vault.clear(store_id)
            await self._registry.invalidate(pid)
            return self._connect_reply(pid, NO_KIDS, reason="no_kids")
        known = await self._store.list_kids(store_id)
        if reconnect and {k.webtop_kid_id for k in known} == {k.webtop_kid_id for k in found}:
            _kids_event("kids_confirmed", pid, found, reconnect=True)
            return Reply(text=RECONNECTED)  # the same kids as before: nothing to confirm
        self._pending[pid] = found
        return Reply(text=_question(found))

    async def _answer_confirmation(self, pid: str, store_id: str, text: str) -> Reply:
        answer = _normalise(text)
        if answer in _YES:
            had_kids = bool(await self._store.list_kids(store_id))
            kids = self._pending.pop(pid)
            await self._store.save_kids(store_id, kids)
            _kids_event("kids_confirmed", pid, kids, reconnect=had_kids)
            if (await self._store.get_digest(store_id)).enabled:
                return Reply(
                    text=self._welcome
                )  # already has the nudge (reconnected): offer nothing
            self._offering.add(pid)
            return Reply(text=self._welcome + "\n\n" + nudge.OFFER)
        if answer in _NO:
            analytics.emit("kids_rejected", pid, kid_count=len(self._pending.pop(pid, [])))
            await self._vault.clear(store_id)
            await self._registry.invalidate(pid)
            return self._connect_reply(pid, WRONG_ACCOUNT, reason="wrong_account")
        return Reply(text=_question(self._pending[pid]))  # unclear: ask the same question again

    async def _nudge_words(self, pid: str, store_id: str, text: str) -> Reply | None:
        """The nudge offer right after onboarding, and the reminder commands later. Plain deterministic words,
        no model call. None means the message is for the agent."""
        if pid in self._offering:
            self._offering.discard(pid)  # asked once: whatever the answer is, never asked again
            if (when := nudge.parse_time(text)) is not None:
                return await self._set_nudge(pid, store_id, when)
            if _normalise(text) in _NO:
                analytics.emit("nudge_setting", pid, state="declined")
                return Reply(text=nudge.DECLINED)
            return None
        command = nudge.parse_command(text)
        if command == "off":
            await nudge.StoreReminders(self._store, store_id, pid).set(None)
            return Reply(text=nudge.OFF)
        if command == "ask":  # a request with no usable time: ask once, the answer goes through the offer path above
            self._offering.add(pid)
            return Reply(text=nudge.ASK)
        if command is not None:
            return await self._set_nudge(pid, store_id, command)
        return None

    async def _set_nudge(self, pid: str, store_id: str, when) -> Reply:
        await nudge.StoreReminders(self._store, store_id, pid).set(when)
        return Reply(text=nudge.ON.format(time=when.strftime("%H:%M")))


def _kids_event(event: str, parent_id: str, kids: list[StoredKid], *, reconnect: bool) -> None:
    """Counts only: how many kids and schools, never a name."""
    analytics.emit(
        event,
        parent_id,
        kid_count=len(kids),
        school_count=len({k.school_id for k in kids}),
        reconnect=reconnect,
    )
