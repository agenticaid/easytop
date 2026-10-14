"""Starts the login server (and, in development, its tunnel) next to the bot, and knows its public address."""

from __future__ import annotations

import logging
from typing import Protocol

from easytop.login.server import LoginServer, OnConnected, ViewerCheck
from easytop.onboarding.links import ConnectLinks

log = logging.getLogger("easytop.login")


class Tunnel(Protocol):
    async def start(self, port: int) -> str: ...

    async def stop(self) -> None: ...


class LoginRuntime:
    def __init__(
        self,
        links: ConnectLinks,
        on_connected: OnConnected,
        *,
        tunnel: Tunnel | None,
        host: str = "127.0.0.1",
        port: int = 8765,
        public_url: str | None = None,
        viewer_check: ViewerCheck | None = None,
        **server_options,
    ):
        """`tunnel` gives a changing development address; `public_url` is a fixed one (deployment). With
        neither, the page is only reachable locally and `base_url` stays None."""
        self.host, self.port = host, port
        self._tunnel, self._public_url = tunnel, public_url
        self._server = LoginServer(links, on_connected, viewer_check=viewer_check, **server_options)
        self._ws_server = None
        self._base_url: str | None = None

    @property
    def base_url(self) -> str | None:
        return self._base_url

    async def start(self) -> None:
        self._ws_server = await self._server.serve(self.host, self.port)
        self.port = self._ws_server.sockets[0].getsockname()[1]
        if self._public_url:
            self._base_url = self._public_url.rstrip("/")
        elif self._tunnel is not None:
            self._base_url = await self._tunnel.start(self.port)
        await self._server.warm()  # the first parent finds Webtop already loaded
        log.info("login server on port %d, public=%s", self.port, bool(self._base_url))

    async def stop(self) -> None:
        self._base_url = None
        if self._tunnel is not None:
            await self._tunnel.stop()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        await self._server.aclose()
