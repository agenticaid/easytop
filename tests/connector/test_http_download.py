"""WebtopHttpClient.download: the one GET the read-only client may make (Spec 14)."""

import httpx
import pytest

from easytop.connector.http import WebtopHttpClient
from easytop.errors import EndpointNotAllowed, FileUnavailable, SessionExpired, UpstreamError

URL = "https://webtopserver.smartschool.co.il/server/api/stream/GetFile?token=abc123"


def client(handler, **kw):
    return WebtopHttpClient(
        {"webToken": "t", "uniqueId": "u"}, transport=httpx.MockTransport(handler), **kw
    )


async def test_download_sends_the_session_cookies_and_returns_the_bytes():
    seen = {}

    def h(req: httpx.Request):
        seen.update(method=req.method, url=str(req.url), cookie=req.headers["cookie"])
        seen["origin"] = req.headers["origin"]
        return httpx.Response(
            200, content=b"%PDF-1.7 data", headers={"content-type": "application/octet-stream"}
        )

    assert await client(h).download(URL) == b"%PDF-1.7 data"
    assert seen["method"] == "GET" and seen["url"] == URL
    assert "webToken=t" in seen["cookie"] and seen["origin"] == "https://webtop.smartschool.co.il"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/server/api/stream/GetFile?token=abc",
        "http://webtopserver.smartschool.co.il/server/api/stream/GetFile?token=abc",
        "https://webtopserver.smartschool.co.il/server/api/messageBox/SendMessage?token=abc",
        URL + "&x=1",
        "",
    ],
)
async def test_any_other_url_is_refused_before_a_request(url):
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, content=b"x")

    with pytest.raises(EndpointNotAllowed):
        await client(h).download(url)
    assert calls == []


async def test_a_file_exactly_at_the_cap_is_returned():
    assert (
        await client(lambda r: httpx.Response(200, content=b"x" * 12)).download(URL, max_bytes=12)
        == b"x" * 12
    )


async def test_a_listed_length_over_the_cap_is_refused_without_reading():
    with pytest.raises(FileUnavailable) as e:
        await client(lambda r: httpx.Response(200, content=b"x" * 50)).download(URL, max_bytes=10)
    assert e.value.reason == "too_big"


async def test_reading_stops_once_the_cap_is_passed_even_without_a_length():
    read = []

    async def chunks():
        for i in range(10):
            read.append(i)
            yield b"x" * 6

    with pytest.raises(FileUnavailable) as e:
        await client(lambda r: httpx.Response(200, content=chunks())).download(URL, max_bytes=10)
    assert e.value.reason == "too_big" and len(read) <= 3


async def test_a_refused_session_raises_and_fires_the_expiry_hook():
    fired = []

    async def hook():
        fired.append(1)

    with pytest.raises(SessionExpired):
        await client(lambda r: httpx.Response(401), on_expired=hook).download(URL)
    assert fired == [1]


async def test_a_login_page_instead_of_a_file_means_the_session_expired():
    page = httpx.Response(200, text="<html>Login</html>", headers={"content-type": "text/html"})
    with pytest.raises(SessionExpired):
        await client(lambda r: page).download(URL)


async def test_a_server_error_is_an_upstream_error_after_one_retry():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(UpstreamError):
        await client(h).download(URL)
    assert len(calls) == 2
