import json

from easytop.login import trace

NOW = 1_800_000_000.0


def test_a_login_request_is_summarised_without_any_secret():
    body = json.dumps(
        {
            "Code": 828132,
            "Guid": "11111111-2222-3333-4444-555555555555",
            "UniqueId": "SECRET-UNIQUE-ID",
            "rememberMe": False,
            "Password": "hunter2",
            "captcha": "03AFcWeA-SECRET-CAPTCHA",
            "deviceDataJson": json.dumps(
                {
                    "isMobile": False,
                    "isDesktop": True,
                    "getDeviceType": "Desktop",
                    "os": "Mac OS X",
                    "browser": "Chrome",
                    "screen_resolution": "393 x 720",
                    "userAgent": "Mozilla/5.0 (Macintosh)",
                    "someIdentifier": "SECRET-DEVICE-ID",
                }
            ),
        }
    )
    summary = trace.summarize_request(
        "/server/api/user/LoginByOneTimePassword",
        {
            "rememberme": "0",
            "language": "he",
            "x-xsrf-token": "SECRET-XSRF",
            "cookie": "webToken=SECRET",
        },
        body,
    )
    dumped = json.dumps(summary, ensure_ascii=False)
    for secret in ("SECRET", "hunter2", "828132", "11111111"):
        assert secret not in dumped
    assert summary["endpoint"] == "user/LoginByOneTimePassword"
    assert (
        summary["body"]["rememberMe"] is False
    )  # booleans and small numbers of known flags are shown
    assert summary["headers"] == {"rememberme": "0", "language": "he"}
    assert (
        summary["device"]["screen_resolution"] == "393 x 720"
        and summary["device"]["isDesktop"] is True
    )
    assert (
        summary["body_string_lengths"]["Password"] == 7
    )  # only the length of a secret, never the text


def test_other_endpoints_and_broken_bodies_are_ignored_or_safe():
    assert trace.summarize_request("/server/api/dashboard/InitDashboard", {}, "{}") is None
    assert (
        trace.summarize_request("/server/api/user/SendOneTimePassword", {}, "not json")["body"]
        == {}
    )
    assert trace.summarize_request("/server/api/user/SendOneTimePassword", {}, None)["body"] == {}


def test_the_lifetime_comes_from_the_set_cookie_header_without_the_value():
    year = "webToken=SECRETVALUE; expires=Thu, 09 Sep 2027 00:00:00 GMT; path=/; secure"
    other = "uniqueId=abc; Max-Age=3600; path=/"
    out = trace.webtoken_lifetime_from_set_cookie([other, year], now=1_790_000_000.0)
    assert out["webToken_hours"] is not None and out["webToken_hours"] > 8000
    assert "SECRET" not in json.dumps(out)
    short = trace.webtoken_lifetime_from_set_cookie(["webToken=x; Max-Age=32400; path=/"], now=NOW)
    assert short["webToken_hours"] == 9.0
    assert trace.webtoken_lifetime_from_set_cookie(["a=b"], now=NOW) == {"webToken_hours": None}
