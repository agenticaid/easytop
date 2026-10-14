"""The Store contract, run against the in-memory store and the Supabase store (against a fake Data API; and
against the real dev database when EASYTOP_LIVE_STORE=1 with SUPABASE_URL_DEV / SUPABASE_SERVICE_KEY_DEV set)."""

import os
import uuid
from datetime import time

import pytest

from easytop.store.base import DigestSetting, StoredKid
from easytop.store.file import FileStore
from easytop.store.memory import InMemoryStore
from easytop.store.supabase import SupabaseStore
from tests.store.fake_postgrest import FakePostgrest

LIVE = os.environ.get("EASYTOP_LIVE_STORE") == "1"
PARAMS = [
    "memory",
    "file",
    "supabase-fake",
    pytest.param(
        "supabase-live",
        marks=pytest.mark.skipif(
            not LIVE,
            reason="live store test: set EASYTOP_LIVE_STORE=1 with SUPABASE_URL_DEV and SUPABASE_SERVICE_KEY_DEV in .env",
        ),
    ),
]


@pytest.fixture(params=PARAMS)
async def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    elif request.param == "file":
        yield FileStore(tmp_path / "store.json")
    elif request.param == "supabase-fake":
        fake = FakePostgrest()
        s = SupabaseStore("https://example.supabase.co", fake.key, transport=fake.transport())
        yield s
        await s.aclose()
    else:
        s = SupabaseStore.from_env()
        yield s
        await s.aclose()


@pytest.fixture
def uid():
    return f"contract-test-{uuid.uuid4()}"  # live runs share the real dev database: never collide


KID_A = StoredKid("1001", "Noa", "ג׳2", "s1", "School One")
KID_B = StoredKid("1002", "Ido", "ו׳1", "s2", "School Two", context_ref="ctx-2")


async def test_parent_identity_is_stable_channel_scoped_and_findable(store, uid):
    a = await store.get_or_create_parent("telegram", uid)
    try:
        assert a == await store.get_or_create_parent("telegram", uid)
        assert a == await store.find_parent("telegram", uid)
        other = await store.get_or_create_parent("whatsapp", uid)
        assert other.id != a.id
        await store.delete_parent(other.id)
        assert await store.find_parent("telegram", f"{uid}-nope") is None
    finally:
        await store.delete_parent(a.id)


async def test_kids_are_replaced_as_a_set_and_keep_their_fields(store, uid):
    p = await store.get_or_create_parent("telegram", uid)
    try:
        await store.save_kids(p.id, [KID_A, KID_B])
        assert sorted(await store.list_kids(p.id), key=lambda k: k.webtop_kid_id) == [KID_A, KID_B]
        renamed = StoredKid("1002", "Ido R", "ו׳1", "s2", "School Two", context_ref=None)
        await store.save_kids(p.id, [renamed])  # KID_A is gone, KID_B updated
        assert await store.list_kids(p.id) == [renamed]
        await store.save_kids(p.id, [])
        assert await store.list_kids(p.id) == []
    finally:
        await store.delete_parent(p.id)


async def test_session_roundtrip_overwrite_and_clear_keep_exact_bytes(store, uid):
    p = await store.get_or_create_parent("telegram", uid)
    try:
        assert await store.load_session(p.id) is None
        blob = bytes(range(256))  # every byte value, including 0x00 and 0xff
        await store.save_session(p.id, blob)
        assert await store.load_session(p.id) == blob
        await store.save_session(p.id, b"second")
        assert await store.load_session(p.id) == b"second"
        await store.clear_session(p.id)
        assert await store.load_session(p.id) is None
    finally:
        await store.delete_parent(p.id)


async def test_digest_defaults_off_roundtrips_and_refuses_on_without_a_time(store, uid):
    p = await store.get_or_create_parent("telegram", uid)
    try:
        assert await store.get_digest(p.id) == DigestSetting(False, None)
        await store.set_digest(p.id, DigestSetting(True, time(19, 0)))
        assert await store.get_digest(p.id) == DigestSetting(True, time(19, 0))
        await store.set_digest(p.id, DigestSetting(False, None))
        assert await store.get_digest(p.id) == DigestSetting(False, None)
        with pytest.raises(ValueError, match="time"):
            await store.set_digest(p.id, DigestSetting(True, None))
    finally:
        await store.delete_parent(p.id)


async def test_the_digest_content_level_roundtrips_defaults_to_none_and_refuses_unknown_levels(store, uid):
    p = await store.get_or_create_parent("telegram", uid)
    try:
        await store.set_digest(p.id, DigestSetting(True, time(19, 0)))
        assert (await store.get_digest(p.id)).content == "none"  # the fixed, content-free nudge
        await store.set_digest(p.id, DigestSetting(True, time(17, 3), "due"))
        assert await store.get_digest(p.id) == DigestSetting(True, time(17, 3), "due")
        await store.set_digest(p.id, DigestSetting(True, time(17, 3), "all"))
        assert (await store.get_digest(p.id)).content == "all"
        await store.set_digest(p.id, DigestSetting(False, None))
        assert await store.get_digest(p.id) == DigestSetting(False, None, "none")  # off forgets the level
        with pytest.raises(ValueError, match="content"):
            DigestSetting(True, time(19, 0), "everything")
    finally:
        await store.delete_parent(p.id)


async def test_deleting_a_parent_removes_everything_about_them(store, uid):
    p = await store.get_or_create_parent("telegram", uid)
    await store.save_kids(p.id, [KID_A])
    await store.save_session(p.id, b"x")
    await store.set_digest(p.id, DigestSetting(True, time(7, 30)))
    await store.delete_parent(p.id)
    assert await store.find_parent("telegram", uid) is None
    assert await store.list_kids(p.id) == []
    assert await store.load_session(p.id) is None
    assert await store.get_digest(p.id) == DigestSetting(False, None)


async def test_two_parents_never_see_each_others_data(store, uid):
    a = await store.get_or_create_parent("telegram", f"{uid}-a")
    b = await store.get_or_create_parent("telegram", f"{uid}-b")
    try:
        await store.save_kids(a.id, [KID_A])
        await store.save_session(a.id, b"a-secret")
        assert await store.list_kids(b.id) == [] and await store.load_session(b.id) is None
    finally:
        await store.delete_parent(a.id)
        await store.delete_parent(b.id)


async def test_due_digests_lists_only_enabled_parents_at_that_minute(store, uid):
    a = await store.get_or_create_parent("telegram", f"{uid}-a")
    b = await store.get_or_create_parent("telegram", f"{uid}-b")
    c = await store.get_or_create_parent("telegram", f"{uid}-c")
    try:
        await store.set_digest(a.id, DigestSetting(True, time(19, 0)))
        await store.set_digest(b.id, DigestSetting(True, time(20, 0)))
        await store.set_digest(c.id, DigestSetting(False, None))
        due = await store.due_digests(time(19, 0, 30))  # seconds are ignored
        mine = [p for p in due if p.channel_user_id.startswith(uid)]
        assert mine == [a]
        assert [
            p for p in await store.due_digests(time(19, 1)) if p.channel_user_id.startswith(uid)
        ] == []
        await store.set_digest(a.id, DigestSetting(False, None))  # turned off: not due any more
        assert [
            p for p in await store.due_digests(time(19, 0)) if p.channel_user_id.startswith(uid)
        ] == []
    finally:
        for p in (a, b, c):
            await store.delete_parent(p.id)


# ---- invites and the stored allowlist ----


@pytest.fixture
def code_hash(uid):
    return f"hash-{uid}"  # the live database is shared: never collide


async def test_an_invite_can_be_redeemed_exactly_once(store, code_hash, uid):
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    await store.create_invite(code_hash, "telegram:1", now + timedelta(hours=24))
    try:
        assert await store.redeem_invite(code_hash, f"telegram:{uid}", now) is True
        assert await store.redeem_invite(code_hash, "telegram:2", now) is False  # used up
    finally:
        await store.delete_invite(code_hash)


async def test_an_expired_or_unknown_invite_is_refused(store, code_hash):
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    await store.create_invite(code_hash, "telegram:1", now + timedelta(hours=1))
    try:
        assert await store.redeem_invite(code_hash, "telegram:2", now + timedelta(hours=2)) is False
        assert await store.redeem_invite(f"{code_hash}-nope", "telegram:2", now) is False
        # an expired invite stays unused: a later look at it inside its time would still work
        assert await store.redeem_invite(code_hash, "telegram:2", now) is True
    finally:
        await store.delete_invite(code_hash)


async def test_two_people_opening_the_same_invite_at_once_let_in_only_one(store, code_hash):
    import asyncio
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    await store.create_invite(code_hash, "telegram:1", now + timedelta(hours=24))
    try:
        results = await asyncio.gather(
            store.redeem_invite(code_hash, "telegram:10", now),
            store.redeem_invite(code_hash, "telegram:11", now),
        )
        assert sorted(results) == [False, True]
    finally:
        await store.delete_invite(code_hash)


async def test_allowed_users_are_stored_once_listed_and_removable(store, uid):
    await store.add_allowed("telegram", uid, "invite")
    try:
        await store.add_allowed("telegram", uid, "invite")  # again: no error, no duplicate
        mine = [u for u in await store.list_allowed() if u == ("telegram", uid)]
        assert mine == [("telegram", uid)]
    finally:
        await store.remove_allowed("telegram", uid)
    assert ("telegram", uid) not in await store.list_allowed()


async def test_list_sessions_names_who_has_a_stored_session_and_since_when(store, uid):
    from datetime import UTC, datetime, timedelta

    a = await store.get_or_create_parent("telegram", f"{uid}-a")
    b = await store.get_or_create_parent("telegram", f"{uid}-b")

    def mine(found):  # live runs share the dev database: look at our own rows only
        return sorted(s.channel_user_id for s in found if s.channel_user_id.startswith(uid))

    try:
        assert mine(await store.list_sessions()) == []
        before = datetime.now(UTC) - timedelta(seconds=5)
        await store.save_session(a.id, b"cipher-a")
        found = [s for s in await store.list_sessions() if s.channel_user_id.startswith(uid)]
        assert [(s.channel, s.channel_user_id) for s in found] == [("telegram", f"{uid}-a")]
        assert found[0].saved_at is not None and before <= found[0].saved_at <= datetime.now(UTC)
        await store.save_session(b.id, b"cipher-b")
        assert mine(await store.list_sessions()) == [f"{uid}-a", f"{uid}-b"]
        await store.clear_session(a.id)
        assert mine(await store.list_sessions()) == [f"{uid}-b"]
    finally:
        await store.delete_parent(a.id)
        await store.delete_parent(b.id)
