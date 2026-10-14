import json

from easytop.login import protocol as p

W, H = 390, 700


def parse(msg):
    return p.parse_client_message(json.dumps(msg), width=W, height=H)


def test_tap_is_clamped_into_the_viewport():
    assert parse({"t": "tap", "x": 10.5, "y": 20}) == {"t": "tap", "x": 10.5, "y": 20}
    assert parse({"t": "tap", "x": -5, "y": 9999}) == {"t": "tap", "x": 0, "y": H - 1}
    assert parse({"t": "tap", "x": 9999, "y": 5}) == {"t": "tap", "x": W - 1, "y": 5}


def test_malformed_and_unknown_messages_are_dropped():
    assert p.parse_client_message("not json", width=W, height=H) is None
    assert p.parse_client_message("[1,2]", width=W, height=H) is None
    assert parse({"t": "evaluate", "js": "1"}) is None
    assert parse({"t": "tap", "x": "a", "y": 1}) is None
    assert parse({"t": "tap", "x": float("nan"), "y": 1}) is None
    assert parse({"t": "tap", "x": True, "y": 1}) is None
    assert parse({}) is None


def test_text_is_capped_and_must_be_a_string():
    assert parse({"t": "text", "s": "abc"}) == {"t": "text", "s": "abc"}
    assert len(parse({"t": "text", "s": "x" * 5000})["s"]) == p.MAX_TEXT
    assert parse({"t": "text", "s": 5}) is None
    assert parse({"t": "text", "s": ""}) is None


def test_only_allowed_keys_pass():
    assert parse({"t": "key", "k": "Backspace"}) == {"t": "key", "k": "Backspace"}
    assert parse({"t": "key", "k": "F12"}) is None
    assert parse({"t": "key", "k": "Control+W"}) is None


def test_scroll_is_clamped_and_ping_lat_are_numbers():
    assert parse({"t": "scroll", "dy": 50})["dy"] == 50
    assert parse({"t": "scroll", "dy": 99999})["dy"] == p.MAX_SCROLL
    assert parse({"t": "scroll", "dy": -99999})["dy"] == -p.MAX_SCROLL
    assert parse({"t": "ping", "c": 12.5}) == {"t": "ping", "c": 12.5}
    assert parse({"t": "lat", "ms": 80}) == {"t": "lat", "ms": 80}
    assert parse({"t": "lat", "ms": -1}) is None


def test_tunnel_url_is_found_in_cloudflared_output():
    line = "2026-10-07T10:00:00Z INF |  https://random-words-here.trycloudflare.com  |"
    assert p.find_tunnel_url(line) == "https://random-words-here.trycloudflare.com"
    assert p.find_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None
    assert p.find_tunnel_url("https://api.trycloudflare.com/tunnel") is None


def test_session_payload_keeps_only_webtop_cookies():
    cookies = [
        {"name": "webToken", "value": "t", "domain": ".smartschool.co.il", "expires": -1},
        {"name": "g", "value": "x", "domain": ".google.com"},
        {"name": "a", "value": "y", "domain": "webtop.smartschool.co.il"},
    ]
    payload = p.session_payload(cookies, saved_at="2026-10-07T10:00:00+00:00")
    assert payload["saved_at"] == "2026-10-07T10:00:00+00:00"
    assert [c["name"] for c in payload["cookies"]] == ["webToken", "a"]


def test_webtoken_lifetime_tells_the_login_route():
    now = 1_000_000.0
    nine_hours = [{"name": "webToken", "domain": ".smartschool.co.il", "expires": now + 9 * 3600}]
    assert p.webtoken_lifetime_hours(nine_hours, now=now) == 9.0
    year = [{"name": "webToken", "domain": ".smartschool.co.il", "expires": now + 365 * 86400}]
    assert p.webtoken_lifetime_hours(year, now=now) == 365 * 24
    session = [{"name": "webToken", "domain": ".smartschool.co.il", "expires": -1}]
    assert p.webtoken_lifetime_hours(session, now=now) is None
    assert p.webtoken_lifetime_hours([], now=now) is None


def test_latency_summary():
    assert p.summarize_ms([]) == {"n": 0}
    s = p.summarize_ms([10, 20, 30, 40, 100])
    assert s["n"] == 5 and s["median"] == 30 and s["max"] == 100


def test_scroll_carries_a_clamped_position_defaulting_to_the_centre():
    m = parse({"t": "scroll", "dy": 10, "x": 5000, "y": -3})
    assert (m["x"], m["y"]) == (W - 1, 0)
    m = parse({"t": "scroll", "dy": 10})
    assert (m["x"], m["y"]) == (W / 2, H / 2)


def test_taps_can_be_sent_as_a_fraction_of_the_picture_so_a_size_mismatch_cannot_shift_them():
    # 390 x 700 remote view: a tap 50% across and 25% down is (195, 175) however big the phone draws it
    assert parse({"t": "tap", "fx": 0.5, "fy": 0.25}) == {"t": "tap", "x": 195, "y": 175}
    assert parse({"t": "tap", "fx": 2, "fy": -1}) == {"t": "tap", "x": W - 1, "y": 0}
    assert parse({"t": "tap", "fx": "a", "fy": 0.1}) is None
    assert parse({"t": "tap", "fx": 0.5}) is None


def test_scroll_can_be_a_fraction_of_the_picture_height():
    m = parse({"t": "scroll", "fdy": 0.1, "fx": 0.5, "fy": 0.5})
    assert m == {"t": "scroll", "dy": 70, "x": 195, "y": 350}


def test_the_phones_view_numbers_are_kept_short_and_numeric_for_the_log():
    m = parse({"t": "diag", "v": {"innerH": 700.4, "vvScale": 1, "bad": "text", "x" * 40: 1}})
    assert m == {"t": "diag", "v": {"innerH": 700.4, "vvScale": 1}}
    assert parse({"t": "diag", "v": "nope"}) is None
    assert len(parse({"t": "diag", "v": {f"k{i}": i for i in range(50)}})["v"]) == p.MAX_DIAG_KEYS


def test_only_the_three_known_sign_in_routes_can_be_chosen():
    for route in ("code", "password", "ministry"):
        assert parse({"t": "route", "to": route}) == {"t": "route", "to": route}
    assert parse({"t": "route", "to": "https://evil.example"}) is None
    assert parse({"t": "route", "to": 5}) is None
    assert parse({"t": "route"}) is None


def test_a_backed_up_connection_drops_stale_pictures():
    assert p.should_drop_frame(buffered_bytes=0) is False
    assert p.should_drop_frame(buffered_bytes=p.FRAME_BACKLOG_BYTES) is False
    assert p.should_drop_frame(buffered_bytes=p.FRAME_BACKLOG_BYTES + 1) is True


def test_the_phone_page_and_the_server_agree_on_the_fixed_remote_screen_size():
    import re
    from pathlib import Path

    html = (Path(p.__file__).parent / "static" / "index.html").read_text()
    w = int(re.search(r"const CW = (\d+)", html).group(1))
    h = int(re.search(r"CH = (\d+)", html).group(1))
    assert (w, h) == (p.CANON_W, p.CANON_H)


def test_login_route_comes_from_the_ministry_cookies_else_the_last_tab():
    from easytop.login.protocol import login_route

    ministry = [{"name": "webToken"}, {"name": "rememberMeMoe"}]
    assert login_route(ministry, tab="code") == "ministry"  # the cookies decide when they are there
    assert login_route([{"name": "webToken"}, {"name": "loginMoe"}], tab="password") == "ministry"
    assert login_route([{"name": "webToken"}], tab="code") == "code"
    assert login_route([{"name": "webToken"}], tab="password") == "password"
    assert (
        login_route([{"name": "webToken"}], tab="ministry") == "unknown"
    )  # Ministry tab without its cookies
