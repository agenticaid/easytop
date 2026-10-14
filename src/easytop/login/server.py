"""The phone-only Webtop login: a server-side Chromium streamed into a Telegram Mini App (Spec 04, option 2).

The parent logs in themselves on the real Webtop page: they solve the captcha and type the one-time code.
We forward taps and typing to a Chromium on this server and send its screen back as JPEG frames. We never
read, extract or replay a captcha token. When the browser holds a Webtop session that Webtop itself calls
valid (`dashboard/CheckToken`), the Webtop cookies are handed to `on_connected(parent_id, cookies)` and
nothing is written to disk here.

Who may open a session: the link token (one-time, made for one parent by `ConnectLinks`) and, when a
`viewer_check` is given, proof that the viewer is that very Telegram user (signed Mini App initData).
Privacy: nothing the phone types and no cookie value is ever logged; the log holds counts and timings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from easytop import analytics
from easytop.login import protocol
from easytop.onboarding.links import ConnectLinks

log = logging.getLogger("easytop.login")
STATIC = Path(__file__).parent / "static" / "index.html"
WEB_ORIGIN = "https://webtop.smartschool.co.il"
# The sign-in starts on the Ministry of Education route (Erez 2026-10-11): it showed no captcha to solve and gave
# the session that lived longest. The code tab (SMS / email / voice, also for a forgotten password) and the
# password tab are one tap away.
DEFAULT_ROUTE = "ministry"
CODE_LOGIN_URL = WEB_ORIGIN + "/account/oneTimePassword"
ROUTE_URLS = {
    "code": CODE_LOGIN_URL,  # SMS / email / voice one-time code
    "password": WEB_ORIGIN + "/account/login",  # user name and password
    "ministry": WEB_ORIGIN
    + "/account/login",  # ...then the page's own "Ministry of Education" button
}
MINISTRY_BUTTON = "הזדהות משרד החינוך"
DEAD_LINK_PAGE = """<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Webtop</title>
<body style="font:18px/1.5 system-ui,sans-serif;margin:0;padding:32px 24px;text-align:center">
<p>הקישור פג או כבר נוצל. שלחו הודעה לבוט, ותקבלו כפתור חדש.</p>
<p dir="ltr">This link has expired or was already used. Send the bot a message to get a new button.</p>
</body></html>""".encode()

Verify = Callable[[list[dict]], Awaitable[bool]]
OnConnected = Callable[[str, dict[str, str]], Awaitable[None]]
ViewerCheck = Callable[
    [str, str], bool
]  # (parent_id, Mini App initData) -> may this viewer log in?

_SCROLL_TO_FOCUS_JS = (
    "() => document.activeElement && document.activeElement.scrollIntoView({block: 'center'})"
)
# Is a text field focused, and where does it end (as a fraction of the screen height)?
_EDITABLE_JS = """() => {
  const e = document.activeElement;
  const text = !!e && (e.isContentEditable || e.tagName === 'TEXTAREA' ||
    (e.tagName === 'INPUT' && !['checkbox','radio','button','submit','image','file'].includes(e.type)));
  if (!text) return { on: false, bottom: 0 };
  return { on: true, bottom: e.getBoundingClientRect().bottom / window.innerHeight };
}"""


# Where are the visible text fields? Rectangles as fractions of the screen, so the phone knows locally which
# taps need the keyboard (iOS opens it only when asked inside the tap itself).
_FIELDS_JS = """() => {
  const W = window.innerWidth, H = window.innerHeight, out = [];
  let covered = 0, total = 0;
  const skip = ['checkbox', 'radio', 'button', 'submit', 'image', 'file', 'hidden'];
  for (const e of document.querySelectorAll('input, textarea, [contenteditable=""], [contenteditable="true"]')) {
    if (e.tagName === 'INPUT' && skip.includes(e.type)) continue;
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    if (r.width < 8 || r.height < 8 || cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (r.bottom < 0 || r.top > H || r.right < 0 || r.left > W) continue;
    total++;
    // Covered by something else (the captcha picture grid, a dialog)? Then a tap there is not on this field. A
    // transparent label or icon laid over the field inside its own box does not count as covering.
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), W - 1), cy = Math.min(Math.max(r.top + r.height / 2, 0), H - 1);
    const top = document.elementFromPoint(cx, cy);
    const box = e.closest('mat-form-field, label, .mat-mdc-form-field') ||
      (e.parentElement && e.parentElement !== document.body && e.parentElement !== document.documentElement ? e.parentElement : e);
    if (top && top !== e && !e.contains(top) && !box.contains(top)) { covered++; continue; }
    // The tap target is the whole visible field (Webtop's is a 65 px box around a 16 px input), not just the input.
    let t = { left: r.left, top: r.top - 10, right: r.right, bottom: r.bottom + 10 };
    const wrap = e.closest('mat-form-field, .mat-mdc-form-field, label');
    if (wrap) {
      const b = wrap.getBoundingClientRect();
      if (b.height <= 120 && b.width <= W && b.top <= r.top + 1 && b.bottom >= r.bottom - 1) t = b;
    }
    out.push([t.left / W, t.top / H, t.right / W, t.bottom / H].map((v) => Math.round(v * 1000) / 1000));
  }
  return { r: out, covered, total };
}"""


async def webtop_verify(cookies: list[dict]) -> bool:
    """True when Webtop itself says these cookies are a live session (`dashboard/CheckToken`)."""
    from easytop.connector.http import WEBTOP_HEADERS

    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in protocol.webtop_cookies(cookies))
    if not cookie_header:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://webtopserver.smartschool.co.il/server/api/dashboard/CheckToken",
                json={},
                headers={
                    **WEBTOP_HEADERS,
                    "Origin": WEB_ORIGIN,
                    "Referer": WEB_ORIGIN + "/",
                    "Cookie": cookie_header,
                },
            )
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return False
    return r.status_code == 200 and isinstance(body, dict) and body.get("data") is True


def browser_context_options(
    device: dict, *, width: int, height: int, scale: float, native_identity: bool
) -> dict:
    """Context options for the login browser: a phone-sized touch screen in Hebrew / Israel time.

    The bundled Chromium borrows a Pixel 7's identity (user agent). A real installed Chrome keeps its own:
    claiming to be an Android phone on a Mac is inconsistent, and the page's risk checks (reCAPTCHA) read
    the browser's identity. Nothing about automation is hidden either way.
    """
    options = dict(device)
    options.pop("default_browser_type", None)
    if native_identity:
        options.pop("user_agent", None)
    options.update(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
    )
    return options


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers(
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
        ]
    )
    return Response(status.value, status.phrase, headers, body)


WARM_TTL_SECONDS = 600  # a waiting page older than this is replaced, so it never goes stale


@dataclass
class _Prepared:
    context: object
    page: object
    nav: asyncio.Task  # the page load, which may still be running when a parent arrives
    created: float


class LoginServer:
    def __init__(
        self,
        links: ConnectLinks,
        on_connected: OnConnected,
        *,
        target_url: str | None = None,
        start_route: str = DEFAULT_ROUTE,
        verify: Verify = webtop_verify,
        viewer_check: ViewerCheck | None = None,
        headless: bool = True,
        chrome: bool = False,
        scale: float = 2,
        quality: int = 75,
        max_seconds: int = 600,
        cookie_filter: Callable[[list[dict]], list[dict]] = protocol.webtop_cookies,
        routes: dict[str, str] | None = None,
        save_dir: Path | None = None,
        trace: bool = False,
        remember: bool = True,
        remember_origin: str = WEB_ORIGIN,
        ministry_button: str = MINISTRY_BUTTON,
    ) -> None:
        self.links = links
        self.on_connected = on_connected
        self.target_url = target_url  # tests: a fixed first page instead of the start route
        self.start_route = start_route
        self.verify = verify
        self.viewer_check = viewer_check
        self.headless = headless
        self.chrome = chrome
        self.scale = scale
        self.quality = quality
        self.max_seconds = max_seconds
        self.cookie_filter = cookie_filter  # tests widen it to a local page's cookies
        self.routes = routes or ROUTE_URLS
        # DEV ONLY: keep each captured session as a file so its real lifetime can be probed over the next days
        self.save_dir = save_dir
        self.remember, self.remember_origin = remember, remember_origin
        self.trace = (
            trace  # DEV ONLY: log the secret-free shape of login requests (see login/trace.py)
        )
        self.ministry_button = ministry_button
        self._busy = asyncio.Lock()
        self._pw = (
            None  # the shared Playwright driver and browser: started once, one context per login
        )
        self._browser = None
        self._warm: _Prepared | None = None  # a page already loaded and waiting for the next parent
        self._keep_warm = False
        self._refresher: asyncio.Task | None = None

    # --- HTTP + handshake -------------------------------------------------------------------------

    def process_request(self, connection, request):
        parts = urlsplit(request.path)
        query = parse_qs(parts.query)
        token = query.get("t", [""])[0]
        parent_id = self.links.peek(token)
        if parent_id is None:
            if (
                parts.path == "/"
            ):  # a person opening an old button: say what to do, not just "forbidden"
                return _response(HTTPStatus.FORBIDDEN, DEAD_LINK_PAGE, "text/html; charset=utf-8")
            return _response(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        if parts.path == "/ws":
            if self.viewer_check is not None and not self.viewer_check(
                parent_id, query.get("d", [""])[0]
            ):
                log.warning("login refused: viewer is not the parent the link was made for")
                return _response(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            return None  # continue with the WebSocket handshake
        if parts.path == "/":
            return _response(HTTPStatus.OK, STATIC.read_bytes(), "text/html; charset=utf-8")
        return _response(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    async def serve(self, host: str, port: int):
        return await serve(
            self.handler,
            host,
            port,
            process_request=self.process_request,
            max_size=2**16,
            compression=None,  # JPEG pictures do not compress: deflate would only add delay
        )

    # --- one login session ------------------------------------------------------------------------

    async def handler(self, ws) -> None:
        query = parse_qs(urlsplit(ws.request.path).query)
        token = query.get("t", [""])[0]
        if self._busy.locked():
            await ws.send(json.dumps({"t": "busy"}))
            await ws.close(1013, "busy")
            return
        async with self._busy:
            parent_id, opened = self.links.peek(token), time.monotonic()
            if parent_id is not None:
                analytics.emit("connect_opened", parent_id)

            def abandoned(reason: str) -> None:
                if parent_id is not None:
                    seconds = round(time.monotonic() - opened, 1)
                    analytics.emit(
                        "connect_abandoned", parent_id, reason=reason, login_seconds=seconds
                    )

            try:
                await asyncio.wait_for(self._run(ws, token, abandoned), self.max_seconds)
            except TimeoutError:
                abandoned("timeout")
                await self._tell(ws, {"t": "error", "why": "timeout"})
            except ConnectionClosed:
                log.info("phone disconnected")
                abandoned("left")
            except Exception as e:  # never send or log details: they could hold cookie values
                log.error("login session failed: %s", type(e).__name__)
                analytics.emit_error("login", e)
                abandoned("failed")
                await self._tell(ws, {"t": "error", "why": "failed"})

    # --- the browser: shared, with a ready page waiting -------------------------------------------

    async def _ensure_browser(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        launch = {"channel": "chrome"} if self.chrome else {}
        self._browser = await self._pw.chromium.launch(headless=self.headless, **launch)

    async def _prepare(self) -> _Prepared:
        """A fresh isolated browser context with Webtop's sign-in page loading, nobody's data in it."""
        await self._ensure_browser()
        device = browser_context_options(
            self._pw.devices["Pixel 7"],
            width=protocol.CANON_W,
            height=protocol.CANON_H,
            scale=self.scale,
            native_identity=self.chrome,
        )
        context = await self._browser.new_context(**device)
        if self.remember:
            # The same short-lived preference Webtop's own password form sets when its "remember me" box (ticked
            # by default on the phone layout) stays ticked; the code screen has no box and reads only this.
            await context.add_cookies(
                [
                    {
                        "name": "saveRememberMe",
                        "value": "1",
                        "url": self.remember_origin,
                        "expires": time.time() + 1800,
                    }
                ]
            )
        page = await context.new_page()
        if self.target_url is not None:
            nav = asyncio.create_task(page.goto(self.target_url))
        else:
            nav = asyncio.create_task(self._open_route(page, self.start_route))
        return _Prepared(context, page, nav, time.monotonic())

    async def _open_route(self, page, route: str) -> None:
        """Show one of the sign-in screens. A page that cannot load raises; a missing Ministry button only leaves the
        parent on Webtop's own sign-in page, where the Ministry button is one tap away."""
        await page.goto(self.routes[route])
        if route == "ministry":
            try:
                # the page's own button (a plain navigation); the parent signs in on the Ministry's page
                await page.get_by_text(self.ministry_button).first.dispatch_event("click")
            except Exception as e:
                log.warning("could not press the Ministry button: %s", type(e).__name__)

    async def warm(self, *, force: bool = False) -> None:
        """Have a page loaded and waiting, so the next parent sees Webtop at once instead of after a browser start
        and a page load. Safe to call at any time; failures only mean the next parent waits as before."""
        self._keep_warm = True
        if self._refresher is None:
            self._refresher = asyncio.create_task(self._refresh_when_old())
        if (
            not force
            and self._warm is not None
            and time.monotonic() - self._warm.created < WARM_TTL_SECONDS
        ):
            return
        old, self._warm = self._warm, None
        if old is not None:
            await self._discard(old)
        try:
            self._warm = await self._prepare()
        except Exception as e:
            log.warning("could not prepare a waiting page: %s", type(e).__name__)

    async def _refresh_when_old(self) -> None:
        """A page nobody connected to is replaced before it gets old, so the next parent still finds a fresh one."""
        while self._keep_warm:
            await asyncio.sleep(60)
            if (
                self._keep_warm
                and not self._busy.locked()
                and (
                    self._warm is None
                    or time.monotonic() - self._warm.created > WARM_TTL_SECONDS * 0.8
                )
            ):
                await self.warm(force=True)

    @staticmethod
    async def _discard(prepared: _Prepared) -> None:
        prepared.nav.cancel()
        with contextlib.suppress(Exception):
            await prepared.context.close()

    @contextlib.asynccontextmanager
    async def _taken(self):
        prepared, self._warm = self._warm, None
        if prepared is not None and time.monotonic() - prepared.created > WARM_TTL_SECONDS:
            await self._discard(prepared)
            prepared = None
        if prepared is None:
            prepared = await self._prepare()
        try:
            yield prepared
        finally:
            await self._discard(prepared)
            if self._keep_warm:  # the next parent's page is prepared in the background
                asyncio.create_task(self.warm())

    async def aclose(self) -> None:
        self._keep_warm = False
        if self._refresher is not None:
            self._refresher.cancel()
            self._refresher = None
        if self._warm is not None:
            await self._discard(self._warm)
            self._warm = None
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._browser = self._pw = None

    @staticmethod
    async def _tell(ws, msg: dict) -> None:
        try:
            await ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass

    async def _run(
        self, ws, token: str, abandoned: Callable[[str], None] = lambda reason: None
    ) -> None:
        width, height = protocol.CANON_W, protocol.CANON_H

        started = time.monotonic()
        stats = {
            "frames": 0,
            "dropped": 0,
            "bytes": 0,
            "first_frame_s": None,
            "taps": 0,
            "text_msgs": 0,
            "keys": 0,
        }
        rtts: list[float] = []
        tab = [
            self.start_route
        ]  # the sign-in tab the parent is on (the analytics route when no cookie tells)
        async with self._taken() as prepared:
            context, page = prepared.context, prepared.page
            cdp = await context.new_cdp_session(
                page
            )  # opened now: a navigation in flight can detach an older one
            editable = False

            tap_at: list[float | None] = [
                None
            ]  # when the last tap was made, until the next picture is sent
            tap_to_picture: list[float] = []

            async def send_frame(params: dict) -> None:
                data = base64.b64decode(params["data"])
                backlog = (
                    ws.transport.get_write_buffer_size() if getattr(ws, "transport", None) else 0
                )
                if protocol.should_drop_frame(buffered_bytes=backlog):
                    stats["dropped"] += (
                        1  # the connection is behind: this picture would only be stale
                    )
                else:
                    try:
                        await ws.send(data)
                    except ConnectionClosed:
                        return
                    stats["frames"] += 1
                    stats["bytes"] += len(data)
                    if stats["first_frame_s"] is None:
                        stats["first_frame_s"] = round(time.monotonic() - started, 2)
                    if tap_at[0] is not None:
                        tap_to_picture.append((time.monotonic() - tap_at[0]) * 1000)
                        tap_at[0] = None
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                except (
                    Exception
                ):  # the browser closed while a picture was in flight: nothing to acknowledge
                    return

            cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(send_frame(p)))
            screencast = {
                "format": "jpeg",
                "quality": self.quality,
                "maxWidth": int(width * self.scale),
                "maxHeight": int(height * self.scale),
                "everyNthFrame": 1,
            }
            for attempt in range(6):
                try:
                    await cdp.send("Page.startScreencast", screencast)
                    break
                except Exception:
                    # A navigation in flight can detach the channel for a moment: open a fresh one and retry.
                    if attempt == 5:
                        raise
                    await asyncio.sleep(0.15)
                    cdp = await context.new_cdp_session(page)
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(send_frame(p)))

            fields_seen: list = [None]
            fields_counts: list = [None]

            async def report_fields() -> None:
                try:
                    found = await page.evaluate(_FIELDS_JS)
                except Exception:  # mid-navigation: the next poll will do
                    return
                rects = found["r"]
                counts = (len(rects), found["covered"], found["total"])
                if counts != fields_counts[0]:
                    fields_counts[0] = counts
                    log.info(
                        "remote text fields: visible=%d hidden-under-something=%d on-screen=%d",
                        *counts,
                    )
                if rects != fields_seen[0]:
                    fields_seen[0] = rects
                    await self._tell(ws, {"t": "fields", "r": rects})

            report_wanted = asyncio.Event()

            async def report_loop() -> None:
                while True:
                    await report_wanted.wait()
                    report_wanted.clear()  # taps that arrive meanwhile are covered by the next round
                    try:
                        await report_editable()
                    except Exception as e:  # a page mid-navigation: the next tap or poll will do
                        log.debug("could not read the focus state: %s", type(e).__name__)

            async def poll_fields() -> None:
                while True:
                    await report_fields()
                    await asyncio.sleep(0.5)

            async def report_editable() -> None:
                nonlocal editable
                await asyncio.sleep(0.15)
                state = await page.evaluate(_EDITABLE_JS)
                editable = bool(state["on"])
                log.info("after the tap the remote page is on a text field: %s", editable)
                # Sent after every tap, also when nothing changed: the phone must close the keyboard whenever the
                # tapped thing is not a text field, and refocus it when it is.
                await self._tell(
                    ws,
                    {"t": "editable", "on": editable, "bottom": round(float(state["bottom"]), 4)},
                )
                await report_fields()

            async def pump_input() -> None:
                async for raw in ws:
                    msg = protocol.parse_client_message(raw, width=width, height=height)
                    if msg is None:
                        continue
                    kind = msg["t"]
                    if kind == "tap":
                        stats["taps"] += 1
                        tap_at[0] = time.monotonic()
                        await page.touchscreen.tap(msg["x"], msg["y"])
                        report_wanted.set()  # answered in the background, never holding up the next tap
                    elif kind == "route":
                        tab[0] = msg["to"]
                        for t in route_tasks:
                            t.cancel()
                        route_tasks[:] = [asyncio.create_task(show_route(msg["to"]))]
                    elif kind == "diag":
                        log.info("phone view: %s", json.dumps(msg["v"]))
                    elif kind == "scroll":
                        await page.mouse.move(msg["x"], msg["y"])
                        await page.mouse.wheel(0, msg["dy"])
                    elif kind == "text":
                        stats["text_msgs"] += 1
                        await page.keyboard.type(
                            msg["s"]
                        )  # never logged: may be a password or code
                    elif kind == "key":
                        stats["keys"] += 1
                        await page.keyboard.press(msg["k"])
                        report_wanted.set()
                    elif kind == "ping":
                        await self._tell(ws, {"t": "pong", "c": msg["c"]})
                    elif kind == "lat":
                        rtts.append(msg["ms"])

            async def watch_login() -> list[dict]:
                last_token, last_check = None, 0.0
                while True:
                    await asyncio.sleep(1.5)
                    cookies = await context.cookies()
                    tok = next(
                        (
                            c["value"]
                            for c in self.cookie_filter(cookies)
                            if c["name"] == "webToken"
                        ),
                        None,
                    )
                    if tok and (tok != last_token or time.monotonic() - last_check > 10):
                        last_token, last_check = tok, time.monotonic()
                        if await self.verify(cookies):
                            return cookies

            # The page loads in the background: taps are handled from the first moment instead of queueing
            # for the several seconds Webtop takes to finish loading.
            route_tasks: list[asyncio.Task] = []
            if self.trace:
                from easytop.login import trace as login_trace

                def on_request(req) -> None:
                    # Tracing is a dev aid: whatever a request looks like, it must never break the login.
                    try:
                        if login_trace.login_endpoint(req.url) is None:
                            return  # only login requests are looked at; other bodies may be binary
                        raw = req.post_data_buffer  # bytes: `post_data` raises on non-UTF-8 bodies
                        text = raw.decode("utf-8", errors="replace") if raw else None
                        summary = login_trace.summarize_request(req.url, req.headers, text)
                        if summary is not None:
                            log.info("TRACE request: %s", json.dumps(summary, ensure_ascii=False))
                    except Exception as e:
                        log.warning("trace skipped a request: %s", type(e).__name__)

                async def on_response(resp) -> None:
                    try:
                        endpoint = login_trace.login_endpoint(resp.url)
                        if endpoint is None:
                            return
                        cookies = [
                            h["value"]
                            for h in await resp.headers_array()
                            if h["name"].lower() == "set-cookie"
                        ]
                        life = login_trace.webtoken_lifetime_from_set_cookie(
                            cookies, now=time.time()
                        )
                        log.info(
                            "TRACE response: %s",
                            json.dumps({"endpoint": endpoint, "status": resp.status, **life}),
                        )
                    except Exception as e:
                        log.warning("trace skipped a response: %s", type(e).__name__)

                page.on("request", on_request)
                page.on("response", lambda r: asyncio.create_task(on_response(r)))

            async def show_route(route: str) -> None:
                try:
                    await self._open_route(page, route)
                except Exception as e:  # a failed switch must not end the login
                    log.warning("could not show the %s sign-in: %s", route, type(e).__name__)

            nav_task = prepared.nav
            input_task = asyncio.create_task(pump_input())
            fields_task = asyncio.create_task(poll_fields())
            report_task = asyncio.create_task(report_loop())
            login_task = asyncio.create_task(watch_login())
            try:
                waiting = {nav_task, input_task, login_task}
                while True:
                    finished, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                    if nav_task in finished:
                        nav_task.result()  # raises when the page could not be loaded
                        waiting.discard(nav_task)
                    if input_task in finished or login_task in finished:
                        break
                if login_task in finished:
                    cookies = login_task.result()
                    parent_id = self.links.consume(token)
                    if parent_id is None:  # the link ran out while the parent was logging in
                        abandoned("link_expired")
                        await self._tell(ws, {"t": "error", "why": "failed"})
                        return
                    self._log_summary(cookies, started, stats, rtts, tap_to_picture)
                    analytics.emit(
                        "login_captured",
                        parent_id,
                        route=protocol.login_route(cookies, tab=tab[0]),
                        login_seconds=round(time.monotonic() - started, 1),
                        cookie_hours=protocol.webtoken_lifetime_hours(cookies, now=time.time()),
                        taps=stats["taps"],
                        frames_dropped=stats["dropped"],
                    )
                    self._save_for_measuring(cookies)
                    mine = {c["name"]: c["value"] for c in self.cookie_filter(cookies)}
                    try:
                        await self.on_connected(parent_id, mine)
                    except Exception as e:
                        log.error("hand-over after login failed: %s", type(e).__name__)
                        analytics.emit_error("login", e)
                        await self._tell(ws, {"t": "error", "why": "failed"})
                        return
                    await self._tell(ws, {"t": "done"})
                else:
                    log.info("phone left before the login finished")
                    abandoned("left")
            finally:
                everything = [
                    nav_task,
                    input_task,
                    login_task,
                    fields_task,
                    report_task,
                    *route_tasks,
                ]
                for t in everything:
                    t.cancel()
                await asyncio.gather(*everything, return_exceptions=True)

    def _save_for_measuring(self, cookies: list[dict]) -> None:
        if self.save_dir is None:
            return
        import os
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        path = self.save_dir / f"session_{now:%Y%m%dT%H%M%SZ}.json"
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )  # a live session: owner only
        with os.fdopen(fd, "w") as f:
            json.dump(protocol.session_payload(cookies, saved_at=now.isoformat()), f)
        log.warning("DEV: a live session was saved to %s", path.name)

    def _log_summary(
        self,
        cookies: list[dict],
        started: float,
        stats: dict,
        rtts: list[float],
        tap_to_picture: list[float],
    ) -> None:
        log.info(
            "login captured: %s",
            json.dumps(
                {
                    "login_seconds": round(time.monotonic() - started, 1),
                    "webtoken_cookie_hours": protocol.webtoken_lifetime_hours(
                        cookies, now=time.time()
                    ),
                    "frames": stats["frames"],
                    "frames_dropped": stats["dropped"],
                    "tap_to_picture_ms": protocol.summarize_ms(tap_to_picture),
                    "avg_frame_kb": round(stats["bytes"] / max(stats["frames"], 1) / 1024, 1),
                    "first_frame_s": stats["first_frame_s"],
                    "taps": stats["taps"],
                    "text_messages": stats["text_msgs"],
                    "keys": stats["keys"],
                    "rtt_ms": protocol.summarize_ms(rtts),
                    "headless": self.headless,
                    "chrome_channel": self.chrome,
                }
            ),
        )
