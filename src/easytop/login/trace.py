"""DEV-ONLY: summarise the login requests the streamed browser makes, without any secret.

Used to find out why a stream login gets a different session lifetime than a normal browser login: the shape
of the request (flags, device data, screen size) is compared, never the content. Codes, passwords, tokens,
captcha answers and ids are never recorded: strings appear only as lengths.
"""

from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime

_LOGIN_ENDPOINT = re.compile(r"/server/api/(user/\w*(Login|OneTimePassword)\w*)", re.IGNORECASE)
_SHOWN_HEADERS = ("rememberme", "language")
_SHOWN_INTS = ("RecoveryType", "LoginCounter")
_SHOWN_DEVICE = (
    "isMobile",
    "isTablet",
    "isDesktop",
    "getDeviceType",
    "os",
    "osVersion",
    "browser",
    "browserMajorVersion",
    "screen_resolution",
    "cookies",
)


def login_endpoint(path: str) -> str | None:
    m = _LOGIN_ENDPOINT.search(path)
    return m.group(1) if m else None


def summarize_request(path: str, headers: dict[str, str], body_text: str | None) -> dict | None:
    endpoint = login_endpoint(path)
    if endpoint is None:
        return None
    try:
        body = json.loads(body_text) if body_text else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    flags = {k: v for k, v in body.items() if isinstance(v, bool)}
    flags.update({k: body[k] for k in _SHOWN_INTS if isinstance(body.get(k), int)})
    lengths = {k: len(v) for k, v in body.items() if isinstance(v, str) and k != "deviceDataJson"}
    device: dict = {}
    raw = body.get("deviceDataJson")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            device = {k: parsed[k] for k in _SHOWN_DEVICE if k in parsed}
    shown = {
        k.lower(): v for k, v in headers.items() if k.lower() in _SHOWN_HEADERS and len(v) <= 20
    }
    return {
        "endpoint": endpoint,
        "headers": shown,
        "body": flags,
        "body_string_lengths": lengths,
        "device": device,
    }


def webtoken_lifetime_from_set_cookie(values: list[str], *, now: float) -> dict:
    """How long the server told the browser to keep the `webToken` cookie, from Set-Cookie headers."""
    for value in values:
        if not value.startswith("webToken="):
            continue
        age = re.search(r"max-age=(\d+)", value, re.IGNORECASE)
        if age:
            return {"webToken_hours": round(int(age.group(1)) / 3600, 2)}
        expires = re.search(r"expires=([^;]+)", value, re.IGNORECASE)
        if expires:
            try:
                return {
                    "webToken_hours": round(
                        (parsedate_to_datetime(expires.group(1).strip()).timestamp() - now) / 3600,
                        2,
                    )
                }
            except (TypeError, ValueError):
                pass
        return {"webToken_hours": None}
    return {"webToken_hours": None}
