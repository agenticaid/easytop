import httpx
import pytest

from easytop.store.base import StoredKid, StoreError
from easytop.store.supabase import SupabaseStore
from tests.store.fake_postgrest import FakePostgrest

SECRET = "SERVICE-KEY-SECRET-VALUE"
URL = "https://proj.supabase.co"


def make(fake=None, **kw):
    fake = fake or FakePostgrest(SECRET)
    return fake, SupabaseStore(URL, SECRET, transport=fake.transport(), **kw)


async def test_every_request_goes_to_the_data_api_with_the_key_in_the_apikey_header_only():
    """Supabase: secret keys (sb_secret_...) are not JWTs, so they go on `apikey` and never on
    `Authorization: Bearer` (PostgREST would try to verify them as a JWT and reject the request)."""
    fake, s = make()
    await s.get_or_create_parent("telegram", "1")
    assert fake.requests and all(
        str(r.url).startswith(f"{URL}/rest/v1/")
        and r.headers["apikey"] == SECRET
        and "authorization" not in r.headers
        for r in fake.requests
    )


async def test_a_wrong_key_is_a_store_error_that_never_contains_a_key():
    fake = FakePostgrest(SECRET)
    s = SupabaseStore(URL, "wrong-key-value", transport=fake.transport())
    with pytest.raises(StoreError) as e:
        await s.find_parent("telegram", "1")
    assert e.value.status == 401
    assert "wrong-key-value" not in str(e.value) and SECRET not in str(e.value)


async def test_an_unreachable_database_is_a_store_error_without_the_url():
    def boom(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    s = SupabaseStore(URL, SECRET, transport=httpx.MockTransport(boom))
    with pytest.raises(StoreError) as e:
        await s.find_parent("telegram", "1")
    assert "proj.supabase.co" not in str(e.value) and "ConnectError" in str(e.value)


def _clear_supabase_env(monkeypatch):
    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_URL_DEV",
        "SUPABASE_SERVICE_KEY_DEV",
        "SUPABASE_URL_PROD",
        "SUPABASE_SERVICE_KEY_PROD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_from_env_names_the_missing_settings(monkeypatch):
    _clear_supabase_env(monkeypatch)
    with pytest.raises(StoreError, match="SUPABASE_URL.*SUPABASE_SERVICE_KEY"):
        SupabaseStore.from_env()


def test_from_env_uses_the_dev_names_of_the_local_env_file(monkeypatch):
    _clear_supabase_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL_DEV", "https://dev.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY_DEV", "dev-key")
    assert SupabaseStore.from_env()._base == "https://dev.supabase.co/rest/v1"


def test_the_plain_names_set_by_cloud_run_win_over_the_dev_names(monkeypatch):
    _clear_supabase_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://cloud.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "cloud-key")
    monkeypatch.setenv("SUPABASE_URL_DEV", "https://dev.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY_DEV", "dev-key")
    assert SupabaseStore.from_env()._base == "https://cloud.supabase.co/rest/v1"


def test_a_local_run_never_reads_the_prod_names(monkeypatch):
    _clear_supabase_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL_PROD", "https://prod.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY_PROD", "prod-key")
    with pytest.raises(StoreError):
        SupabaseStore.from_env()


async def test_two_first_logins_at_once_end_with_one_parent_and_no_orphan():
    fake = FakePostgrest(SECRET)
    real = fake.handle
    raced = {"done": False}

    def handler(request):
        if (
            request.method == "POST"
            and request.url.path.endswith("/parent_channels")
            and not raced["done"]
        ):
            raced["done"] = True  # another process creates the same identity just before our insert
            fake.tables["parents"].append({"id": "competitor-parent"})
            fake.tables["parent_channels"].append(
                {
                    "id": "c1",
                    "parent_id": "competitor-parent",
                    "channel": "telegram",
                    "channel_user_id": "race",
                }
            )
        return real(request)

    s = SupabaseStore(URL, SECRET, transport=httpx.MockTransport(handler))
    parent = await s.get_or_create_parent("telegram", "race")
    assert parent.id == "competitor-parent"
    assert [p["id"] for p in fake.tables["parents"]] == [
        "competitor-parent"
    ]  # our orphan was removed
    assert len(fake.tables["parent_channels"]) == 1


async def test_a_failing_write_never_wipes_the_kids_already_stored():
    fake, s = make()
    p = await s.get_or_create_parent("telegram", "1")
    await s.save_kids(p.id, [StoredKid("1", "Noa", "", "s", "S")])
    real = fake.handle

    def failing(request):
        if request.method == "POST" and request.url.path.endswith("/kids"):
            return httpx.Response(500, json={"code": "XX000", "message": "boom"})
        return real(request)

    s2 = SupabaseStore(URL, SECRET, transport=httpx.MockTransport(failing))
    with pytest.raises(StoreError):
        await s2.save_kids(p.id, [StoredKid("2", "Ido", "", "s", "S")])
    assert [k.webtop_kid_id for k in await s.list_kids(p.id)] == ["1"]  # the old set is intact


async def test_kid_ids_with_quotes_and_commas_are_filtered_safely():
    fake, s = make()
    p = await s.get_or_create_parent("telegram", "1")
    odd = StoredKid('a,"b"\\c', "Odd", "", "s", "S")
    keep = StoredKid("2", "Keep", "", "s", "S")
    await s.save_kids(p.id, [odd, keep])
    await s.save_kids(p.id, [keep])  # the odd one must be deleted, the other kept
    assert [k.webtop_kid_id for k in await s.list_kids(p.id)] == ["2"]
