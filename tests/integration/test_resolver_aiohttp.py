"""
P1 prototype: how SafeResolver behaves inside a real aiohttp client.

These tests double as *characterization tests* of aiohttp. Two of them pin
behaviour the design depends on (the connection goes to the resolver's address,
Host is preserved) and one pins a limitation the design works around (aiohttp
never consults the resolver for IP-literal URLs). If aiohttp changes any of
them, this file fails and the design must be revisited.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver

from seoprobe.core.net.safety import SafeResolver


class _MapResolver(AbstractResolver):
    """TEST ONLY: maps a name to 127.0.0.1 and records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[dict[str, Any]]:
        self.calls.append(host)
        if host != "pinned.test":
            raise OSError("unknown host")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        return None


@pytest.fixture
async def server() -> AsyncIterator[tuple[int, dict[str, str | None]]]:
    seen: dict[str, str | None] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["host"] = request.headers.get("Host")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield port, seen
    await runner.cleanup()


async def test_connection_goes_to_the_resolver_address_and_host_header_is_kept(
    server: tuple[int, dict[str, str | None]],
) -> None:
    port, seen = server
    resolver = _MapResolver()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver)) as s:
        async with s.get(f"http://pinned.test:{port}/") as r:
            assert r.status == 200
    assert resolver.calls == ["pinned.test"]
    assert seen["host"] == f"pinned.test:{port}"


async def test_ip_literal_urls_bypass_the_resolver_entirely(
    server: tuple[int, dict[str, str | None]],
) -> None:
    """LIMITATION: this is why every request and redirect hop needs check_url first."""
    port, _ = server
    resolver = _MapResolver()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver)) as s:
        async with s.get(f"http://127.0.0.1:{port}/") as r:
            assert r.status == 200
    assert resolver.calls == []


async def test_safe_resolver_blocks_a_hostname_that_resolves_to_loopback(
    server: tuple[int, dict[str, str | None]],
) -> None:
    port, seen = server

    async def lookup(host: str, p: int) -> list[str]:
        return ["127.0.0.1"]

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=SafeResolver(lookup))) as s:
        with pytest.raises(aiohttp.ClientConnectorError):
            await s.get(f"http://rebinder.example:{port}/")
    assert seen == {}  # the server never saw a request


async def test_safe_resolver_blocks_metadata_names_before_any_lookup() -> None:
    async def boom(host: str, p: int) -> list[str]:
        raise AssertionError("lookup must not run")

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=SafeResolver(boom))) as s:
        with pytest.raises(aiohttp.ClientConnectorError):
            await s.get("http://metadata.google.internal./latest/meta-data/")
