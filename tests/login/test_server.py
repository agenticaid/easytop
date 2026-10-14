"""The product login server on a local page (no Webtop): per-parent one-time links, the viewer check,
the hand-over to on_connected, and what is and is not logged. Needs Playwright Chromium."""

import asyncio
import http.server
import json
import logging
import threading
from urllib.parse import quote

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from easytop.login.server import LoginServer
from easytop.onboarding.links import ConnectLinks

PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i style="position:absolute;top:10px;left:10px;width:200px;height:40px;font-size:16px">
<button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="if(document.getElementById('i').value==='abc'){document.cookie='webToken=SECRET-COOKIE; path=/'}">go</button>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_page():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


class Rig:
    def __init__(self, page, **kw):
        fail = kw.pop("fail", False)
        self.links = ConnectLinks()
        self.connected: list[tuple[str, dict]] = []
        self.event = asyncio.Event()

        async def verify(cookies):
            return True

        async def on_connected(parent_id, cookies):
            if fail:
                raise RuntimeError("boom SECRET-COOKIE")
            self.connected.append((parent_id, cookies))
            self.event.set()

        self.server = LoginServer(
            self.links,
            on_connected,
            target_url=page,
            verify=verify,
            cookie_filter=lambda cookies: cookies,
            scale=1,
            **kw,
        )

    async def start(self):
        self.srv = await self.server.serve("127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def stop(self):
        self.srv.close()
        await self.srv.wait_closed()

    def ws(self, token, init=""):
        return f"ws://127.0.0.1:{self.port}/ws?t={token}&w=390&h=700&d={quote(init)}"


async def drive(ws, rig):
    """frame, focus the field, type abc, press the button."""
    for _ in range(40):
        if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
            break
    await ws.send(json.dumps({"t": "tap", "x": 50, "y": 30}))
    await asyncio.sleep(0.5)
    await ws.send(json.dumps({"t": "text", "s": "abc"}))
    await asyncio.sleep(0.3)
    await ws.send(json.dumps({"t": "tap", "x": 50, "y": 120}))
    await asyncio.wait_for(rig.event.wait(), 20)


async def test_http_gate_follows_the_parents_link_token(local_page):
    rig = Rig(local_page)
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        async with httpx.AsyncClient() as c:
            assert (await c.get(f"http://127.0.0.1:{rig.port}/?t={token}")).status_code == 200
            assert (await c.get(f"http://127.0.0.1:{rig.port}/?t=nope")).status_code == 403
            assert (await c.get(f"http://127.0.0.1:{rig.port}/")).status_code == 403
    finally:
        await rig.stop()


async def test_login_hands_the_parents_cookies_over_and_uses_the_link_up(local_page, caplog):
    rig = Rig(local_page)
    await rig.start()
    caplog.set_level(logging.INFO)
    try:
        token = rig.links.issue("telegram:7")
        try:
            async with connect(rig.ws(token), max_size=None) as ws:
                await drive(ws, rig)
                while True:
                    m = await asyncio.wait_for(ws.recv(), 10)
                    if isinstance(m, str) and json.loads(m).get("t") == "done":
                        break
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                pytest.skip("Playwright Chromium not installed")
            raise
        ((parent_id, cookies),) = rig.connected
        assert parent_id == "telegram:7" and cookies["webToken"] == "SECRET-COOKIE"
        assert rig.links.peek(token) is None  # single use
        with pytest.raises(InvalidStatus):  # the used link no longer opens a session
            async with connect(rig.ws(token)):
                pass
    finally:
        await rig.stop()
    assert "abc" not in caplog.text and "SECRET-COOKIE" not in caplog.text
    events = [r.analytics for r in caplog.records if hasattr(r, "analytics")]
    assert [e["event"] for e in events] == ["connect_opened", "login_captured"]
    captured = events[1]
    assert (
        captured["route"] == "unknown"
    )  # the local page sets no Ministry cookie and no tab was chosen
    assert captured["login_seconds"] > 0 and captured["taps"] == 2
    assert events[0]["parent_ref"] == captured["parent_ref"]


async def test_the_viewer_check_must_pass_to_open_a_session(local_page):
    seen = []

    def viewer_check(parent_id, init_data):
        seen.append((parent_id, init_data))
        return init_data == "genuine"

    rig = Rig(local_page, viewer_check=viewer_check)
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        with pytest.raises(InvalidStatus):
            async with connect(rig.ws(token, "forged")):
                pass
        with pytest.raises(InvalidStatus):
            async with connect(rig.ws(token)):
                pass
        assert rig.links.peek(token) == "telegram:7"  # a failed check does not burn the link
        assert seen[0] == ("telegram:7", "forged")
        try:
            async with connect(rig.ws(token, "genuine"), max_size=None) as ws:
                await drive(ws, rig)
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                pytest.skip("Playwright Chromium not installed")
            raise
        assert rig.connected[0][0] == "telegram:7"
    finally:
        await rig.stop()


async def test_leaving_early_keeps_the_link_so_the_parent_can_reopen_it(local_page, caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(local_page)
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        try:
            async with connect(rig.ws(token), max_size=None) as ws:
                for _ in range(40):
                    if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                        break
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                pytest.skip("Playwright Chromium not installed")
            raise
        await asyncio.sleep(1)
        assert rig.connected == [] and rig.links.peek(token) == "telegram:7"
    finally:
        await rig.stop()
    events = [r.analytics for r in caplog.records if hasattr(r, "analytics")]
    assert [e["event"] for e in events] == ["connect_opened", "connect_abandoned"]
    assert events[1]["reason"] == "left" and events[1]["login_seconds"] >= 0


async def test_a_failing_hand_over_tells_the_phone_without_details(local_page, caplog):
    rig = Rig(local_page, fail=True)
    await rig.start()
    caplog.set_level(logging.INFO)
    try:
        token = rig.links.issue("telegram:7")
        try:
            async with connect(rig.ws(token), max_size=None) as ws:
                for _ in range(40):
                    if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                        break
                await ws.send(json.dumps({"t": "tap", "x": 50, "y": 30}))
                await asyncio.sleep(0.5)
                await ws.send(json.dumps({"t": "text", "s": "abc"}))
                await asyncio.sleep(0.3)
                await ws.send(json.dumps({"t": "tap", "x": 50, "y": 120}))
                msgs = []
                while True:
                    m = await asyncio.wait_for(ws.recv(), 20)
                    if isinstance(m, str):
                        msgs.append(json.loads(m))
                        if msgs[-1].get("t") in ("done", "error"):
                            break
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                pytest.skip("Playwright Chromium not installed")
            raise
        assert msgs[-1]["t"] == "error" and "SECRET" not in json.dumps(msgs[-1])
    finally:
        await rig.stop()
    assert "SECRET-COOKIE" not in caplog.text and "RuntimeError" in caplog.text


TALL_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i style="position:absolute;top:560px;left:10px;width:200px;height:40px;font-size:16px">
<button style="position:absolute;top:400px;left:10px;width:200px;height:60px"
 onclick="document.cookie='vh='+innerHeight+'; path=/'; document.cookie='webToken=T; path=/'">go</button>"""

RESIZE_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i style="position:absolute;top:10px;left:10px;width:200px;height:40px;font-size:16px">
<button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="document.cookie='webToken=T; path=/'">go</button>
<script>addEventListener('resize',()=>{document.cookie='vh='+innerHeight+'; path=/'})</script>"""


async def test_a_dead_link_gets_a_friendly_page_not_a_bare_forbidden(local_page):
    rig = Rig(local_page)
    await rig.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{rig.port}/?t=ZZZTOKEN")
        assert r.status_code == 403
        assert "text/html" in r.headers["content-type"]
        assert "הקישור" in r.text and "link" in r.text.lower()  # Hebrew first, English second
        assert "ZZZTOKEN" not in r.text  # the token is never echoed back
    finally:
        await rig.stop()


def test_the_login_starts_on_the_ministry_sign_in_and_links_last_half_an_hour():
    from easytop.login.server import CODE_LOGIN_URL
    from easytop.onboarding.links import TTL_SECONDS

    async def on_connected(parent_id, cookies): ...

    server = LoginServer(ConnectLinks(), on_connected)
    assert server.start_route == "ministry" and server.target_url is None
    assert CODE_LOGIN_URL.endswith("/account/oneTimePassword")  # still the address of the code tab
    assert TTL_SECONDS == 1800


async def test_taps_still_land_when_the_phone_draws_the_picture_at_a_different_size(caplog):
    """The calibration bug seen on a real phone: whatever size the picture is drawn at, a tap on a button
    in the picture must hit that button. Here the picture is squeezed to half height on purpose."""
    from playwright.async_api import async_playwright

    class P(Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(RESIZE_PAGE)

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/")
    await rig.start()
    caplog.set_level(logging.INFO)
    try:
        token = rig.links.issue("telegram:7")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": 392, "height": 735}, has_touch=True, is_mobile=True
            )
            page = await ctx.new_page()
            await page.route(
                "**/telegram-web-app.js",
                lambda r: r.fulfill(body="", content_type="text/javascript"),
            )
            await page.goto(f"http://127.0.0.1:{rig.port}/?t={token}")
            await page.wait_for_selector("#msg", state="hidden", timeout=30000)
            await page.evaluate("document.getElementById('screen').style.height = '350px'")
            await asyncio.sleep(3.2)  # the page reports its sizes once, after about 2.5 s
            await page.touchscreen.tap(
                50, 48 + 70
            )  # the button (remote y 130 of 652) drawn at 70 of 350, below the 48 px bar
            await asyncio.wait_for(rig.event.wait(), 20)
            await browser.close()
    finally:
        await rig.stop()
        src.shutdown()
    assert "phone view:" in caplog.text and "innerW" in caplog.text and "vvScale" in caplog.text


async def test_when_the_keyboard_opens_the_picture_slides_up_and_the_remote_screen_keeps_its_size():
    """Real Chrome cannot be resized reliably (its frames come out the wrong size), so the remote screen never
    changes: the phone slides the picture so the field being typed in stays above the keyboard, and taps
    still land because they are fractions of the picture."""
    from playwright.async_api import async_playwright

    class P(Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(TALL_PAGE)

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/")
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": 392, "height": 735}, has_touch=True, is_mobile=True
            )
            page = await ctx.new_page()
            await page.route(
                "**/telegram-web-app.js",
                lambda r: r.fulfill(body="", content_type="text/javascript"),
            )
            await page.goto(f"http://127.0.0.1:{rig.port}/?t={token}")
            await page.wait_for_selector("#msg", state="hidden", timeout=30000)
            await asyncio.sleep(1)
            await page.touchscreen.tap(
                50, 48 + 580
            )  # the field, low on the screen (below the 48 px bar)
            await page.wait_for_function("document.activeElement.id === 'kb'", timeout=10000)
            await page.set_viewport_size({"width": 392, "height": 400})  # the keyboard took 300 px
            await page.wait_for_function(
                "document.getElementById('screen').style.transform.includes('translateY(-')",
                timeout=10000,
            )
            top = await page.evaluate(
                "document.getElementById('screen').getBoundingClientRect().top"
            )
            bottom_of_field = 600 + top  # the field's bottom edge, where it is drawn now
            assert bottom_of_field <= 400, "the field must be above the keyboard"
            await asyncio.sleep(1)
            await page.touchscreen.tap(
                50, 430 + top
            )  # the button (remote y 400-460), now drawn higher
            await asyncio.wait_for(rig.event.wait(), 20)
            await browser.close()
        assert (
            rig.connected[0][1]["vh"] == "687"
        )  # the remote screen never changed size (700 - bar)
    finally:
        await rig.stop()
        src.shutdown()


ROUTE_PAGES = {
    "/": b"""<!doctype html><meta name=viewport content="width=device-width"><h1>code</h1>
<button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="document.cookie='route=code; path=/'; document.cookie='webToken=T; path=/'">go</button>""",
    "/pw": b"""<!doctype html><meta name=viewport content="width=device-width"><h1>password</h1>
<button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="document.cookie='route=password; path=/'; document.cookie='webToken=T; path=/'">go</button>""",
    "/login": b"""<!doctype html><meta name=viewport content="width=device-width"><h1>login</h1>
<a href="/ministry" style="position:absolute;top:300px">MINISTRY-BUTTON</a>""",
    "/ministry": b"""<!doctype html><meta name=viewport content="width=device-width"><h1>ministry</h1>
<button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="document.cookie='route=ministry; path=/'; document.cookie='webToken=T; path=/'">go</button>""",
}


class RouteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ROUTE_PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body or b"no")

    def log_message(self, *a):
        pass


async def _route_rig(explicit_target=True):
    """`explicit_target=False` is the product setup: no fixed first page, the server's start route decides."""
    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RouteHandler)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{src.server_port}"
    rig = Rig(
        base + "/" if explicit_target else None,
        routes={"code": base + "/", "password": base + "/pw", "ministry": base + "/login"},
        ministry_button="MINISTRY-BUTTON",
    )
    await rig.start()
    return src, rig


@pytest.mark.parametrize("route", ["password", "ministry"])
async def test_the_parent_can_switch_between_the_three_sign_in_routes(route):
    from playwright.async_api import async_playwright

    src, rig = await _route_rig()
    try:
        token = rig.links.issue("telegram:7")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": 392, "height": 735}, has_touch=True, is_mobile=True
            )
            page = await ctx.new_page()
            await page.route(
                "**/telegram-web-app.js",
                lambda r: r.fulfill(body="", content_type="text/javascript"),
            )
            await page.goto(f"http://127.0.0.1:{rig.port}/?t={token}")
            await page.wait_for_selector("#msg", state="hidden", timeout=30000)
            labels = await page.eval_on_selector_all(
                "#bar button", "els => els.map(e => e.dataset.r)"
            )
            assert labels == ["ministry", "code", "password"]
            await page.click(f'#bar button[data-r="{route}"]')
            await asyncio.sleep(3)  # the remote browser navigates
            bar_h = await page.evaluate("document.getElementById('bar').offsetHeight")
            await page.touchscreen.tap(
                50, bar_h + 130
            )  # the remote button on whichever page is showing
            await asyncio.wait_for(rig.event.wait(), 20)
            await browser.close()
        assert rig.connected[0][1]["route"] == route
    finally:
        await rig.stop()
        src.shutdown()


async def test_a_parent_who_does_not_switch_signs_in_on_the_ministry_route():
    from playwright.async_api import async_playwright

    src, rig = await _route_rig(explicit_target=False)
    try:
        token = rig.links.issue("telegram:7")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": 392, "height": 735}, has_touch=True, is_mobile=True
            )
            page = await ctx.new_page()
            await page.route(
                "**/telegram-web-app.js",
                lambda r: r.fulfill(body="", content_type="text/javascript"),
            )
            await page.goto(f"http://127.0.0.1:{rig.port}/?t={token}")
            await page.wait_for_selector("#msg", state="hidden", timeout=30000)
            assert await page.eval_on_selector("#bar button.on", "e => e.dataset.r") == "ministry"
            await asyncio.sleep(
                3
            )  # the waiting page opens the sign-in and presses the Ministry button
            bar_h = await page.evaluate("document.getElementById('bar').offsetHeight")
            await page.touchscreen.tap(50, bar_h + 130)  # the remote button, no tab was touched
            await asyncio.wait_for(rig.event.wait(), 20)
            await browser.close()
        assert rig.connected[0][1]["route"] == "ministry"
    finally:
        await rig.stop()
        src.shutdown()


async def _login_once(local_page, tmp_path, **options):
    event = asyncio.Event()

    async def on_connected(parent_id, cookies):
        event.set()

    async def verify(cookies):
        return True

    links = ConnectLinks()
    server = LoginServer(
        links,
        on_connected,
        target_url=local_page,
        verify=verify,
        cookie_filter=lambda cookies: cookies,
        scale=1,
        **options,
    )
    srv = await server.serve("127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        token = links.issue("telegram:7")
        async with connect(f"ws://127.0.0.1:{port}/ws?t={token}&w=390&h=700", max_size=None) as ws:
            for _ in range(40):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    break
            await ws.send(json.dumps({"t": "tap", "x": 50, "y": 30}))
            await asyncio.sleep(0.5)
            await ws.send(json.dumps({"t": "text", "s": "abc"}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"t": "tap", "x": 50, "y": 120}))
            await asyncio.wait_for(event.wait(), 20)
    finally:
        srv.close()
        await srv.wait_closed()


async def test_sessions_are_never_written_to_disk_by_default(local_page, tmp_path):
    await _login_once(local_page, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_a_dev_save_directory_keeps_the_webtop_cookies_for_measuring_session_length(
    local_page, tmp_path
):
    save_dir = tmp_path / "sessions"
    await _login_once(local_page, tmp_path, save_dir=save_dir)
    (saved,) = list(save_dir.glob("session_*.json"))
    payload = json.loads(saved.read_text())
    assert set(payload) == {
        "saved_at",
        "cookies",
    }  # the file format load_cookies and the probe read
    assert oct(saved.stat().st_mode & 0o777) == "0o600"  # the owner only


async def test_the_dev_trace_logs_the_login_request_shape_and_the_cookie_lifetime_but_no_secret(
    caplog,
):
    """With trace on, a login request made by the remote page is summarised: flags and lengths, never the
    typed code or the cookie value."""

    class Api(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Set-Cookie", "webToken=SECRET-COOKIE; Max-Age=32400; Path=/")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""<!doctype html><button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="fetch('/server/api/user/LoginByOneTimePassword',{method:'POST',headers:{rememberme:'0'},
 body:JSON.stringify({Code:828132,rememberMe:false,UniqueId:'SECRET-UNIQUE',
 deviceDataJson:JSON.stringify({isMobile:false,isDesktop:true,screen_resolution:'393 x 720'})})})">go</button>"""
            )

        def log_message(self, *a):
            pass

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/", trace=True)
    await rig.start()
    caplog.set_level(logging.INFO)
    try:
        token = rig.links.issue("telegram:7")
        async with connect(rig.ws(token), max_size=None) as ws:
            for _ in range(40):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    break
            await ws.send(json.dumps({"t": "tap", "x": 50, "y": 130}))
            for _ in range(40):
                await asyncio.sleep(0.25)
                if "TRACE response" in caplog.text:
                    break
    finally:
        await rig.stop()
        src.shutdown()
    assert "TRACE request" in caplog.text and "LoginByOneTimePassword" in caplog.text
    assert '"webToken_hours": 9.0' in caplog.text
    assert "393 x 720" in caplog.text and '"rememberMe": false' in caplog.text
    for secret in ("SECRET-COOKIE", "SECRET-UNIQUE", "828132"):
        assert secret not in caplog.text


async def test_with_remember_on_the_page_finds_the_same_preference_cookie_webtops_own_form_sets():
    """Webtop's code screen has no 'remember me' box: its flag comes from a short-lived cookie that only the
    password form sets. --login-remember sets that same preference cookie, so the code login can be
    long-lived like the password login with the box ticked (Webtop's own default on the phone layout)."""

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""<!doctype html><button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="document.cookie='saw='+(document.cookie.includes('saveRememberMe=1')?'yes':'no')+'; path=/';
          document.cookie='webToken=T; path=/'">go</button>"""
            )

        def log_message(self, *a):
            pass

    async def run(remember: bool) -> str:
        src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=src.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{src.server_port}"
        rig = Rig(origin + "/", remember=remember, remember_origin=origin)
        await rig.start()
        try:
            token = rig.links.issue("telegram:7")
            async with connect(rig.ws(token), max_size=None) as ws:
                for _ in range(40):
                    if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                        break
                await ws.send(json.dumps({"t": "tap", "x": 50, "y": 130}))
                await asyncio.wait_for(rig.event.wait(), 20)
            return rig.connected[0][1]["saw"]
        finally:
            await rig.stop()
            src.shutdown()

    assert await run(True) == "yes"
    assert await run(False) == "no"  # off by default: nothing is set on the parent's behalf


async def test_every_dev_option_can_be_on_at_once(local_page, tmp_path):
    """Run 3 on a real phone used --login-save-dir, --login-remember and --login-trace together."""
    await _login_once(
        local_page, tmp_path, save_dir=tmp_path / "s", remember=True, trace=True, chrome=False
    )
    assert list((tmp_path / "s").glob("session_*.json"))


async def test_the_dev_trace_can_never_break_a_login_even_on_a_binary_request_body(caplog):
    """Seen on a real login: some request of Webtop's page POSTs bytes that are not UTF-8, and reading the body
    as text crashed the session. The trace looks only at login requests and survives any body."""

    class Page(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""<!doctype html><button style="position:absolute;top:100px;left:10px;width:200px;height:60px"
 onclick="fetch('/analytics',{method:'POST',body:new Uint8Array([249,1,2,255])}).then(
   ()=>fetch('/server/api/user/LoginByOneTimePassword',{method:'POST',body:new Uint8Array([249,250,255])})).then(
   ()=>{document.cookie='webToken=T; path=/'})">go</button>"""
            )

        def log_message(self, *a):
            pass

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/", trace=True)
    await rig.start()
    caplog.set_level(logging.INFO)
    try:
        token = rig.links.issue("telegram:7")
        async with connect(rig.ws(token), max_size=None) as ws:
            for _ in range(40):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    break
            await ws.send(json.dumps({"t": "tap", "x": 50, "y": 130}))
            await asyncio.wait_for(rig.event.wait(), 20)  # the login still completes
    finally:
        await rig.stop()
        src.shutdown()
    assert "UnicodeDecodeError" not in caplog.text
    assert (
        "TRACE request" in caplog.text
    )  # the login request is still summarised (body unreadable = empty)


KB_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i style="position:absolute;top:60px;left:10px;width:300px;height:40px;font-size:16px">
<button style="position:absolute;top:200px;left:10px;width:300px;height:60px">just a button</button>"""


PHONES = ["android", "ios"]  # Chrome on a Pixel, and WebKit on an iPhone


async def _kb_session(body: bytes, phone: str = "android", telegram_js: str = ""):
    """A phone page against a stand-in page; returns (phone page, cleanup, rig). `telegram_js` stands in for
    Telegram's own script (the empty default is an old client without secure storage)."""
    from playwright.async_api import async_playwright

    class P(Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/")
    await rig.start()
    pw = await async_playwright().start()
    engine, device = (pw.chromium, "Pixel 7") if phone == "android" else (pw.webkit, "iPhone 13")
    browser = await engine.launch()
    ctx = await browser.new_context(
        **{**pw.devices[device], "viewport": {"width": 392, "height": 735}}
    )
    page = await ctx.new_page()
    await page.route(
        "**/telegram-web-app.js",
        lambda r: r.fulfill(body=telegram_js, content_type="text/javascript"),
    )
    token = rig.links.issue("telegram:7")
    await page.goto(f"http://127.0.0.1:{rig.port}/?t={token}")
    await page.wait_for_selector("#msg", state="hidden", timeout=30000)
    await asyncio.sleep(1.5)  # the page learns where the text fields are

    async def cleanup():
        await browser.close()
        await pw.stop()
        await rig.stop()
        src.shutdown()

    page.rig, page.token = rig, token
    return page, cleanup


@pytest.mark.parametrize("phone", PHONES)
async def test_a_tap_on_a_plain_button_leaves_no_keyboard_focus_so_the_next_field_tap_opens_it(
    phone,
):
    """Real phone bug: after tapping something that is not a text field (the captcha), the hidden keyboard
    field stayed focused, and iOS then ignored the focus() of the next field tap, so no keyboard opened."""
    page, cleanup = await _kb_session(KB_PAGE, phone)
    try:
        active = "document.activeElement && document.activeElement.id"
        await page.touchscreen.tap(100, 48 + 230)  # the button
        await asyncio.sleep(0.8)
        assert await page.evaluate(active) != "kb"
        await page.touchscreen.tap(100, 48 + 80)  # the text field
        await asyncio.sleep(0.8)
        assert await page.evaluate(active) == "kb"
        await page.touchscreen.tap(100, 48 + 230)  # the button again
        await asyncio.sleep(0.8)
        assert await page.evaluate(active) != "kb"
        await page.touchscreen.tap(100, 48 + 80)  # and the field again: it opens again
        await asyncio.sleep(0.8)
        assert await page.evaluate(active) == "kb"
    finally:
        await cleanup()


@pytest.mark.parametrize("phone", PHONES)
async def test_tapping_a_plain_button_never_even_flashes_the_keyboard(phone):
    page, cleanup = await _kb_session(KB_PAGE, phone)
    try:
        await page.evaluate(
            "window.kbFocus = 0; document.getElementById('kb').addEventListener('focus', () => window.kbFocus++)"
        )
        await page.touchscreen.tap(100, 48 + 230)
        await asyncio.sleep(0.8)
        assert await page.evaluate("window.kbFocus") == 0
        await page.touchscreen.tap(100, 48 + 80)
        await asyncio.sleep(0.8)
        assert await page.evaluate("window.kbFocus") >= 1
    finally:
        await cleanup()


# Telegram's secure storage (Bot API 9.0, Keychain / Keystore) stood in by localStorage, so a reload keeps it.
SECURE_TELEGRAM_JS = """
(() => {
  const KEY = '__tg_secure';
  const read = () => JSON.parse(localStorage.getItem(KEY) || '{}');
  const write = (o) => localStorage.setItem(KEY, JSON.stringify(o));
  window.Telegram = { WebApp: {
    ready() {}, expand() {}, close() {}, onEvent() {}, disableVerticalSwipes() {}, lockOrientation() {},
    isVersionAtLeast: () => true, initData: '', viewportHeight: innerHeight, viewportStableHeight: innerHeight,
    SecureStorage: {
      setItem(k, v, cb) { const o = read(); o[k] = v; write(o); cb && cb(null, true); return this; },
      getItem(k, cb) { const o = read(); cb(null, k in o ? o[k] : null, false); return this; },
      removeItem(k, cb) { const o = read(); delete o[k]; write(o); cb && cb(null, true); return this; },
      clear(cb) { write({}); cb && cb(null, true); return this; },
    },
  } };
})();
"""


async def _save_details(page, **values):
    """Open the saved-details panel on the phone page, type the values, save."""
    await page.click("#vaultbtn")
    for key, value in values.items():
        await page.fill(f'#vault input[data-k="{key}"]', value)
    await page.click("#vault_save")
    await asyncio.sleep(0.5)


@pytest.mark.parametrize("phone", PHONES)
async def test_without_secure_storage_the_page_offers_no_saved_details(phone):
    page, cleanup = await _kb_session(PAGE, phone)  # an old Telegram client: no SecureStorage
    try:
        assert await page.eval_on_selector("#chips", "e => e.hidden")
        assert await page.eval_on_selector("#vaultbtn", "e => e.hidden")
    finally:
        await cleanup()


# The same storage with a password already saved on an earlier visit.
SEEDED_TELEGRAM_JS = SECURE_TELEGRAM_JS.replace(
    "const KEY = '__tg_secure';",
    "const KEY = '__tg_secure'; localStorage.setItem(KEY, JSON.stringify({ed_pass: 'abc'}));",
)


@pytest.mark.parametrize("phone", PHONES)
async def test_saving_a_detail_keeps_it_on_the_phone_and_not_in_the_panel(phone):
    page, cleanup = await _kb_session(PAGE, phone, SECURE_TELEGRAM_JS)
    try:
        assert await page.locator("#vaultbtn").is_visible()
        assert await page.locator("#chiplist button").count() == 0  # nothing saved yet
        await _save_details(page, **{"pass": "abc"})
        assert await page.locator('#chiplist button[data-k="pass"]').count() == 1
        assert (
            await page.input_value('#vault input[data-k="pass"]') == ""
        )  # the panel does not keep it
        assert await page.eval_on_selector("#vault", "e => e.hidden")
        assert "abc" in await page.evaluate("localStorage.getItem('__tg_secure')")
    finally:
        await cleanup()


@pytest.mark.parametrize("phone", PHONES)
async def test_a_saved_detail_is_typed_into_the_focused_remote_field_by_its_chip(phone, caplog):
    caplog.set_level(logging.DEBUG, logger="easytop")
    page, cleanup = await _kb_session(PAGE, phone, SEEDED_TELEGRAM_JS)
    try:
        assert await page.locator('#chiplist button[data-k="pass"]').count() == 1
        assert await page.eval_on_selector(
            "#chips", "e => e.hidden"
        )  # not over the picture until a field is focused
        await page.touchscreen.tap(50, 48 + 30)  # the remote text field
        await asyncio.sleep(0.8)
        assert not await page.eval_on_selector("#chips", "e => e.hidden")
        await page.tap('#chiplist button[data-k="pass"]')
        await asyncio.sleep(0.8)
        assert await page.evaluate("document.activeElement && document.activeElement.id") == "kb"
        await page.touchscreen.tap(
            50, 48 + 120
        )  # the remote button: sets the cookie only if the field says abc
        await asyncio.wait_for(page.rig.event.wait(), 20)
        assert page.rig.connected[0][1]["webToken"] == "SECRET-COOKIE"
        ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("easytop"))
        assert (
            "abc" not in ours
        )  # the typed detail is never logged by our code (the websockets library is not ours)
    finally:
        await cleanup()


@pytest.mark.parametrize("phone", PHONES)
async def test_forgetting_removes_every_saved_detail_from_the_phone(phone):
    page, cleanup = await _kb_session(PAGE, phone, SECURE_TELEGRAM_JS)
    try:
        await _save_details(page, id="123456789", phone="0501234567")
        assert await page.locator("#chiplist button").count() == 2
        await page.click("#vaultbtn")
        await page.click("#vault_forget")
        await asyncio.sleep(0.5)
        assert await page.locator("#chiplist button").count() == 0
        assert await page.evaluate("localStorage.getItem('__tg_secure')") in ("{}", None)
    finally:
        await cleanup()


CAPTCHA_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i style="position:absolute;top:60px;left:10px;width:300px;height:40px;font-size:16px">
<iframe style="position:absolute;top:300px;left:10px;width:300px;height:80px;border:1px solid #888"
 srcdoc="<input type=checkbox style='width:40px;height:40px;margin:20px'> I am not a robot"></iframe>"""


@pytest.mark.parametrize("phone", PHONES)
async def test_solving_a_captcha_style_frame_first_does_not_stop_the_keyboard_opening_for_the_fields(
    phone,
):
    """The reported bug: tap the captcha before the fields, then the keyboard would not open for them."""
    page, cleanup = await _kb_session(CAPTCHA_PAGE, phone)
    try:
        active = "document.activeElement && document.activeElement.id"
        await page.touchscreen.tap(50, 48 + 340)  # the checkbox inside the embedded frame
        await asyncio.sleep(0.8)
        assert await page.evaluate(active) != "kb"
        for _ in range(3):  # and every field tap afterwards still opens it
            await page.touchscreen.tap(100, 48 + 80)
            await asyncio.sleep(0.6)
            assert await page.evaluate(active) == "kb"
            await page.touchscreen.tap(50, 48 + 340)  # back to the frame
            await asyncio.sleep(0.6)
            assert await page.evaluate(active) != "kb"
    finally:
        await cleanup()


TYPING_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=i value="abc" style="position:absolute;top:60px;left:10px;width:300px;height:40px;font-size:16px">
<button style="position:absolute;top:200px;left:10px;width:300px;height:60px"
 onclick="document.cookie='val='+encodeURIComponent(document.getElementById('i').value)+'; path=/'">read</button>"""

# How a phone keyboard really types: Gboard composes a word letter by letter and replaces the whole composing
# region when it autocorrects; the page sees input events on its hidden field with isComposing true.
_IME_JS = """async (steps) => {
  const kb = document.getElementById('kb');
  const base = kb.value;
  kb.dispatchEvent(new CompositionEvent('compositionstart'));
  for (const s of steps) {
    kb.value = base + s;
    kb.dispatchEvent(new InputEvent('input', {inputType: 'insertCompositionText', data: s, isComposing: true}));
    await new Promise((r) => setTimeout(r, 40));
  }
  kb.dispatchEvent(new CompositionEvent('compositionend', {data: steps[steps.length - 1]}));
}"""


async def _typing(phone, actions):
    """Run `actions(page)` against the typing page, then return the remote input's value."""
    import urllib.parse

    from playwright.async_api import async_playwright

    done = {}

    class P(Handler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(TYPING_PAGE)

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    event = asyncio.Event()
    links = ConnectLinks()

    async def on_connected(parent_id, cookies):
        done.update(cookies)
        event.set()

    async def verify(cookies):
        return "val" in {c["name"] for c in cookies}

    server = LoginServer(
        links,
        on_connected,
        target_url=f"http://127.0.0.1:{src.server_port}/",
        verify=verify,
        cookie_filter=lambda cookies: [c for c in cookies] + [{"name": "webToken", "value": "t"}],
        scale=1,
    )
    srv = await server.serve("127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    async with async_playwright() as pw:
        engine, device = (
            (pw.chromium, "Pixel 7") if phone == "android" else (pw.webkit, "iPhone 13")
        )
        browser = await engine.launch()
        ctx = await browser.new_context(
            **{**pw.devices[device], "viewport": {"width": 392, "height": 735}}
        )
        page = await ctx.new_page()
        await page.route(
            "**/telegram-web-app.js", lambda r: r.fulfill(body="", content_type="text/javascript")
        )
        try:
            await page.goto(f"http://127.0.0.1:{port}/?t={links.issue('telegram:7')}")
            await page.wait_for_selector("#msg", state="hidden", timeout=30000)
            await asyncio.sleep(1.5)
            await page.touchscreen.tap(300, 48 + 80)  # the field, to the right of its text
            await asyncio.sleep(0.6)
            await actions(page)
            await asyncio.sleep(0.8)
            await page.touchscreen.tap(100, 48 + 230)  # read the value out
            await asyncio.wait_for(event.wait(), 20)
        finally:
            await browser.close()
            srv.close()
            await srv.wait_closed()
            src.shutdown()
    return urllib.parse.unquote(done["val"])


@pytest.mark.parametrize("phone", PHONES)
async def test_a_word_composed_letter_by_letter_arrives_whole_and_once(phone):
    async def act(page):
        await page.evaluate(_IME_JS, ["s", "sh", "sha", "shal", "shalom"])

    assert await _typing(phone, act) == "abcshalom"


@pytest.mark.parametrize("phone", PHONES)
async def test_an_autocorrect_that_replaces_the_word_replaces_it_on_the_remote_too(phone):
    async def act(page):
        await page.evaluate(_IME_JS, ["h", "he", "hel", "helo"])  # typed with a typo
        await page.evaluate(_IME_JS, ["hello"])  # the keyboard autocorrects it

    # The second composition starts after the first committed "helo"; the keyboard replaces that word, so a real
    # Gboard reports value base+"hello" where base is what preceded the word.
    assert await _typing(phone, act) in ("abchelohello", "abchello")


@pytest.mark.parametrize("phone", PHONES)
async def test_hebrew_composition_arrives_intact(phone):
    async def act(page):
        await page.evaluate(_IME_JS, ["ש", "של", "שלו", "שלום"])

    assert await _typing(phone, act) == "abcשלום"


@pytest.mark.parametrize("phone", PHONES)
async def test_backspace_deletes_text_that_was_already_in_the_field(phone):
    async def act(page):
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Backspace")

    assert await _typing(phone, act) == "a"  # the field held "abc": two Backspaces leave "a"


@pytest.mark.parametrize("phone", PHONES)
async def test_backspace_after_typing_deletes_the_typed_text_first_then_the_old(phone):
    async def act(page):
        await page.keyboard.type("xy")
        for _ in range(3):
            await page.keyboard.press("Backspace")

    assert await _typing(phone, act) == "ab"  # abc + xy, then three deletions: y, x, c


POPUP_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<div style="position:absolute;top:60px;left:10px;width:300px;height:50px">
  <input id=own style="width:300px;height:40px;font-size:16px">
  <div id=label style="position:absolute;top:0;left:0;width:300px;height:40px"></div>
</div>
<input id=hidden style="position:absolute;top:300px;left:10px;width:300px;height:40px;font-size:16px">
<div id=popup style="position:absolute;top:250px;left:0;width:390px;height:200px;background:#eee"> picture grid </div>"""


async def test_a_field_hidden_under_a_popup_does_not_open_the_keyboard_but_one_under_its_own_label_does():
    """Real phone bug: tapping a captcha picture that sat over a text field briefly opened the keyboard."""
    page, cleanup = await _kb_session(POPUP_PAGE, "android")
    try:
        await page.evaluate(
            "window.kbFocus = 0; document.getElementById('kb').addEventListener('focus', () => window.kbFocus++)"
        )
        await page.touchscreen.tap(100, 48 + 320)  # the captcha-like popup covers this field
        await asyncio.sleep(0.8)
        assert await page.evaluate("window.kbFocus") == 0
        await page.touchscreen.tap(100, 48 + 80)  # a field with a transparent label laid over it
        await asyncio.sleep(0.8)
        assert await page.evaluate("window.kbFocus") >= 1
    finally:
        await cleanup()


async def test_frames_are_not_compressed_on_the_wire(local_page):
    """JPEG pictures do not compress; deflate only adds delay."""
    rig = Rig(local_page)
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        async with connect(rig.ws(token), max_size=None) as ws:
            assert ws.protocol.extensions == []
    finally:
        await rig.stop()


async def test_a_burst_of_taps_is_handled_without_waiting_after_each_one():
    """Captcha picture grids are tapped several times in a row: each tap must not hold up the next."""
    import time as _time

    class Counter(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""<!doctype html><meta name=viewport content="width=device-width">
<script>var n=0</script>
<div style="position:absolute;top:100px;left:10px;width:200px;height:60px;background:#ccf"
 onclick="n++">count</div>
<div style="position:absolute;top:300px;left:10px;width:200px;height:60px;background:#cfc"
 onclick="document.cookie='n='+n+'; path=/'; document.cookie='webToken=T; path=/'">done</div>"""
            )

        def log_message(self, *a):
            pass

    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counter)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    rig = Rig(f"http://127.0.0.1:{src.server_port}/")
    await rig.start()
    try:
        token = rig.links.issue("telegram:7")
        async with connect(rig.ws(token), max_size=None) as ws:
            for _ in range(40):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    break
            await asyncio.sleep(1.0)
            started = _time.monotonic()
            for _ in range(12):
                await ws.send(json.dumps({"t": "tap", "x": 50, "y": 130}))
            await ws.send(json.dumps({"t": "tap", "x": 50, "y": 330}))
            await asyncio.wait_for(rig.event.wait(), 20)
            elapsed = _time.monotonic() - started
        assert rig.connected[0][1]["n"] == "12"
        assert elapsed < 1.5, (
            f"12 taps took {elapsed:.1f} s"
        )  # 12 x (0.15 s wait + checks) would be > 2 s
    finally:
        await rig.stop()
        src.shutdown()


async def _first_picture_seconds(local_page, *, warm: bool) -> float:
    import time as _time

    rig = Rig(local_page)
    await rig.start()
    try:
        if warm:
            await rig.server.warm()
        token = rig.links.issue("telegram:7")
        started = _time.monotonic()
        async with connect(rig.ws(token), max_size=None) as ws:
            for _ in range(80):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    return _time.monotonic() - started
        raise AssertionError("no picture")
    finally:
        await rig.server.aclose()
        await rig.stop()


async def test_a_ready_page_shows_its_first_picture_sooner_than_a_cold_start(local_page):
    cold = await _first_picture_seconds(local_page, warm=False)
    warm = await _first_picture_seconds(local_page, warm=True)
    assert warm < cold and warm < 1.0, f"cold {cold:.2f} s, warm {warm:.2f} s"


async def test_after_a_login_another_ready_page_is_prepared_for_the_next_parent(local_page):
    rig = Rig(local_page)
    await rig.start()
    try:
        await rig.server.warm()
        first = rig.server._warm
        token = rig.links.issue("telegram:7")
        async with connect(rig.ws(token), max_size=None) as ws:
            for _ in range(80):
                if isinstance(await asyncio.wait_for(ws.recv(), 20), bytes):
                    break
        for _ in range(60):  # the next page is prepared in the background once the session ends
            await asyncio.sleep(0.25)
            if rig.server._warm is not None and rig.server._warm is not first:
                break
        assert rig.server._warm is not None and rig.server._warm is not first
    finally:
        await rig.server.aclose()
        await rig.stop()


# Webtop's real fields: a 16 px input inside a 65 px tall mat-form-field whose label sits over it. Like Angular
# Material, a click anywhere in the box focuses the input.
MAT_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<mat-form-field onclick="document.getElementById('real').focus()"  style="display:block;position:absolute;top:100px;left:20px;width:314px;height:65px">
  <label for=real style="position:absolute;top:0;left:0;width:314px;height:50px">Mobile number *</label>
  <input id=real style="position:absolute;top:23px;left:0;width:314px;height:16px;font-size:16px;border:0">
</mat-form-field>
<button style="position:absolute;top:300px;left:20px;width:300px;height:60px">just a button</button>"""


@pytest.mark.parametrize("phone", PHONES)
async def test_tapping_anywhere_on_the_visible_field_box_opens_the_keyboard_not_only_its_16_px_input(
    phone,
):
    page, cleanup = await _kb_session(MAT_PAGE, phone)
    try:
        active = "document.activeElement && document.activeElement.id"
        for y in (
            100 + 8,
            100 + 30,
            100 + 60,
        ):  # the label text, the input line, the bottom of the field
            await page.touchscreen.tap(150, 48 + y)
            await asyncio.sleep(0.7)
            assert await page.evaluate(active) == "kb", f"tap at remote y={y}"
            await page.touchscreen.tap(150, 48 + 330)  # the button: keyboard closes
            await asyncio.sleep(0.5)
            assert await page.evaluate(active) != "kb"
    finally:
        await cleanup()


# A field the page itself focuses from a click handler on a covering element: the phone cannot know to open the
# keyboard inside the tap, so a keyboard button must appear as a sure way in.
WRAPPED_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width">
<input id=inner style="position:absolute;top:100px;left:20px;width:300px;height:40px;font-size:16px">
<div id=cover style="position:absolute;top:90px;left:10px;width:330px;height:60px"
 onclick="document.getElementById('inner').focus()"></div>"""


@pytest.mark.parametrize("phone", PHONES)
async def test_when_the_keyboard_could_not_open_in_the_tap_a_keyboard_button_gets_it_open(phone):
    page, cleanup = await _kb_session(WRAPPED_PAGE, phone)
    try:
        active = "document.activeElement && document.activeElement.id"
        await page.touchscreen.tap(
            150, 48 + 120
        )  # the covering element: the remote input focuses itself
        await page.wait_for_function("!document.getElementById('kbbtn').hidden", timeout=5000)
        assert await page.evaluate(active) != "kb"  # no keyboard could open without a real gesture
        await page.click("#kbbtn")  # a real tap on the button
        await asyncio.sleep(0.5)
        assert await page.evaluate(active) == "kb"
        await page.set_viewport_size(
            {"width": 392, "height": 435}
        )  # the keyboard really opened: the screen shrank
        await page.wait_for_function("document.getElementById('kbbtn').hidden", timeout=5000)
    finally:
        await cleanup()


@pytest.mark.parametrize("phone", PHONES)
async def test_the_keyboard_button_also_appears_when_the_field_is_focused_but_the_keyboard_stays_closed(
    phone,
):
    """Real Android: every field tap showed the hidden field as focused, yet no keyboard appeared for 15 s, and
    the button stayed hidden because the field counted as focused. Judge by the screen, not by focus."""
    page, cleanup = await _kb_session(KB_PAGE, phone)
    try:
        await page.touchscreen.tap(
            100, 48 + 80
        )  # a real text field: the page focuses its hidden field
        await asyncio.sleep(0.2)
        assert await page.evaluate("document.activeElement.id") == "kb"
        assert await page.evaluate(
            "document.getElementById('kbbtn').hidden"
        )  # not yet: give the keyboard time
        await page.wait_for_function("!document.getElementById('kbbtn').hidden", timeout=5000)
        await page.set_viewport_size({"width": 392, "height": 435})  # now the keyboard opens
        await page.wait_for_function("document.getElementById('kbbtn').hidden", timeout=5000)
    finally:
        await cleanup()


@pytest.mark.parametrize("phone", PHONES)
async def test_a_tap_reaches_the_page_as_a_genuine_click_so_the_phone_allows_the_keyboard(phone):
    """The page used to cancel the touch events, which also cancels the click. A phone is surest to open the
    keyboard for a focus() made while handling a genuine click."""
    page, cleanup = await _kb_session(KB_PAGE, phone)
    try:
        await page.evaluate(
            "window.clicks = []; document.getElementById('screen').addEventListener('click', e => window.clicks.push(e.isTrusted), true)"
        )
        await page.touchscreen.tap(100, 48 + 80)
        await asyncio.sleep(0.5)
        assert await page.evaluate("window.clicks") == [True]
    finally:
        await cleanup()
