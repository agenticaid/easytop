import json

import httpx
import pytest

from easytop.connector.http import WebtopHttpClient
from easytop.errors import EndpointNotAllowed, SessionExpired, UpstreamError

ALLOWED = frozenset({"a/Read"})


def client(handler, **kw):
    return WebtopHttpClient(
        {"s": "v"}, allowed=ALLOWED, transport=httpx.MockTransport(handler), **kw
    )


async def test_whitelisted_call_sends_headers_and_cookie():
    seen = {}

    def h(req: httpx.Request):
        seen["origin"] = req.headers["origin"]
        seen["cookie"] = req.headers["cookie"]
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": 1})

    assert await client(h).call("a/Read", {"x": 1}) == {"ok": 1}
    assert seen["origin"] == "https://webtop.smartschool.co.il"
    assert seen["cookie"] == "s=v"
    assert seen["url"].endswith("/server/api/a/Read")


async def test_non_whitelisted_endpoint_raises_before_any_request():
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, json={})

    with pytest.raises(EndpointNotAllowed):
        await client(h).call("messageBox/SendMessage", {})
    assert calls == []


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(401),
        httpx.Response(302, headers={"location": "/login"}),
        httpx.Response(200, text="<html>Login</html>", headers={"content-type": "text/html"}),
    ],
)
async def test_expiry_detected_and_hook_called(resp):
    fired = []

    async def hook():
        fired.append(1)

    with pytest.raises(SessionExpired):
        await client(lambda r: resp, on_expired=hook).call("a/Read")
    assert fired == [1]


async def test_one_retry_on_timeout_then_success():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"ok": True})

    assert await client(h).call("a/Read") == {"ok": True}
    assert n["c"] == 2


async def test_gives_up_after_one_retry():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(UpstreamError):
        await client(h).call("a/Read")
    assert n["c"] == 2


async def test_5xx_retried_then_upstream_error():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(503)

    with pytest.raises(UpstreamError):
        await client(h).call("a/Read")
    assert n["c"] == 2


async def test_timeout_is_ten_seconds():
    c = client(lambda r: httpx.Response(200, json={}))
    assert c._client.timeout.read == 10.0


async def test_browser_like_headers_sent():
    seen = {}

    def h(req):
        seen.update(req.headers)
        return httpx.Response(200, json={})

    await client(h).call("a/Read")
    assert seen["x-xsrf-token"] == "" and seen["language"] == "he" and seen["rememberme"] == "0"


def test_default_whitelist_is_the_verified_parent_read_endpoints_only():
    from easytop.connector.http import READ_ENDPOINTS

    assert {
        "user/GetMultipleUsersForUser",
        "dashboard/InitDashboard",
        "WeeklyScedule/GetEvents",
        "PupilCard/GetSettingsList",
        "PupilCard/GetPupilGrades",
        "PupilCard/GetPupilDiciplineEvents",
        "PupilCard/GetPupilLessonsAndHomework",
        "PupilCard/GetPreviousHomeworks",
        "WeeklyScedule/InitData",
        "messageBox/GetMessagesInbox",
        "messageBox/GetMessagesInboxData",
    } <= READ_ENDPOINTS
    # teacher-role names from the legacy scripts, never seen in the parent traffic
    assert (
        not {
            "grades/getCurrentGrades",
            "messageBox/GetMessagesOutbox",
            "notification/GetNotifications",
        }
        & READ_ENDPOINTS
    )
    assert (
        "user/ChangeUser" not in READ_ENDPOINTS
    )  # a context switch, done only by switch_context()


async def test_change_user_cannot_go_through_the_generic_call():
    c = WebtopHttpClient(
        {"webToken": "t"}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(EndpointNotAllowed):
        await c.call("user/ChangeUser", {"StudentId": "x"})


async def test_switch_context_posts_the_recorded_body_and_returns_the_new_token():
    seen = {}

    def h(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["json"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"status": True, "data": {"token": "new", "userType": 2, "institutionCode": 5}},
        )

    c = WebtopHttpClient({"webToken": "old"}, transport=httpx.MockTransport(h))
    user = await c.switch_context("ctx-1")
    assert seen["url"].endswith("/server/api/user/ChangeUser")
    assert seen["json"] == {"StudentId": "ctx-1", "institutionCode": None, "userType": None}
    assert user.token == "new" and user.is_parent and user.institution_code == 5


async def test_with_token_replaces_only_the_web_token_cookie():
    cookies = []

    def h(req):
        cookies.append(req.headers["cookie"])
        return httpx.Response(200, json={})

    base = WebtopHttpClient(
        {"uniqueId": "u", "webToken": "old"}, allowed=ALLOWED, transport=httpx.MockTransport(h)
    )
    await base.with_token("new").call("a/Read")
    await base.call("a/Read")
    assert cookies == ["uniqueId=u; webToken=new", "uniqueId=u; webToken=old"]
