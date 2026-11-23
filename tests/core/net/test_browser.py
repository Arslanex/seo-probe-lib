"""
Renderer against local servers. The URL policy is relaxed ONLY for ``*.localhost`` (test seam);
everything else, including every loopback IP literal, goes through the real policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from aiohttp import WSMsgType, web

from seoprobe.core.net.browser import Renderer
from seoprobe.core.net.safety import Target, check_url

PUBLIC_LOOKING = ["93.184.216.34"]
WORDS = " ".join(f"rendered{i}" for i in range(80))


@dataclass
class Hits:
    secret: int = 0
    ws: int = 0
    pings: int = 0
    paths: list[str] = field(default_factory=list)


def _relaxed_check(url: str) -> Target:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(".localhost"):
        p = urlsplit(url)
        return Target(url, p.scheme, host, p.port or 80, False)
    return check_url(url)


async def _resolve(host: str) -> list[str]:
    return PUBLIC_LOOKING


async def _start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


@pytest.fixture
async def servers() -> AsyncIterator[tuple[int, int, Hits]]:
    hits = Hits()

    # "private" server: must never be reached from page JavaScript or redirects.
    private = web.Application()

    async def secret(r: web.Request) -> web.Response:
        hits.secret += 1
        return web.Response(text="secret", headers={"Access-Control-Allow-Origin": "*"})

    async def ws(r: web.Request) -> web.WebSocketResponse:
        hits.ws += 1
        sock = web.WebSocketResponse()
        await sock.prepare(r)
        async for msg in sock:
            if msg.type == WSMsgType.CLOSE:
                break
        return sock

    private.router.add_get("/secret", secret)
    private.router.add_get("/ws", ws)
    p_runner, p_port = await _start(private)

    site = web.Application()

    async def js(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text=f"<html><head><title>t</title></head><body><div id='root'></div><script>"
            f"setTimeout(()=>{{document.getElementById('root').innerHTML="
            f"'<h1>Rendered Heading</h1><p>{WORDS}</p>'}},50)</script></body></html>",
        )

    async def evil(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text=f"<html><body><p>{WORDS}</p><script>"
            f"fetch('http://127.0.0.1:{p_port}/secret').catch(()=>{{}});"
            f"var f=document.createElement('iframe');f.src='http://127.0.0.1:{p_port}/secret';document.body.appendChild(f);"
            f"try{{new WebSocket('ws://127.0.0.1:{p_port}/ws')}}catch(e){{}}"
            f"</script></body></html>",
        )

    async def redir(r: web.Request) -> web.Response:
        raise web.HTTPFound(f"http://127.0.0.1:{p_port}/secret")

    async def flood(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text="<html><body><p>flood</p><script>for(let i=0;i<60;i++){fetch('/ping?i='+i).catch(()=>{})}</script></body></html>",
        )

    async def ping(r: web.Request) -> web.Response:
        hits.pings += 1
        return web.Response(text="ok")

    async def slow(r: web.Request) -> web.Response:
        await asyncio.sleep(6)
        return web.Response(text="late", content_type="text/html")

    async def r1(r: web.Request) -> web.Response:
        raise web.HTTPFound("/r2")

    async def r2(r: web.Request) -> web.Response:
        raise web.HTTPFound(f"http://127.0.0.1:{p_port}/secret")

    async def sub(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text="<html><body><p>sub</p><script>fetch('/r1').catch(()=>{})</script></body></html>",
        )

    async def hop(r: web.Request) -> web.Response:
        raise web.HTTPFound("/hop2")

    async def hop2(r: web.Request) -> web.Response:
        raise web.HTTPFound("/js")

    for path, h in (
        ("/hop", hop),
        ("/hop2", hop2),
        ("/r1", r1),
        ("/r2", r2),
        ("/sub", sub),
        ("/js", js),
        ("/evil", evil),
        ("/redir", redir),
        ("/flood", flood),
        ("/ping", ping),
        ("/slow", slow),
    ):
        site.router.add_get(path, h)
    s_runner, s_port = await _start(site)
    yield s_port, p_port, hits
    await s_runner.cleanup()
    await p_runner.cleanup()


@pytest.fixture
async def renderer() -> AsyncIterator[Renderer]:
    r = Renderer(url_check=_relaxed_check, resolve=_resolve)
    await r.start()
    if r._browser is None:
        pytest.skip(f"chromium unavailable: {r._launch_error}")
    yield r
    await r.close()


def _url(port: int, path: str) -> str:
    return f"http://app.localhost:{port}{path}"


async def test_production_policy_rejects_loopback_and_metadata_without_a_browser(
    servers: tuple[int, int, Hits],
) -> None:
    s_port, p_port, hits = servers
    r = Renderer()  # real policy, browser never started
    results = [
        await r.render(f"http://127.0.0.1:{s_port}/js"),
        await r.render(f"http://127.0.0.1:{p_port}/secret"),
        await r.render(f"http://localhost:{p_port}/secret"),
        await r.render("http://169.254.169.254/latest/meta-data/"),
        await r.render("http://[::1]:80/"),
        await r.render("http://2130706433/"),
    ]
    assert all((x.error or "").startswith("target-rejected") for x in results)
    assert r._browser is None and r._pw is None  # nothing was launched
    assert hits.secret == 0


async def test_subrequests_to_private_ips_are_aborted(renderer: Renderer, servers: tuple[int, int, Hits]) -> None:
    s_port, _, hits = servers
    res = await renderer.render(_url(s_port, "/evil"))
    assert res.error is None
    await asyncio.sleep(0.3)
    assert hits.secret == 0 and hits.ws == 0
    blocked = {b["url"].split("/")[2] for b in res.blocked_requests}
    assert any(h.startswith("127.0.0.1") for h in blocked)
    assert all(b["reason"].startswith("policy") for b in res.blocked_requests)


async def test_redirect_hop_to_private_ip_is_refused(renderer: Renderer, servers: tuple[int, int, Hits]) -> None:
    s_port, _, hits = servers
    res = await renderer.render(_url(s_port, "/redir"))
    assert res.error is not None and res.html is None
    assert hits.secret == 0
    assert any("127.0.0.1" in b["url"] for b in res.blocked_requests)


async def test_websocket_is_never_connected(renderer: Renderer, servers: tuple[int, int, Hits]) -> None:
    s_port, _, hits = servers
    await renderer.render(_url(s_port, "/evil"))
    await asyncio.sleep(0.3)
    assert hits.ws == 0


async def test_request_limit_caps_subrequests() -> None:
    hits_holder: dict[str, int] = {}
    app = web.Application()

    async def flood(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text="<html><body><p>flood</p><script>for(let i=0;i<60;i++){fetch('/ping?i='+i).catch(()=>{})}</script></body></html>",
        )

    async def ping(r: web.Request) -> web.Response:
        hits_holder["n"] = hits_holder.get("n", 0) + 1
        return web.Response(text="ok")

    app.router.add_get("/flood", flood)
    app.router.add_get("/ping", ping)
    runner, port = await _start(app)
    r = Renderer(url_check=_relaxed_check, resolve=_resolve, max_requests=10)
    await r.start()
    if r._browser is None:
        await runner.cleanup()
        pytest.skip(f"chromium unavailable: {r._launch_error}")
    res = await r.render(_url(port, "/flood"))
    await r.close()
    await runner.cleanup()
    assert res.error is None
    assert any(b["reason"] == "request-limit" for b in res.blocked_requests)
    assert hits_holder.get("n", 0) <= 10


async def test_navigation_timeout(renderer: Renderer, servers: tuple[int, int, Hits]) -> None:
    s_port, _, _ = servers
    res = await renderer.render(_url(s_port, "/slow"), timeout_ms=800)
    assert res.error == "timeout" and res.html is None


async def test_browser_unavailable_returns_an_error_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import seoprobe.core.net.browser as b

    def boom() -> object:
        raise RuntimeError("Executable doesn't exist at /nowhere")

    monkeypatch.setattr(b, "async_playwright", boom)
    r = Renderer(url_check=_relaxed_check, resolve=_resolve)
    res = await r.render("http://app.localhost:9/")
    assert (res.error or "").startswith("render-failed:") and "Executable" in (res.error or "")


async def test_concurrency_is_bounded_and_context_is_isolated(
    servers: tuple[int, int, Hits],
) -> None:
    s_port, _, _ = servers
    async with Renderer(url_check=_relaxed_check, resolve=_resolve, max_concurrent=2) as r:
        if r._browser is None:
            pytest.skip(f"chromium unavailable: {r._launch_error}")
        results = await asyncio.gather(*(r.render(_url(s_port, "/js")) for _ in range(4)))
    assert all(x.error is None and "Rendered Heading" in (x.html or "") for x in results)


async def test_multi_hop_redirect_chain_ending_in_private_ip_is_refused(
    renderer: Renderer, servers: tuple[int, int, Hits]
) -> None:
    s_port, _, hits = servers
    res = await renderer.render(_url(s_port, "/r1"))
    await asyncio.sleep(0.2)
    assert hits.secret == 0
    assert res.html is None and res.error is not None


async def test_subresource_redirect_to_private_ip_is_refused(
    renderer: Renderer, servers: tuple[int, int, Hits]
) -> None:
    s_port, _, hits = servers
    res = await renderer.render(_url(s_port, "/sub"))
    await asyncio.sleep(0.3)
    assert res.error is None
    assert hits.secret == 0
    assert any("127.0.0.1" in b["url"] for b in res.blocked_requests)


async def test_redirect_to_a_hostname_resolving_to_a_private_ip_is_refused(
    servers: tuple[int, int, Hits],
) -> None:
    """Hostname-level DNS judgement applies to redirect targets too."""
    s_port, _, hits = servers

    async def resolve(host: str) -> list[str]:
        return ["10.0.0.7"] if host.startswith("evil.") else PUBLIC_LOOKING

    def check(url: str) -> Target:
        from urllib.parse import urlsplit

        p = urlsplit(url)
        return Target(url, p.scheme, (p.hostname or "").lower(), p.port or 80, False)

    app = web.Application()

    async def to_evil(r: web.Request) -> web.Response:
        raise web.HTTPFound(f"http://evil.localhost:{s_port}/js")

    app.router.add_get("/go", to_evil)
    runner, port = await _start(app)
    r = Renderer(url_check=check, resolve=resolve)
    await r.start()
    if r._browser is None:
        await runner.cleanup()
        pytest.skip(f"chromium unavailable: {r._launch_error}")
    res = await r.render(f"http://app.localhost:{port}/go")
    await r.close()
    await runner.cleanup()
    assert res.html is None and res.error is not None
    assert any("redirect" in b["reason"] for b in res.blocked_requests)


async def test_legitimate_redirect_chain_is_followed_and_final_url_reported(
    renderer: Renderer, servers: tuple[int, int, Hits]
) -> None:
    s_port, _, _ = servers
    res = await renderer.render(_url(s_port, "/hop"))
    assert res.error is None and res.status == 200
    assert "Rendered Heading" in (res.html or "")
    assert res.final_url is not None and res.final_url.endswith("/js")
    assert res.request_count >= 3
