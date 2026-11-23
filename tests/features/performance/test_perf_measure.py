"""
measure_page against local servers. The URL policy is relaxed ONLY for ``*.localhost``
(test seam, as in test_render_browser); every other target goes through the real policy.
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp import web

from seoprobe.core.net.browser import Renderer
from seoprobe.core.net.safety import Target, check_url
from seoprobe.features.performance.lab import measure_page
from seoprobe.features.performance.vitals import perf_checks

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "perf"
WEBP_1X1 = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


def make_png(w: int, h: int) -> bytes:
    """A valid PNG of incompressible pixels (stored deflate): size is ~ w*h*3 bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    rows = b"".join(b"\x00" + os.urandom(w * 3) for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 0))
        + chunk(b"IEND", b"")
    )


def _relaxed(url: str) -> Target:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.endswith(".localhost"):
        return Target(url, p.scheme, host, p.port or 80, False)
    return check_url(url)


async def _resolve(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def site() -> AsyncIterator[tuple[int, int]]:
    """(first-party port, third-party port). Hosts: app.localhost and cdn.localhost."""
    big_js = ("/* " + "x" * 60_000 + " */\nwindow.__big = 1;\n").encode()
    png = make_png(400, 300)
    lazy = make_png(200, 150)
    third = web.Application()

    async def tp(r: web.Request) -> web.Response:
        return web.Response(body=b"window.__tp=1;", content_type="application/javascript")

    third.router.add_get("/tp.js", tp)
    t_runner = web.AppRunner(third)
    await t_runner.setup()
    t_site = web.TCPSite(t_runner, "127.0.0.1", 0)
    await t_site.start()
    t_port = t_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    app = web.Application()

    async def page(r: web.Request) -> web.Response:
        html = (GOLDEN / "page.html").read_text().replace("__CDN__", f"http://cdn.localhost:{t_port}")
        return web.Response(text=html, content_type="text/html")

    async def mobile(r: web.Request) -> web.Response:
        return web.Response(text=(GOLDEN / "mobile.html").read_text(), content_type="text/html")

    async def mobile_nometa(r: web.Request) -> web.Response:
        html = (
            (GOLDEN / "mobile.html")
            .read_text()
            .replace('<meta name="viewport" content="width=device-width, initial-scale=1">', "")
        )
        return web.Response(text=html, content_type="text/html")

    async def js(r: web.Request) -> web.Response:
        return web.Response(body=big_js, content_type="application/javascript")

    async def css(r: web.Request) -> web.Response:
        resp = web.Response(body=(".a{color:red}\n" * 400).encode(), content_type="text/css")
        resp.enable_compression()
        resp.headers["Cache-Control"] = "public, max-age=31536000"
        return resp

    def serve(body: bytes, ct: str):  # type: ignore[no-untyped-def]
        async def handler(r: web.Request) -> web.Response:
            return web.Response(body=body, content_type=ct)

        return handler

    app.router.add_get("/page", page)
    app.router.add_get("/mobile", mobile)
    app.router.add_get("/mobile-nometa", mobile_nometa)
    app.router.add_get("/big.js", js)
    app.router.add_get("/small.css", css)
    app.router.add_get("/big.png", serve(png, "image/png"))
    app.router.add_get("/lazy.png", serve(lazy, "image/png"))
    app.router.add_get("/photo.jpg", serve(WEBP_1X1, "image/jpeg"))
    runner = web.AppRunner(app)
    await runner.setup()
    s = web.TCPSite(runner, "127.0.0.1", 0)
    await s.start()
    port = s._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield port, t_port
    await runner.cleanup()
    await t_runner.cleanup()


@pytest.fixture
async def renderer() -> AsyncIterator[Renderer]:
    r = Renderer(url_check=_relaxed, resolve=_resolve)
    await r.start()
    if r._browser is None:
        pytest.skip(f"chromium unavailable: {r.launch_error}")
    yield r
    await r.close()


async def test_desktop_measurement_reports_timing_weight_resources_and_images(
    renderer: Renderer, site: tuple[int, int]
) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/page")
    assert p.error is None and p.device == "desktop"
    # timing
    assert p.ttfb_ms is not None and p.ttfb_ms >= 0
    assert p.load_ms and p.dom_content_loaded_ms
    assert p.lcp_ms is not None and p.lcp_ms > 0
    assert p.cls is not None and p.cls >= 0 and p.tbt_ms is not None
    # weight
    assert p.total_requests >= 7 and p.html_bytes > 0
    assert p.total_bytes > 350_000  # the 400x300 incompressible PNG alone
    assert {"document", "script", "stylesheet", "image"} <= set(p.by_type)
    assert p.by_type["script"]["count"] == 2 and p.by_type["image"]["count"] >= 3
    # third party: the second host is another "site"
    assert [t["host"] for t in p.third_party] == ["cdn.localhost"]
    assert p.third_party[0]["requests"] == 1
    # compression and cache (first-party only)
    assert any(u.endswith("/big.js") for u in p.uncompressed_text)
    assert not any(u.endswith("/small.css") for u in p.uncompressed_text)  # gzip
    assert any(u.endswith("/big.js") for u in p.no_cache_static)
    assert not any(u.endswith("/small.css") for u in p.no_cache_static)  # long max-age
    assert not any("cdn.localhost" in u for u in p.uncompressed_text + p.no_cache_static)
    assert p.mobile is None


async def test_images_are_classified_by_served_format_size_and_display(
    renderer: Renderer, site: tuple[int, int]
) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/page")
    by = {i.src.rsplit("/", 1)[-1]: i for i in p.images}
    big = by["big.png"]
    assert big.served_format == "png" and big.bytes and big.bytes > 350_000
    assert big.category == "thumbnail" and big.displayed_w == 100 and big.natural_w == 400
    assert {"image-critically-large", "image-legacy-format", "image-oversized-for-display"} <= set(big.issues)
    photo = by["photo.jpg"]
    assert photo.served_format == "webp" and photo.issues == []  # served WebP behind a .jpg URL
    lazy = by["lazy.png"]  # below the fold: only loaded because the measurement scrolls
    assert lazy.lazy and not lazy.above_fold and lazy.bytes and lazy.bytes > 50_000
    assert big.above_fold and big.has_alt


async def test_findings_from_a_real_measurement(renderer: Renderer, site: tuple[int, int]) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/page")
    ids = {f.id for f in perf_checks([p]).findings}
    assert {"uncompressed-text-resources", "image-critically-large", "image-legacy-format"} <= ids


async def test_mobile_run_reports_overflow_small_text_and_tap_targets(
    renderer: Renderer, site: tuple[int, int]
) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/mobile", device="mobile")
    assert p.error is None and p.device == "mobile" and p.mobile is not None
    m = p.mobile
    assert m["horizontal_overflow"] is True and m["viewport_meta"] is True
    assert m["small_font_share"] > 0.5
    assert m["small_tap_targets"] == 4 and len(m["tap_target_examples"]) == 3
    ids = {f.id for f in perf_checks([p]).findings}
    assert {"mobile-horizontal-scroll", "mobile-font-too-small", "mobile-tap-targets-small"} <= ids


async def test_overflow_is_measured_against_the_layout_viewport_even_without_a_viewport_meta(
    renderer: Renderer, site: tuple[int, int]
) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/mobile-nometa", device="mobile")
    assert p.mobile is not None and p.mobile["viewport_meta"] is False
    assert p.mobile["horizontal_overflow"] is True  # 1200 px content in a 980 px layout viewport


async def test_the_mobile_run_is_phone_shaped(renderer: Renderer, site: tuple[int, int]) -> None:
    port, _ = site
    p = await measure_page(renderer, f"http://app.localhost:{port}/page", device="mobile")
    assert p.error is None and p.mobile is not None and p.mobile["horizontal_overflow"] is False
    big = next(i for i in p.images if i.src.endswith("big.png"))
    assert big.displayed_w == 100  # CSS pixels, whatever the device pixel ratio
    assert "image-oversized-for-display" not in big.issues  # 400 px for a 100 px slot is fine at 3x


async def test_production_policy_rejects_private_targets_without_a_browser(
    site: tuple[int, int],
) -> None:
    port, tport = site
    r = Renderer()  # real policy; the browser is never started
    results = [
        await measure_page(r, f"http://127.0.0.1:{port}/page"),
        await measure_page(r, f"http://localhost:{tport}/tp.js"),
        await measure_page(r, "http://169.254.169.254/latest/meta-data/", device="mobile"),
        await measure_page(r, "http://[::1]:80/"),
    ]
    assert all((x.error or "").startswith("target-rejected") for x in results)
    assert r._browser is None and r._pw is None


async def test_unreachable_host_returns_an_error_instead_of_raising(renderer: Renderer) -> None:
    p = await measure_page(renderer, "http://closed.localhost:1/", timeout_ms=8000)
    assert p.error is not None and p.error.startswith("render-failed")
    assert perf_checks([p]).findings == []


async def test_sub_resources_to_private_ips_are_aborted_during_measurement(
    site: tuple[int, int],
) -> None:
    """A measured page that tries to reach a private IP never gets there (route guard intact)."""
    hits = {"n": 0}
    secret = web.Application()

    async def s(r: web.Request) -> web.Response:
        hits["n"] += 1
        return web.Response(text="x", headers={"Access-Control-Allow-Origin": "*"})

    secret.router.add_get("/secret", s)
    runner = web.AppRunner(secret)
    await runner.setup()
    st = web.TCPSite(runner, "127.0.0.1", 0)
    await st.start()
    sport = st._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    app = web.Application()

    async def evil(r: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text=f"<html><body><p>hi</p><img src='http://127.0.0.1:{sport}/secret'>"
            f"<script>fetch('http://127.0.0.1:{sport}/secret').catch(()=>{{}})</script></body></html>",
        )

    app.router.add_get("/evil", evil)
    a_runner = web.AppRunner(app)
    await a_runner.setup()
    a_site = web.TCPSite(a_runner, "127.0.0.1", 0)
    await a_site.start()
    aport = a_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    r = Renderer(url_check=_relaxed, resolve=_resolve)
    await r.start()
    if r._browser is None:
        await runner.cleanup()
        await a_runner.cleanup()
        pytest.skip("chromium unavailable")
    p = await measure_page(r, f"http://app.localhost:{aport}/evil")
    await r.close()
    await runner.cleanup()
    await a_runner.cleanup()
    assert p.error is None and hits["n"] == 0
