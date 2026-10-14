"""Check Telegram Mini App `initData`: proof of WHICH Telegram user opened the page.

The connect link is delivered by the bot to one parent, but a link can be forwarded. initData is signed by
Telegram with a key derived from the bot token, so the login page can require that the viewer is the very
user the link was made for (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def verify_init_data(
    init_data: object, bot_token: str, *, now: float | None = None, max_age: int = 3600
) -> int | None:
    """The Telegram user id when `init_data` is genuine and fresh, else None. Never raises."""
    if not isinstance(init_data, str) or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        now = time.time() if now is None else now
        if now - int(pairs["auth_date"]) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None
