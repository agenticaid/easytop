import httpx

from easytop.login.runtime import LoginRuntime
from easytop.onboarding.links import ConnectLinks


class FakeTunnel:
    def __init__(self):
        self.started_on = None
        self.stopped = False
        self.url = "https://fake-words-here.trycloudflare.com"

    async def start(self, port):
        self.started_on = port
        return self.url

    async def stop(self):
        self.stopped = True


async def test_runtime_serves_the_login_page_and_exposes_the_tunnel_address():
    links = ConnectLinks()

    async def on_connected(parent_id, cookies): ...

    tunnel = FakeTunnel()
    rt = LoginRuntime(links, on_connected, tunnel=tunnel, port=0)
    assert rt.base_url is None  # nothing before start
    await rt.start()
    try:
        assert rt.base_url == tunnel.url and tunnel.started_on == rt.port and rt.port > 0
        token = links.issue("telegram:1")
        async with httpx.AsyncClient() as c:
            assert (await c.get(f"http://127.0.0.1:{rt.port}/?t={token}")).status_code == 200
            assert (await c.get(f"http://127.0.0.1:{rt.port}/?t=x")).status_code == 403
    finally:
        await rt.stop()
    assert tunnel.stopped and rt.base_url is None


async def test_a_fixed_public_address_needs_no_tunnel():
    async def on_connected(parent_id, cookies): ...

    rt = LoginRuntime(
        ConnectLinks(), on_connected, tunnel=None, port=0, public_url="https://login.example"
    )
    await rt.start()
    try:
        assert rt.base_url == "https://login.example"
    finally:
        await rt.stop()
