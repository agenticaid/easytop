from datetime import UTC, datetime, timedelta

from easytop.access.invites import InviteService
from easytop.adapters.gateway import PRIVATE_BETA, Gateway
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.store.memory import InMemoryStore
from tests.agent.fakes import ScriptedClient

NOW = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)
ADMIN = "telegram:1"


class Harness:
    def __init__(self):
        self.client = ScriptedClient()
        self.store, self.allow = InMemoryStore(), Allowlist({ADMIN})
        self.now = NOW
        self.invites = InviteService(
            self.store,
            self.allow,
            link=lambda payload: f"https://t.me/bot?start={payload}",
            clock=lambda: self.now,
            ttl=timedelta(hours=5),
        )
        self.agent_calls = 0

        def agent_for(parent_id):
            self.agent_calls += 1
            return None

        self.gateway = Gateway(self.allow, RateLimiter(), agent_for, invites=self.invites)

    async def say(self, pid, text):
        return await self.gateway.handle(Incoming(pid, text=text))

    async def new_link(self):
        reply = await self.say(ADMIN, "invite")
        return next(w for w in reply.text.split() if w.startswith("https://t.me/"))


async def test_the_admin_asks_for_an_invite_in_a_word_and_gets_a_link_with_its_term():
    h = Harness()
    for word in ("invite", "הזמנה", " Invite. "):
        reply = await h.say(ADMIN, word)
        assert "https://t.me/bot?start=inv_" in reply.text
        assert "5 שעות" in reply.text and "5 hours" in reply.text


async def test_anyone_else_saying_invite_is_just_a_stranger():
    h = Harness()
    assert (await h.say("telegram:2", "invite")).text == PRIVATE_BETA
    h.allow.allow("telegram:3")  # a guest, not an admin
    reply = await h.say("telegram:3", "invite")
    assert "t.me" not in reply.text  # no link: it went on as an ordinary message


async def test_a_friend_who_opens_the_link_is_let_in_and_gets_the_connect_flow():
    h = Harness()
    link = await h.new_link()
    code = link.split("?start=")[1]
    reply = await h.say("telegram:50", f"/start {code}")
    assert reply.text != PRIVATE_BETA
    assert h.allow.is_allowed("telegram:50")
    assert (await h.store.list_allowed()) == [("telegram", "50")]


async def test_the_link_works_once_the_second_person_is_a_stranger():
    h = Harness()
    code = (await h.new_link()).split("?start=")[1]
    await h.say("telegram:50", f"/start {code}")
    assert (await h.say("telegram:51", f"/start {code}")).text == PRIVATE_BETA
    assert not h.allow.is_allowed("telegram:51")


async def test_an_expired_or_made_up_link_gets_exactly_the_stranger_reply():
    h = Harness()
    code = (await h.new_link()).split("?start=")[1]
    h.now = NOW + timedelta(hours=6)
    assert (await h.say("telegram:50", f"/start {code}")).text == PRIVATE_BETA
    assert (await h.say("telegram:50", "/start inv_madeup")).text == PRIVATE_BETA
    assert (await h.say("telegram:50", "/start")).text == PRIVATE_BETA
    assert h.agent_calls == 0 and not h.allow.is_allowed("telegram:50")


async def test_someone_already_allowed_who_opens_a_link_just_gets_the_normal_start():
    h = Harness()
    code = (await h.new_link()).split("?start=")[1]
    reply = await h.say(ADMIN, f"/start {code}")  # the admin tries their own link
    assert reply.text != PRIVATE_BETA
    # the link was NOT used up by someone who did not need it
    assert (await h.say("telegram:50", f"/start {code}")).text != PRIVATE_BETA
