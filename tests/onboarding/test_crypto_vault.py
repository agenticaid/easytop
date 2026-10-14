import json

import pytest
from cryptography.fernet import Fernet

from easytop.errors import SessionDecryptError
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.vault import SessionVault
from easytop.store.memory import InMemoryStore

COOKIES = {"webToken": "SECRET-WEB-TOKEN-123", "deviceType": "web"}


def test_round_trip_and_the_ciphertext_hides_the_token():
    cipher = SessionCipher(new_key())
    blob = cipher.encrypt(COOKIES)
    assert isinstance(blob, bytes)
    assert b"SECRET-WEB-TOKEN-123" not in blob and b"webToken" not in blob
    assert cipher.decrypt(blob) == COOKIES


def test_two_encryptions_of_the_same_cookies_differ():
    cipher = SessionCipher(new_key())
    assert cipher.encrypt(COOKIES) != cipher.encrypt(COOKIES)


def test_wrong_key_and_tampering_raise_without_content():
    blob = SessionCipher(new_key()).encrypt(COOKIES)
    with pytest.raises(SessionDecryptError) as e:
        SessionCipher(new_key()).decrypt(blob)
    assert "SECRET" not in str(e.value)
    own = SessionCipher(new_key())
    good = own.encrypt(COOKIES)
    with pytest.raises(SessionDecryptError):
        own.decrypt(good[:-4] + b"AAAA")
    with pytest.raises(SessionDecryptError):
        own.decrypt(b"not a token")


def test_decrypted_payload_of_the_wrong_shape_is_refused():
    key = new_key()
    for payload in (["a"], {"a": 1}, "text"):
        blob = Fernet(key).encrypt(json.dumps(payload).encode())
        with pytest.raises(SessionDecryptError):
            SessionCipher(key).decrypt(blob)


def test_a_bad_key_is_refused_by_name_not_value():
    with pytest.raises(ValueError, match="SESSION_ENCRYPTION_KEY") as e:
        SessionCipher("not-a-fernet-key")
    assert "not-a-fernet-key" not in str(e.value)


def test_from_env(monkeypatch):
    key = new_key()
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", key.decode())
    assert SessionCipher.from_env().decrypt(SessionCipher(key).encrypt(COOKIES)) == COOKIES
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY")
    with pytest.raises(ValueError, match="SESSION_ENCRYPTION_KEY"):
        SessionCipher.from_env()


async def test_the_vault_stores_only_ciphertext_and_forgets_on_clear():
    store = InMemoryStore()
    vault = SessionVault(store, SessionCipher(new_key()))
    parent = await store.get_or_create_parent("telegram", "1")
    assert await vault.load(parent.id) is None
    await vault.save(parent.id, COOKIES)
    raw = await store.load_session(parent.id)
    assert raw is not None and b"SECRET-WEB-TOKEN-123" not in raw
    assert await vault.load(parent.id) == COOKIES
    await vault.clear(parent.id)
    assert await vault.load(parent.id) is None
    assert await store.load_session(parent.id) is None


async def test_an_undecryptable_stored_session_counts_as_no_session_and_is_cleared():
    store = InMemoryStore()
    parent = await store.get_or_create_parent("telegram", "1")
    await SessionVault(store, SessionCipher(new_key())).save(parent.id, COOKIES)
    other_key_vault = SessionVault(store, SessionCipher(new_key()))
    assert await other_key_vault.load(parent.id) is None
    assert await store.load_session(parent.id) is None
