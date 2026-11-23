"""
Real Chromium renderer for the render tests. The URL policy is relaxed ONLY for ``*.localhost``
(test seam, as in tests/core/test_browser.py); every other target goes through the real policy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import pytest
from aiohttp import web

from seoprobe.core.net.browser import Renderer
from seoprobe.core.net.safety import Target, check_url


def _relaxed(url: str) -> Target:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.endswith(".localhost"):
        return Target(url, p.scheme, host, p.port or 80, False)
    return check_url(url)


async def _resolve(host: str) -> list[str]:
    return ["93.184.216.34"]


PageSpec = str | tuple[str, dict[str, str]]  # html, or (html, extra response headers)
Serve = Callable[[dict[str, PageSpec]], Awaitable[int]]


async def _serve(pages: dict[str, PageSpec]) -> tuple[web.AppRunner, int]:
    app = web.Application()

    def handler_for(spec: PageSpec):  # type: ignore[no-untyped-def]
        html, headers = (spec, {}) if isinstance(spec, str) else spec

        async def handler(r: web.Request) -> web.Response:
            return web.Response(text=html, content_type="text/html", headers=headers)

        return handler

    for path, spec in pages.items():
        app.router.add_get(path, handler_for(spec))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


@pytest.fixture
async def serve() -> AsyncIterator[Serve]:
    """``await serve({path: html})`` -> port of a local server (stopped after the test)."""
    runners: list[web.AppRunner] = []

    async def start(pages: dict[str, PageSpec]) -> int:
        runner, port = await _serve(pages)
        runners.append(runner)
        return port

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
async def renderer() -> AsyncIterator[Renderer]:
    r = Renderer(url_check=_relaxed, resolve=_resolve)
    await r.start()
    if r.launch_error is not None:
        pytest.skip(f"chromium unavailable: {r.launch_error}")
    yield r
    await r.close()


@pytest.fixture
async def renderer_prod() -> AsyncIterator[Renderer]:
    r = Renderer()
    yield r
    await r.close()
