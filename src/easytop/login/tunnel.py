"""A free Cloudflare quick tunnel (`cloudflared`) giving the local login server a public HTTPS address.

For development on a Mac (Telegram Mini Apps need https). The address changes on every start. Production
uses a fixed public address instead (Cloud Run), see `LoginRuntime(public_url=...)`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from urllib.parse import urlsplit

import httpx

from easytop.errors import EasytopError
from easytop.login.protocol import find_tunnel_url

log = logging.getLogger("easytop.login")


class TunnelError(EasytopError):
    pass


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(4096):  # a full pipe would block cloudflared
        pass


async def start_cloudflared(port: int) -> tuple[asyncio.subprocess.Process, str]:
    if shutil.which("cloudflared") is None:
        raise TunnelError("cloudflared not found. Install it with: brew install cloudflared")
    proc = await asyncio.create_subprocess_exec(
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    url = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), 5)
        except TimeoutError:
            continue
        if not line:
            break
        text = line.decode(errors="replace")
        url = url or find_tunnel_url(text)
        # Do not look the new name up before it is registered: the resolver would cache the failure
        # (NXDOMAIN) for minutes and every later try would fail too.
        if url and "Registered tunnel connection" in text:
            asyncio.create_task(_drain(proc.stderr))
            return proc, url
    proc.terminate()
    raise TunnelError("cloudflared did not register a tunnel in 60 s")


async def wait_until_reachable(url: str) -> None:
    """A fresh quick tunnel's name takes a while to be published. Asking the local resolver early caches the
    failure for minutes, so first wait for the name on Cloudflare's public DNS-over-HTTPS, then try the URL.
    Any answer below 500 means the tunnel reaches our server (the login server answers 403 without a link)."""
    host = urlsplit(url).hostname
    async with httpx.AsyncClient(timeout=8) as client:
        for _ in range(45):
            try:
                r = await client.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                )
                if r.json().get("Answer"):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(2)
        else:
            raise TunnelError("the tunnel name was never published in DNS")
        for _ in range(30):
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
    raise TunnelError("the tunnel address never became reachable")


class CloudflaredTunnel:
    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self, port: int) -> str:
        self._proc, url = await start_cloudflared(port)
        await wait_until_reachable(url)
        log.info("login tunnel up: %s", urlsplit(url).netloc)
        return url

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()
        self._proc = None
