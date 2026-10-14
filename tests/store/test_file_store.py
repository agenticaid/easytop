import json
import stat
from datetime import time

from easytop.store.base import DigestSetting, StoredKid
from easytop.store.file import FileStore

KID = StoredKid("1001", "Noa", "ג׳2", "s1", "School One", context_ref="ctx")


async def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "data" / "store.json"
    first = FileStore(path)
    parent = await first.get_or_create_parent("telegram", "42")
    await first.save_kids(parent.id, [KID])
    await first.save_session(parent.id, b"\x00ciphertext\xff")
    await first.set_digest(parent.id, DigestSetting(True, time(19, 0)))

    second = FileStore(path)  # a new process reading the same file
    assert await second.find_parent("telegram", "42") == parent
    assert await second.list_kids(parent.id) == [KID]
    assert await second.load_session(parent.id) == b"\x00ciphertext\xff"
    assert await second.get_digest(parent.id) == DigestSetting(True, time(19, 0))


async def test_a_deleted_parent_stays_deleted_after_a_restart(tmp_path):
    path = tmp_path / "store.json"
    store = FileStore(path)
    parent = await store.get_or_create_parent("telegram", "42")
    await store.save_session(parent.id, b"x")
    await store.delete_parent(parent.id)
    again = FileStore(path)
    assert await again.find_parent("telegram", "42") is None
    assert await again.load_session(parent.id) is None


async def test_the_file_is_private_and_holds_the_session_only_as_given(tmp_path):
    path = tmp_path / "store.json"
    store = FileStore(path)
    parent = await store.get_or_create_parent("telegram", "42")
    await store.save_session(parent.id, b"ALREADY-ENCRYPTED-BYTES")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a live (encrypted) session: owner only
    data = json.loads(path.read_text())
    assert "ALREADY-ENCRYPTED-BYTES" not in path.read_text()  # stored as base64, never as raw text
    assert set(data) >= {"parents", "kids", "sessions", "digest"}


async def test_a_damaged_file_starts_empty_instead_of_crashing_the_bot(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{ not json")
    store = FileStore(path)
    assert await store.find_parent("telegram", "42") is None
    parent = await store.get_or_create_parent("telegram", "42")  # and it works again
    assert await FileStore(path).find_parent("telegram", "42") == parent


async def test_invites_and_allowed_users_survive_a_restart(tmp_path):
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "store.json"
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    first = FileStore(path)
    await first.create_invite("h1", "telegram:1", now + timedelta(hours=24))
    await first.create_invite("h2", "telegram:1", now + timedelta(hours=24))
    assert await first.redeem_invite("h2", "telegram:7", now) is True
    await first.add_allowed("telegram", "7", "invite")

    second = FileStore(path)  # a new process reading the same file
    assert await second.list_allowed() == [("telegram", "7")]
    assert await second.redeem_invite("h2", "telegram:8", now) is False  # still used up
    assert await second.redeem_invite("h1", "telegram:9", now) is True  # still open
