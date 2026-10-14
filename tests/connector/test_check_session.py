"""The cheap "is this session still alive" call the hourly health check makes: Webtop's own dashboard/CheckToken."""

import httpx
import pytest

from easytop.connector.webtop import WebtopConnector
from easytop.errors import SessionExpired


def connector(handler, **kw):
    return WebtopConnector({"webToken": "tok"}, transport=httpx.MockTransport(handler), **kw)


async def test_a_live_session_checks_true_with_one_whitelisted_read_call():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": True, "data": True})

    assert await connector(handler).check_session() is True
    assert seen == ["/server/api/dashboard/CheckToken"]


async def test_a_dead_session_checks_false_and_does_not_fire_the_expiry_hook():
    fired = []

    async def on_expired():
        fired.append(1)

    def handler(request):
        return httpx.Response(200, json={"status": True, "data": False})

    assert await connector(handler, on_expired=on_expired).check_session() is False
    assert fired == []  # the caller decides what a dead session means


async def test_a_refused_session_raises_session_expired():
    def handler(request):
        return httpx.Response(401)

    with pytest.raises(SessionExpired):
        await connector(handler).check_session()
