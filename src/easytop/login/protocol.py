"""Pure helpers for the login-streaming spike: validate what the phone sends, pick out Webtop cookies.

Everything the phone sends is untrusted input that ends up driving a real browser, so it is parsed
strictly here and never logged: typed text can be a password or a one-time code.
"""

from __future__ import annotations

import json
import math
import re
import statistics

MAX_TEXT = 200
MAX_SCROLL = 1500
MAX_DIAG_KEYS = 16
# The remote browser's screen is the same for every phone, so a page can be prepared before anyone connects; the
# phone scales the picture to fit. Taps are fractions of the picture, so no phone size matters to the server.
CANON_W = 392
CANON_H = 687
FRAME_BACKLOG_BYTES = (
    96_000  # about three pictures queued: any more is a delay the parent would feel
)
ROUTES = frozenset({"code", "password", "ministry"})
KEYS = frozenset(
    {"Backspace", "Enter", "Tab", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}
)
_DOMAIN = "smartschool.co.il"
_DIAG_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]{0,11}")
_TUNNEL = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return v


def parse_client_message(raw: str | bytes, *, width: int, height: int) -> dict | None:
    """One client message as a normalised dict, or None when it is malformed or not allowed."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("t")
    if kind == "tap":
        if (
            "fx" in msg or "fy" in msg
        ):  # a fraction of the picture, immune to any display-size mismatch
            fx, fy = _num(msg.get("fx")), _num(msg.get("fy"))
            if fx is None or fy is None:
                return None
            x, y = fx * width, fy * height
        else:
            x, y = _num(msg.get("x")), _num(msg.get("y"))
            if x is None or y is None:
                return None
        return {"t": "tap", "x": min(max(x, 0), width - 1), "y": min(max(y, 0), height - 1)}
    if kind == "scroll":
        fdy = _num(msg.get("fdy"))
        dy = fdy * height if fdy is not None else _num(msg.get("dy"))
        if dy is None:
            return None
        fx, fy = _num(msg.get("fx")), _num(msg.get("fy"))
        x = fx * width if fx is not None else _num(msg.get("x"))
        y = fy * height if fy is not None else _num(msg.get("y"))
        return {
            "t": "scroll",
            "dy": min(max(dy, -MAX_SCROLL), MAX_SCROLL),
            "x": width / 2 if x is None else min(max(x, 0), width - 1),
            "y": height / 2 if y is None else min(max(y, 0), height - 1),
        }
    if (
        kind == "diag"
    ):  # what the phone sees (sizes, zoom, scroll), logged to find calibration problems
        raw = msg.get("v")
        if not isinstance(raw, dict):
            return None
        kept = {
            k: v
            for k, v in raw.items()
            if isinstance(k, str) and _DIAG_KEY.fullmatch(k) and _num(v) is not None
        }
        return {"t": "diag", "v": dict(list(kept.items())[:MAX_DIAG_KEYS])}
    if (
        kind == "route"
    ):  # which Webtop sign-in screen to show: one of the three Webtop itself offers
        to = msg.get("to")
        return {"t": "route", "to": to} if to in ROUTES else None
    if kind == "text":
        s = msg.get("s")
        if not isinstance(s, str) or not s:
            return None
        return {"t": "text", "s": s[:MAX_TEXT]}
    if kind == "key":
        k = msg.get("k")
        return {"t": "key", "k": k} if k in KEYS else None
    if kind == "ping":
        c = _num(msg.get("c"))
        return None if c is None else {"t": "ping", "c": c}
    if kind == "lat":
        ms = _num(msg.get("ms"))
        return {"t": "lat", "ms": ms} if ms is not None and 0 <= ms <= 60_000 else None
    return None


def find_tunnel_url(line: str) -> str | None:
    m = _TUNNEL.search(line)
    return m.group(0) if m else None


def _is_webtop(cookie: dict) -> bool:
    return cookie.get("domain", "").lstrip(".").endswith(_DOMAIN)


def webtop_cookies(cookies: list[dict]) -> list[dict]:
    return [c for c in cookies if _is_webtop(c)]


def session_payload(cookies: list[dict], *, saved_at: str) -> dict:
    """The saved-session file format `easytop.connector.session_file.load_cookies` reads. Cookies of other
    sites (Google, ...) never go into it."""
    return {"saved_at": saved_at, "cookies": webtop_cookies(cookies)}


def webtoken_lifetime_hours(cookies: list[dict], *, now: float) -> float | None:
    """Client-side lifetime of the webToken cookie: about 9 h for a password login, about a year for a
    one-time-code login (seen 2026-10-01). None for a session cookie or no cookie."""
    for c in cookies:
        if c.get("name") == "webToken" and _is_webtop(c):
            expires = c.get("expires")
            if isinstance(expires, int | float) and expires > 0:
                return round((expires - now) / 3600, 2)
    return None


MINISTRY_COOKIES = {
    "rememberMeMoe",
    "loginMoe",
}  # set only by Webtop's Ministry sign-in (Spec 09 Q3)


def login_route(cookies: list[dict], *, tab: str) -> str:
    """Which sign-in made this session, for the analytics: the Ministry cookies prove the Ministry route; otherwise
    the tab the parent was on (code or password). Looks at cookie NAMES only."""
    if any(c.get("name") in MINISTRY_COOKIES for c in cookies):
        return "ministry"
    return tab if tab in ("code", "password") else "unknown"


def summarize_ms(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "p95": p95,
        "max": ordered[-1],
    }


def should_drop_frame(*, buffered_bytes: int) -> bool:
    """Skip a picture when the connection is already behind: a stale picture only adds delay."""
    return buffered_bytes > FRAME_BACKLOG_BYTES
