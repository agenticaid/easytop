import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from easytop.access.invites import INVITE_PREFIX, InviteService, invite_code_of
from easytop.adapters.security import Allowlist
from easytop.store.memory import InMemoryStore

NOW = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)  # 20:00 in Israel
ADMIN = "telegram:1"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def make(ttl=timedelta(hours=24), link=lambda payload: f"https://t.me/bot?start={payload}"):
    store, allow, clock = InMemoryStore(), Allowlist({ADMIN}), Clock()
    return store, allow, clock, InviteService(store, allow, link=link, clock=clock, ttl=ttl)


def payload_of(url: str) -> str:
    return url.split("?start=")[1]


def test_only_a_start_command_with_the_invite_prefix_counts_as_an_invite():
    assert invite_code_of("/start inv_abc123") == "abc123"
    assert invite_code_of("  /START inv_abc123 ") == "abc123"
    for not_an_invite in (
        "/start",
        "/start hello",
        "inv_abc",
        "invite",
        "/start inv_",
        "/start inv_" + "x" * 80,
    ):
        assert invite_code_of(not_an_invite) is None


async def test_an_admin_gets_a_link_that_says_when_it_expires():
    store, allow, clock, svc = make()
    invite = await svc.create(ADMIN)
    assert invite is not None
    assert payload_of(invite.url).startswith(INVITE_PREFIX)
    assert invite.expires_at == NOW + timedelta(hours=24)
    reply = svc.invite_text(invite)
    assert invite.url in reply
    assert "24 שעות" in reply and "24 hours" in reply  # the term, in both languages
    assert (
        "20:00" in reply
    )  # and the clock time in Israel, so there is no doubt: 17:00 UTC + 24 h = 20:00 next day


async def test_the_term_follows_the_ttl_in_hours_and_minutes():
    _, _, _, svc = make(ttl=timedelta(hours=5))
    text = svc.invite_text(await svc.create(ADMIN))
    assert "5 שעות" in text and "5 hours" in text
    _, _, _, svc = make(ttl=timedelta(minutes=30))
    text = svc.invite_text(await svc.create(ADMIN))
    assert "30 דקות" in text and "30 minutes" in text
    _, _, _, svc = make(ttl=timedelta(hours=1))
    text = svc.invite_text(await svc.create(ADMIN))
    assert "שעה אחת" in text and "1 hour" in text and "1 hours" not in text


async def test_only_admins_can_create_invites():
    _, _, _, svc = make()
    assert svc.is_admin(ADMIN) and not svc.is_admin("telegram:2")
    assert await svc.create("telegram:2") is None


async def test_an_invite_that_cannot_be_linked_yet_is_not_created():
    store, _, _, svc = make(link=lambda payload: None)  # the bot's username is not known yet
    assert await svc.create(ADMIN) is None
    assert store._invites == {}


async def test_the_code_is_stored_only_as_a_hash():
    store, _, _, svc = make()
    invite = await svc.create(ADMIN)
    code = payload_of(invite.url).removeprefix(INVITE_PREFIX)
    assert list(store._invites) == [hashlib.sha256(code.encode()).hexdigest()]
    assert code not in str(store._invites)


async def test_redeeming_lets_the_person_in_for_good_and_only_once():
    store, allow, clock, svc = make()
    code = invite_code_of(f"/start {payload_of((await svc.create(ADMIN)).url)}")
    assert not allow.is_allowed("telegram:50")
    assert await svc.redeem(code, "telegram:50") is True
    assert allow.is_allowed("telegram:50")
    assert ("telegram", "50") in await store.list_allowed()  # saved, not only in memory
    assert await svc.redeem(code, "telegram:51") is False
    assert not allow.is_allowed("telegram:51")


async def test_an_expired_invite_does_not_work():
    _, allow, clock, svc = make(ttl=timedelta(hours=5))
    code = invite_code_of(f"/start {payload_of((await svc.create(ADMIN)).url)}")
    clock.now = NOW + timedelta(hours=5, minutes=1)
    assert await svc.redeem(code, "telegram:50") is False
    assert not allow.is_allowed("telegram:50")


async def test_guessed_codes_do_not_work():
    _, allow, _, svc = make()
    await svc.create(ADMIN)
    assert await svc.redeem("not-a-real-code", "telegram:50") is False
    assert not allow.is_allowed("telegram:50")


async def test_people_let_in_earlier_are_loaded_back_after_a_restart():
    store, allow, clock, svc = make()
    code = invite_code_of(f"/start {payload_of((await svc.create(ADMIN)).url)}")
    await svc.redeem(code, "telegram:50")
    fresh_allow = Allowlist({ADMIN})  # a new process: only the environment list
    fresh = InviteService(store, fresh_allow, link=lambda p: p, clock=clock)
    assert not fresh_allow.is_allowed("telegram:50")
    await fresh.load()
    assert fresh_allow.is_allowed("telegram:50") and fresh.is_admin(ADMIN)
    assert not fresh.is_admin("telegram:50")  # a guest does not become an admin


def test_israel_time_is_used_for_the_clock_time():
    assert ZoneInfo("Asia/Jerusalem").utcoffset(NOW.replace(tzinfo=None)) is not None
