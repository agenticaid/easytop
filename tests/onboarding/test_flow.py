from datetime import date

import pytest

from easytop.adapters.gateway import WELCOME
from easytop.adapters.model import Incoming, Reply
from easytop.connector.fake import FakeConnector
from easytop.errors import UpstreamError
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.flow import OnboardingFlow
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry, store_parent
from easytop.onboarding.vault import SessionVault
from easytop.store.memory import InMemoryStore

COOKIES = {"webToken": "tok"}
BASE = "https://login.example"


class StubAgent:
    """Stands in for an Agent where only 'a session exists' matters."""

    def bind_reminders(self, reminders):
        pass


class Harness:
    def __init__(self, *, base=BASE, connector=None, store=None, cipher=None):
        self.store = store or InMemoryStore()
        self.vault = SessionVault(self.store, cipher or SessionCipher(new_key()))
        self.links = ConnectLinks()
        self.pushed: list[tuple[str, Reply]] = []
        self.connector_error: Exception | None = None
        self.connector_cls = connector

        def make(cookies, on_expired):
            if self.connector_error:
                raise self.connector_error
            return self.connector_cls() if self.connector_cls else FakeConnector(date(2026, 10, 4))

        self.registry = AgentRegistry(
            self.store, self.vault, connector_factory=make, agent_factory=lambda c: StubAgent()
        )
        self.flow = OnboardingFlow(
            self.store,
            self.vault,
            self.registry,
            self.links,
            connector_factory=make,
            connect_url=lambda token: f"{base}/?t={token}" if base else None,
            welcome=WELCOME,
        )

        async def notify(pid, reply):
            self.pushed.append((pid, reply))

        self.flow.notify = notify

    async def say(self, pid, text):
        return await self.flow.pre_agent(Incoming(pid, text=text))

    async def connect(self, pid, cookies=COOKIES):
        await self.flow.on_connected(pid, cookies)

    async def sid(self, pid):
        return (await store_parent(self.store, pid)).id


def token_of(reply: Reply) -> str:
    return reply.link.url.split("?t=")[1]


async def test_an_unconnected_parent_gets_the_connect_button_for_any_message():
    h = Harness()
    for text in ("/start", "מה הילדים צריכים מחר?"):
        r = await h.say("telegram:1", text)
        assert r.link is not None and r.link.url.startswith(f"{BASE}/?t=")
        assert "וובטופ" in r.text and "Webtop" in r.text  # Hebrew first, one English line
        assert h.links.peek(token_of(r)) == "telegram:1"


async def test_without_a_login_page_the_parent_is_told_to_wait_not_sent_nowhere():
    h = Harness(base=None)
    r = await h.say("telegram:1", "hi")
    assert r.link is None and "not available" in r.text


async def test_after_login_the_kids_are_found_and_the_parent_is_asked_to_confirm():
    h = Harness()
    await h.connect("telegram:1")
    ((pid, reply),) = h.pushed
    assert pid == "telegram:1"
    assert "**__Noa__**" in reply.text and "**__Ido__**" in reply.text
    assert "כן" in reply.text and "yes" in reply.text.lower()
    assert await h.vault.load(await h.sid("telegram:1")) == COOKIES
    assert await h.store.list_kids(await h.sid("telegram:1")) == []  # not confirmed yet


@pytest.mark.parametrize("yes", ["כן", "כן.", " Yes ", "yes!", "נכון", "ok"])
async def test_yes_confirms_the_kids_and_the_parent_is_ready(yes):
    h = Harness()
    await h.connect("telegram:1")
    r = await h.say("telegram:1", yes)
    assert "מה הילדים צריכים מחר" in r.text  # the welcome that teaches the free-language method
    kids = await h.store.list_kids(await h.sid("telegram:1"))
    assert sorted(k.name for k in kids) == ["Ido", "Noa"]
    assert await h.say("telegram:1", "מה הילדים צריכים מחר?") is None  # now the agent takes over


@pytest.mark.parametrize("no", ["לא", "no", "No.", "לא נכון"])
async def test_no_drops_the_session_and_offers_to_connect_again(no):
    h = Harness()
    await h.connect("telegram:1")
    r = await h.say("telegram:1", no)
    assert r.link is not None
    sid = await h.sid("telegram:1")
    assert await h.vault.load(sid) is None and await h.store.list_kids(sid) == []


async def test_an_unclear_answer_asks_again_and_changes_nothing():
    h = Harness()
    await h.connect("telegram:1")
    r = await h.say("telegram:1", "what is the weather?")
    assert "**__Noa__**" in r.text and "yes" in r.text.lower()
    assert await h.store.list_kids(await h.sid("telegram:1")) == []


async def test_after_a_restart_a_connected_unconfirmed_parent_is_asked_again():
    store, cipher = InMemoryStore(), SessionCipher(new_key())
    first = Harness(store=store, cipher=cipher)
    await first.connect("telegram:1")
    second = Harness(store=store, cipher=cipher)  # the pending confirmation was only in memory
    r = await second.say("telegram:1", "hello")
    assert "**__Noa__**" in r.text


async def test_a_failed_kid_lookup_is_a_friendly_retry_and_keeps_the_session():
    h = Harness()
    h.connector_error = UpstreamError("down")
    await h.connect("telegram:1")
    ((_, reply),) = h.pushed
    assert "Webtop" in reply.text and reply.link is None
    h.connector_error = None
    r = await h.say("telegram:1", "hello")  # the next message retries the lookup
    assert "**__Noa__**" in r.text


async def test_an_account_without_kids_is_refused_and_forgotten():
    class NoKids(FakeConnector):
        async def list_kids(self):
            return []

    h = Harness(connector=lambda: NoKids(date(2026, 10, 4)))
    await h.connect("telegram:1")
    ((_, reply),) = h.pushed
    assert reply.link is not None
    assert await h.vault.load(await h.sid("telegram:1")) is None


async def test_reconnecting_with_the_same_kids_needs_no_confirmation():
    h = Harness()
    await h.connect("telegram:1")
    await h.say("telegram:1", "כן")
    await h.registry._on_expired("telegram:1", await h.sid("telegram:1"))  # the session dies
    r = await h.say("telegram:1", "מה מחר?")
    assert r.link is not None and "פג" in r.text  # expired notice + reconnect button
    h.pushed.clear()
    await h.connect("telegram:1", {"webToken": "fresh"})
    ((_, reply),) = h.pushed
    assert reply.link is None and "yes" not in reply.text.lower()
    assert await h.say("telegram:1", "מה מחר?") is None
    assert await h.vault.load(await h.sid("telegram:1")) == {"webToken": "fresh"}


async def test_reconnecting_with_different_kids_asks_to_confirm_again():
    class Other(FakeConnector):
        async def list_kids(self):
            return (await super().list_kids())[:1]

    h = Harness()
    await h.connect("telegram:1")
    await h.say("telegram:1", "כן")
    await h.registry._on_expired("telegram:1", await h.sid("telegram:1"))
    h.connector_cls = lambda: Other(date(2026, 10, 4))
    h.pushed.clear()
    await h.connect("telegram:1", {"webToken": "other-account"})
    ((_, reply),) = h.pushed
    assert "yes" in reply.text.lower()
    assert await h.store.list_kids(await h.sid("telegram:1")) != []  # old kids stay until confirmed
    assert (await h.say("telegram:1", "כן")) is not None


async def test_expired_reply_carries_a_fresh_connect_link():
    h = Harness()
    r = await h.flow.expired_reply("telegram:1")
    assert r.link is not None and "פג" in r.text
    assert h.links.peek(token_of(r)) == "telegram:1"


async def test_parents_are_independent():
    h = Harness()
    await h.connect("telegram:1")
    assert (await h.say("telegram:2", "hi")).link is not None  # parent 2 is not connected
    assert "**__Noa__**" in (await h.say("telegram:1", "hi")).text


# ---- the nudge: offered once, answered in words (Phase 6, D5/D6) ----


async def _ready_and_offered(h, pid="telegram:1"):
    await h.connect(pid)
    return await h.say(pid, "כן")


async def test_after_the_kids_are_confirmed_the_nudge_is_offered_once():
    from easytop.digest.nudge import OFFER

    h = Harness()
    r = await _ready_and_offered(h)
    assert r.text == WELCOME + "\n\n" + OFFER
    assert h.flow._offering == {"telegram:1"}


async def test_a_time_turns_the_nudge_on_and_never_reaches_the_agent():
    from datetime import time

    from easytop.digest.nudge import ON
    from easytop.store.base import DigestSetting

    h = Harness()
    await _ready_and_offered(h)
    r = await h.say("telegram:1", "19:00")
    assert r is not None and r.text == ON.format(time="19:00")
    assert await h.store.get_digest(await h.sid("telegram:1")) == DigestSetting(True, time(19))
    assert await h.say("telegram:1", "מה מחר?") is None  # the offer is over, the agent takes over


async def test_declining_says_once_that_the_summary_can_be_asked_any_time():
    from easytop.digest.nudge import DECLINED
    from easytop.store.base import DigestSetting

    h = Harness()
    await _ready_and_offered(h)
    r = await h.say("telegram:1", "לא")
    assert r is not None and r.text == DECLINED
    assert await h.store.get_digest(await h.sid("telegram:1")) == DigestSetting(False, None)
    assert await h.say("telegram:1", "לא") is None  # not asked again: "no" is now just a message


async def test_any_other_answer_goes_to_the_agent_and_the_offer_is_dropped():
    from easytop.store.base import DigestSetting

    h = Harness()
    await _ready_and_offered(h)
    assert await h.say("telegram:1", "מה הילדים צריכים מחר?") is None
    assert h.flow._offering == set()
    assert await h.say("telegram:1", "19:00") is None  # too late, it is a plain message now
    assert await h.store.get_digest(await h.sid("telegram:1")) == DigestSetting(False, None)


async def test_a_ready_parent_changes_the_reminder_in_words_without_the_agent():
    from datetime import time

    from easytop.digest.nudge import OFF, ON
    from easytop.store.base import DigestSetting

    h = Harness()
    await h.connect("telegram:1")
    await h.say("telegram:1", "כן")
    await h.say("telegram:1", "x")  # drop the offer
    sid = await h.sid("telegram:1")
    assert (await h.say("telegram:1", "תזכורת 20:00")).text == ON.format(time="20:00")
    assert await h.store.get_digest(sid) == DigestSetting(True, time(20))
    assert (await h.say("telegram:1", "reminder 7:30")).text == ON.format(time="07:30")
    assert (await h.say("telegram:1", "תזכורת כבוי")).text == OFF
    assert await h.store.get_digest(sid) == DigestSetting(False, None)


async def _ready(h):
    await h.connect("telegram:1")
    await h.say("telegram:1", "כן")
    await h.say("telegram:1", "x")  # drop the offer
    return await h.sid("telegram:1")


async def test_changing_the_time_in_words_keeps_a_content_choice():
    from datetime import time

    from easytop.digest.nudge import ON
    from easytop.store.base import DigestSetting

    h = Harness()
    sid = await _ready(h)
    await h.store.set_digest(sid, DigestSetting(True, time(17, 3), "due"))
    assert (await h.say("telegram:1", "reminder 20:00")).text == ON.format(time="20:00")
    assert await h.store.get_digest(sid) == DigestSetting(True, time(20), "due")


async def test_a_reminder_request_in_free_words_sets_it_without_the_agent():
    from datetime import time

    from easytop.digest.nudge import OFF, ON
    from easytop.store.base import DigestSetting

    h = Harness()
    sid = await _ready(h)
    assert (await h.say("telegram:1", "set a reminder for 8pm")).text == ON.format(time="20:00")
    assert await h.store.get_digest(sid) == DigestSetting(True, time(20))
    assert (await h.say("telegram:1", "תזכיר לי ב־19:30")).text == ON.format(time="19:30")
    assert (await h.say("telegram:1", "turn off the reminder")).text == OFF
    assert await h.store.get_digest(sid) == DigestSetting(False, None)


async def test_a_reminder_request_without_a_time_asks_for_one_and_then_sets_it():
    from datetime import time

    from easytop.digest.nudge import ASK, ON
    from easytop.store.base import DigestSetting

    h = Harness()
    sid = await _ready(h)
    assert (await h.say("telegram:1", "create a reminder")).text == ASK
    assert await h.store.get_digest(sid) == DigestSetting(False, None)  # nothing is set yet
    assert (await h.say("telegram:1", "20:00")).text == ON.format(time="20:00")
    assert await h.store.get_digest(sid) == DigestSetting(True, time(20))


async def test_the_time_question_ends_when_the_parent_asks_something_else():
    from easytop.digest.nudge import ASK

    h = Harness()
    await _ready(h)
    assert (await h.say("telegram:1", "תוסיף תזכורת")).text == ASK
    assert await h.say("telegram:1", "מה הילדים צריכים מחר?") is None  # the agent answers
    assert h.flow._offering == set()


async def test_a_question_that_only_mentions_reminders_goes_to_the_agent():
    h = Harness()
    await _ready(h)
    for text in ("any reminders from school?", "remind me to bring the ruler", "יש תזכורת מהמורה?"):
        assert await h.say("telegram:1", text) is None


async def test_a_parent_who_is_not_ready_cannot_use_the_reminder_commands():
    h = Harness()
    r = await h.say(
        "telegram:1", "תזכורת 20:00"
    )  # not connected: the connect button, nothing stored
    assert r.link is not None
    assert (await h.store.get_digest(await h.sid("telegram:1"))).enabled is False


async def test_a_parent_who_already_has_the_nudge_is_not_offered_it_again():
    from datetime import time

    from easytop.store.base import DigestSetting

    h = Harness()
    await h.connect("telegram:1")
    await h.store.set_digest(await h.sid("telegram:1"), DigestSetting(True, time(19)))
    r = await h.say("telegram:1", "כן")
    assert r.text == WELCOME and h.flow._offering == set()


def test_the_connect_message_points_to_the_ministry_sign_in_not_to_sms():
    from easytop.onboarding.flow import CONNECT_INTRO

    assert "משרד החינוך" in CONNECT_INTRO and "Ministry of Education" in CONNECT_INTRO
    assert "SMS" not in CONNECT_INTRO  # the old advice: the code login's session was the short one
