from datetime import time
from pathlib import Path

import pglast

from easytop.store.base import DigestSetting, StoredKid
from easytop.store.memory import InMemoryStore

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


async def test_parent_identity_is_stable_and_channel_scoped():
    s = InMemoryStore()
    a = await s.get_or_create_parent("telegram", "1")
    assert a == await s.get_or_create_parent("telegram", "1")
    assert a.id != (await s.get_or_create_parent("whatsapp", "1")).id
    assert await s.find_parent("telegram", "nope") is None


async def test_kids_session_digest_roundtrip_and_delete():
    s = InMemoryStore()
    p = await s.get_or_create_parent("telegram", "1")
    kid = StoredKid("k1", "Noa", "ג׳2", "s1", "Oranim (synthetic)")
    await s.save_kids(p.id, [kid])
    await s.save_session(p.id, b"ciphertext")
    await s.set_digest(p.id, DigestSetting(True, time(19, 0)))
    assert await s.list_kids(p.id) == [kid]
    assert await s.load_session(p.id) == b"ciphertext"
    assert (await s.get_digest(p.id)).local_time == time(19, 0)
    await s.clear_session(p.id)
    assert await s.load_session(p.id) is None
    await s.delete_parent(p.id)
    assert await s.list_kids(p.id) == [] and await s.find_parent("telegram", "1") is None
    assert (await s.get_digest(p.id)) == DigestSetting(False, None)


def test_default_digest_is_off():
    import asyncio

    assert asyncio.run(InMemoryStore().get_digest("x")).enabled is False


def test_migrations_parse_as_postgres_and_enable_rls_everywhere():
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files
    sql = files[0].read_text()
    stmts = pglast.parse_sql(sql)
    assert len(stmts) > 5
    created = {s.stmt.relation.relname for s in stmts if type(s.stmt).__name__ == "CreateStmt"}
    rls = {
        s.stmt.relation.relname
        for s in stmts
        if type(s.stmt).__name__ == "AlterTableStmt"
        and any(c.subtype.name == "AT_EnableRowSecurity" for c in s.stmt.cmds)
    }
    assert (
        created
        == rls
        == {"parents", "parent_channels", "kids", "webtop_sessions", "digest_settings"}
    )
    # no Webtop content columns
    for forbidden in ("message", "homework", "grade", "password", "captcha"):
        assert forbidden not in sql.lower().replace("never the password, never captcha", "")


def test_anon_and_authenticated_have_no_privileges_on_any_easytop_table():
    """RLS has no policies, but TRUNCATE ignores RLS and Supabase grants everything to anon/authenticated by
    default. The bot only uses the server-side service role, so a migration must revoke it all."""
    revoked: dict[str, set[str]] = {}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        for s in pglast.parse_sql(f.read_text()):
            st = s.stmt
            if type(st).__name__ == "GrantStmt" and not st.is_grant and st.privileges is None:
                for obj in st.objects or []:
                    for role in st.grantees:
                        revoked.setdefault(obj.relname, set()).add(role.rolename)
    tables = {"parents", "parent_channels", "kids", "webtop_sessions", "digest_settings"}
    assert {t: revoked.get(t) for t in tables} == {t: {"anon", "authenticated"} for t in tables}
