import hashlib
import hmac
import json
from urllib.parse import urlencode

from easytop.login.telegram_initdata import verify_init_data

TOKEN = "123456:TEST-BOT-TOKEN"
NOW = 1_800_000_000


def sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def fields(user_id=42, age=60):
    return {"auth_date": str(NOW - age), "user": json.dumps({"id": user_id, "first_name": "x"})}


def test_valid_init_data_gives_the_telegram_user_id():
    assert verify_init_data(sign(fields()), TOKEN, now=NOW) == 42


def test_wrong_bot_token_tampering_and_missing_hash_are_refused():
    good = sign(fields())
    assert verify_init_data(good, "999:OTHER", now=NOW) is None
    assert verify_init_data(good.replace("42", "43"), TOKEN, now=NOW) is None
    assert verify_init_data(good.replace("hash=", "hash=0"), TOKEN, now=NOW) is None
    assert verify_init_data(urlencode(fields()), TOKEN, now=NOW) is None


def test_old_init_data_is_refused():
    assert verify_init_data(sign(fields(age=7200)), TOKEN, now=NOW, max_age=3600) is None
    assert verify_init_data(sign(fields(age=3000)), TOKEN, now=NOW, max_age=3600) == 42


def test_garbage_is_refused_without_raising():
    for bad in ("", "x", "hash=abc", "a=b&hash=zz", None, 5):
        assert verify_init_data(bad, TOKEN, now=NOW) is None
