from datetime import datetime, timedelta

import pytest

from easytop.adapters.model import Button, KidSection, Reply, reply_from_brief
from easytop.adapters.security import Allowlist, RateLimiter


def test_reply_limits_buttons_to_three():
    Reply(buttons=[Button(str(i), "x") for i in range(3)])
    with pytest.raises(ValueError):
        Reply(buttons=[Button(str(i), "x") for i in range(4)])


def test_button_id_length_limit():
    with pytest.raises(ValueError):
        Button("x" * 65, "label")


def test_reply_from_brief_per_kid_sections():
    brief = {
        "weekday": "Tuesday",
        "date": "2026-10-06",
        "checked_at": "2026-10-05T18:42",
        "kids": [
            {
                "name": "Noa",
                "class": "ג׳2",
                "ok": True,
                "homework_due": [{"subject": "Math", "description": "p.42", "due": "2026-10-06"}],
            },
            {"name": "Ido", "class": "ו׳1", "ok": False, "error": "unavailable"},
        ],
    }
    r = reply_from_brief(brief)
    assert [s.name for s in r.sections] == ["Noa", "Ido"]
    assert r.sections[0].lines == ["Math: p.42 (due)"]
    assert "didn't respond" in r.sections[1].lines[0]
    assert r.footer == "Source: Webtop, checked 18:42"
    assert isinstance(r.sections[0], KidSection)


def test_allowlist_fails_closed():
    assert not Allowlist().is_allowed("telegram:1")
    a = Allowlist({"telegram:1"})
    assert a.is_allowed("telegram:1") and not a.is_allowed("telegram:2")


def test_allowlist_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "11, 22,,")
    a = Allowlist.from_env()
    assert (
        a.is_allowed("telegram:11")
        and a.is_allowed("telegram:22")
        and not a.is_allowed("telegram:33")
    )
    monkeypatch.delenv("TELEGRAM_ALLOWED_IDS")
    assert not Allowlist.from_env().is_allowed("telegram:11")


def test_rate_limiter_window_and_per_parent():
    t = {"now": datetime(2026, 10, 5, 12, 0)}
    rl = RateLimiter(limit=3, window_seconds=3600, clock=lambda: t["now"])
    assert [rl.check("a") for _ in range(3)] == [0, 0, 0]
    wait = rl.check("a")
    assert 3590 <= wait <= 3600
    assert rl.check("b") == 0  # other parent unaffected
    t["now"] += timedelta(minutes=30)
    assert 1790 <= rl.check("a") <= 1800
    t["now"] += timedelta(minutes=31)
    assert rl.check("a") == 0  # window slid


def test_blocked_hit_not_recorded():
    t = {"now": datetime(2026, 10, 5, 12, 0)}
    rl = RateLimiter(limit=1, window_seconds=60, clock=lambda: t["now"])
    rl.check("a")
    for _ in range(5):
        assert rl.check("a") > 0
    t["now"] += timedelta(seconds=61)
    assert rl.check("a") == 0


def test_a_link_button_must_be_https_and_short():
    from easytop.adapters.model import LinkButton

    LinkButton("Connect", "https://x.example/?t=abc")
    for bad in (
        "http://x.example/",
        "javascript:alert(1)",
        "https://",
        "",
        "https://x.example/" + "a" * 2100,
    ):
        with pytest.raises(ValueError):
            LinkButton("Connect", bad)
    with pytest.raises(ValueError):
        LinkButton("", "https://x.example/")
