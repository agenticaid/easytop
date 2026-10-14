"""Read-only HTTP client for webtopserver.smartschool.co.il.

Only whitelisted parent read endpoints can be called, plus the context switch (switch_context).
Auth is the `webToken` cookie, which is per school/role context: see with_token().
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from easytop.connector.parse import SwitchedUser, parse_change_user
from easytop.errors import EndpointNotAllowed, FileUnavailable, SessionExpired, UpstreamError
from easytop.files import MAX_FILE_BYTES, is_download_url

API_BASE = "https://webtopserver.smartschool.co.il/server/api"
WEB_ORIGIN = "https://webtop.smartschool.co.il"
TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)  # a file is read in pieces, so this bounds each wait

# Seen in the first real recording (2026-10-01): the web app sends an EMPTY x-xsrf-token header plus
# language/rememberme on every call; auth is the `webToken` cookie. Needs spike verification that the
# empty header is really accepted (the recorded session was made by a browser, not by this client).
WEBTOP_HEADERS = {"x-xsrf-token": "", "language": "he", "rememberme": "0"}

# Parent-role read endpoints seen in the real recordings (phase 1 spike, 2026-10-01). Read-only calls
# only. messageBox/GetMessagesInboxData is a read that still MARKS THE MESSAGE READ in Webtop (observed:
# unread counter 16 -> 12 after opening 4 messages); WebtopConnector only calls it for read messages
# unless told otherwise. user/ChangeUser is deliberately absent: see WebtopHttpClient.switch_context.
READ_ENDPOINTS = frozenset(
    {
        "user/GetMultipleUsersForUser",
        "dashboard/InitDashboard",
        "dashboard/CheckToken",
        "PupilCard/GetSettingsList",
        "PupilCard/GetPupilGrades",
        "PupilCard/GetPupilDiciplineEvents",
        "PupilCard/GetPupilLessonsAndHomework",  # which subject/study group is taught when
        "PupilCard/GetPreviousHomeworks",  # one subject's lesson history: class topic + homework
        "WeeklyScedule/GetEvents",
        "WeeklyScedule/InitData",  # hours, vacations (school off days) and inactive weekdays
        "messageBox/GetMessagesInbox",
        "messageBox/GetMessagesInboxData",
    }
)
# Switches the acting school/role of the login. It changes which context the token belongs to, not any
# school data; it is the only non-whitelisted call and only switch_context() can make it.
CONTEXT_SWITCH_ENDPOINT = "user/ChangeUser"

ExpiredHook = Callable[[], Awaitable[None] | None]


def looks_expired(response: httpx.Response) -> bool:
    """Expiry detection. Observed 2026-10-01 with NO cookies: data endpoints answer 401 with an empty
    body (covered here); dashboard/CheckToken answers 200 {status: true, data: false} (probe only).
    Still unverified for a session that EXPIRED (vs never existed): first real expiry will tell."""
    if response.status_code in (401, 403) or 300 <= response.status_code < 400:
        return True
    ctype = response.headers.get("content-type", "")
    return (
        response.status_code == 200
        and "json" not in ctype
        and "<html" in response.text[:200].lower()
    )


class WebtopHttpClient:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        allowed: frozenset[str] = READ_ENDPOINTS,
        on_expired: ExpiredHook | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 1,
    ):
        self._cookies = dict(cookies)
        self._allowed = allowed
        self._on_expired = on_expired
        self._transport = transport
        self._retries = retries
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={
                "Origin": WEB_ORIGIN,
                "Referer": WEB_ORIGIN + "/",
                **WEBTOP_HEADERS,
                "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def with_token(self, token: str) -> WebtopHttpClient:
        """A sibling client for another school/role context: same session cookies, its own webToken.

        The token is returned by user/ChangeUser in the response body; no API response sets a cookie
        (seen in the recordings), so the client has to set it itself.
        """
        return WebtopHttpClient(
            {**self._cookies, "webToken": token},
            allowed=self._allowed,
            transport=self._transport,
            retries=self._retries,
        )

    async def switch_context(self, student_id: str) -> SwitchedUser:
        """user/ChangeUser with the body the web app sends. Returns the new context's token."""
        body = {"StudentId": student_id, "institutionCode": None, "userType": None}
        return parse_change_user(await self._post(CONTEXT_SWITCH_ENDPOINT, body))

    async def call(self, endpoint: str, body: dict[str, Any] | None = None) -> Any:
        if endpoint not in self._allowed:
            raise EndpointNotAllowed(f"endpoint not whitelisted: {endpoint}")
        return await self._post(endpoint, body)

    async def _fire_expired(self) -> None:
        if self._on_expired:
            res = self._on_expired()
            if isinstance(res, Awaitable):
                await res

    async def download(self, url: str, *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        """GET one attachment into memory. Only Webtop's own file address is accepted (`is_download_url`); the
        address comes from a message the parent's session just opened, never from the model. Reading stops
        as soon as `max_bytes` is passed. The address holds a token: it is never put in an error message."""
        if not is_download_url(url):
            raise EndpointNotAllowed("download refused: not Webtop's file address")
        last: Exception | None = None
        for _attempt in range(self._retries + 1):
            try:
                async with self._client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as r:
                    if r.status_code in (401, 403) or 300 <= r.status_code < 400:
                        await self._fire_expired()
                        raise SessionExpired("session expired downloading a file")
                    if r.status_code >= 500:
                        last = UpstreamError(f"HTTP {r.status_code} downloading a file")
                        continue
                    if r.status_code != 200:
                        raise UpstreamError(f"HTTP {r.status_code} downloading a file")
                    if int(r.headers.get("content-length") or 0) > max_bytes:
                        raise FileUnavailable("too_big")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise FileUnavailable("too_big")
            except httpx.HTTPError as e:
                last = e
                continue
            data = bytes(buf)
            if data[:200].lstrip().lower().startswith((b"<html", b"<!doctype html")):
                await self._fire_expired()  # a login page instead of the file
                raise SessionExpired("session expired downloading a file")
            return data
        raise UpstreamError(f"file download failed after retry: {type(last).__name__}") from None

    async def _post(self, endpoint: str, body: dict[str, Any] | None) -> Any:
        last: Exception | None = None
        for _attempt in range(self._retries + 1):
            try:
                r = await self._client.post(f"{API_BASE}/{endpoint}", json=body or {})
            except httpx.TimeoutException as e:
                last = e
                continue
            except httpx.HTTPError as e:
                last = e
                continue
            if looks_expired(r):
                if self._on_expired:
                    res = self._on_expired()
                    if isinstance(res, Awaitable):
                        await res
                raise SessionExpired(f"session expired calling {endpoint}")
            if r.status_code >= 500:
                last = UpstreamError(f"HTTP {r.status_code} from {endpoint}")
                continue
            if r.status_code != 200:
                raise UpstreamError(f"HTTP {r.status_code} from {endpoint}")
            try:
                return r.json()
            except ValueError as e:
                raise UpstreamError(f"non-JSON answer from {endpoint}") from e
        raise UpstreamError(f"{endpoint} failed after retry: {last}") from last
