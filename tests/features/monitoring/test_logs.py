"""
analyze_logs: parsing, bot verification on a fake resolver, findings, and a 1 000 000-line log
in bounded memory with a responsive event loop.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.features.monitoring import analyze_logs
from seoprobe.features.monitoring.logs import bot_catalogue, identify_bot, parse_line
from tests import fakes

GOOGLE_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
BING_UA = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"

# reverse name and forward addresses of the fake resolver
PTR = {
    "66.249.66.1": ("crawl-66-249-66-1.googlebot.com", ["66.249.66.1"]),
    "66.249.66.2": ("crawl-66-249-66-2.googlebot.com", ["66.249.66.2"]),
    "157.55.39.1": ("msnbot-157-55-39-1.search.msn.com.", ["157.55.39.1"]),
    "1.2.3.4": ("host.evil.example", ["1.2.3.4"]),  # wrong domain
    "5.6.7.8": ("crawl.googlebot.com.evil.example", ["5.6.7.8"]),  # suffix trick
    "9.9.9.9": ("crawl-1.googlebot.com", ["66.249.66.1"]),  # forward lookup disagrees
}


def line(ip: str, path: str, status: int = 200, ua: str = GOOGLE_UA, method: str = "GET") -> str:
    return f'{ip} - - [10/Oct/2026:13:55:36 +0000] "{method} {path} HTTP/1.1" {status} 512 "-" "{ua}"'


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replaces the event loop resolver; counts lookups per IP. No real DNS is ever queried."""
    calls: dict[str, int] = {}

    async def getnameinfo(self: Any, sockaddr: tuple[str, int], flags: int = 0) -> tuple[str, str]:
        ip = sockaddr[0]
        calls[ip] = calls.get(ip, 0) + 1
        if ip not in PTR:
            raise socket.gaierror(socket.EAI_NONAME, "no name")
        return PTR[ip][0], "0"

    async def getaddrinfo(self: Any, host: str, port: Any, **kw: Any) -> list[Any]:
        for name, addrs in PTR.values():
            if name.rstrip(".") == host:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addrs]
        raise socket.gaierror(socket.EAI_NONAME, "no name")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getnameinfo", getnameinfo)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return calls


async def _run(lines: Any, **kw: Any) -> Any:
    async with fakes.session({}, config=kw.pop("config", None)) as s:
        return await analyze_logs(lines, session=s, **kw)


# ----------------------------------------------------------------------------- parsing


def test_combined_common_and_json_lines() -> None:
    r = parse_line(line("66.249.66.1", "/a?b=1", 301), "combined")
    assert r is not None and (r.ip, r.path, r.status, r.user_agent) == ("66.249.66.1", "/a?b=1", 301, GOOGLE_UA)
    common = '1.1.1.1 - frank [10/Oct/2026:13:55:36 -0700] "GET /x HTTP/1.0" 404 -'
    r = parse_line(common, "common")
    assert r is not None and (r.path, r.status, r.user_agent) == ("/x", 404, "")
    assert parse_line(common, "combined") is None  # no referer / user agent
    absolute = line("1.1.1.1", "https://example.com/abs?q=1")
    assert parse_line(absolute, "combined").path == "/abs?q=1"  # type: ignore[union-attr]
    escaped = '1.1.1.1 - - [t] "GET / HTTP/1.1" 200 1 "-" "Bot \\"quoted\\" Googlebot/2.1"'
    assert parse_line(escaped, "combined").user_agent == 'Bot "quoted" Googlebot/2.1'  # type: ignore[union-attr]
    js = json.dumps({"remote_addr": "1.1.1.1", "request": "GET /j HTTP/1.1", "status": "200", "http_user_agent": "x"})
    r = parse_line(js, "json")
    assert r is not None and (r.path, r.status, r.user_agent) == ("/j", 200, "x")
    r = parse_line(json.dumps({"ip": "1.1.1.1", "path": "/p", "status_code": 500}), "json")
    assert r is not None and r.status == 500
    for bad in ("", "garbage", '1.1.1.1 - - [t] "-" 400 0 "-" "-"', "[1, 2]", '{"ip": "1.1.1.1"}', "{bad json"):
        assert parse_line(bad, "json" if bad.startswith(("[", "{")) else "combined") is None


def test_bot_catalogue_and_identification() -> None:
    names = [b.name for b in bot_catalogue()]
    assert names[:2] == ["Googlebot", "Bingbot"] and "GPTBot" in names and "ClaudeBot" in names
    google = next(b for b in bot_catalogue() if b.name == "Googlebot")
    assert "googlebot.com" in google.domains and google.kind == "search"
    assert identify_bot(GOOGLE_UA).name == "Googlebot"  # type: ignore[union-attr]
    assert identify_bot("Googlebot-Image/1.0").name == "Googlebot"  # type: ignore[union-attr]
    assert identify_bot(BING_UA).name == "Bingbot"  # type: ignore[union-attr]
    gpt = identify_bot("Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)")
    assert gpt is not None and gpt.name == "GPTBot" and gpt.kind == "ai" and gpt.domains == ()
    assert identify_bot(BROWSER_UA) is None


# ----------------------------------------------------------------------------- analysis


async def test_per_bot_counts_verification_and_findings(resolver: dict[str, int]) -> None:
    lines = [
        line("66.249.66.1", "/a"),
        line("66.249.66.1", "/a"),
        line("66.249.66.2", "/b?sort=1"),
        line("66.249.66.2", "/old", 301),
        line("66.249.66.1", "/gone", 404),
        line("66.249.66.1", "/err", 503),
        line("1.2.3.4", "/a"),
        line("5.6.7.8", "/a"),
        line("9.9.9.9", "/a"),
        line("10.0.0.5", "/a"),  # private address: never a search engine, no lookup
        line("157.55.39.1", "/a", ua=BING_UA),
        line("20.1.1.1", "/a", ua="GPTBot/1.1"),
        line("8.8.8.8", "/", ua=BROWSER_UA),
        "garbage",
        "",
    ]
    res = await _run(
        lines, min_bot_requests=1, waste_share=0.3, urls=["https://example.com/a", "https://example.com/never"]
    )
    assert res.ok and res.data is not None
    d = res.data
    assert (d.lines_total, d.lines_parsed, d.lines_unparsable) == (15, 13, 1)
    assert d.bot_requests == 12 and d.other_requests == 1
    assert [b.name for b in d.bots] == ["Googlebot", "Bingbot", "GPTBot"]
    g = d.bots[0]
    assert g.requests == 10 and g.verified_requests == 6 and g.spoofed_requests == 4 and g.unverified_requests == 0
    assert g.spoofed_ips == ["1.2.3.4", "10.0.0.5", "5.6.7.8", "9.9.9.9"] and g.distinct_ips == 6
    assert g.status == {"200": 7, "301": 1, "404": 1, "503": 1}
    assert g.top_paths[0].path == "/a" and g.top_paths[0].requests == 6
    assert (g.wasted.parameterised, g.wasted.redirect, g.wasted.client_error, g.wasted.total) == (1, 1, 1, 3)
    assert g.wasted.share == 0.3
    bing, gpt = d.bots[1], d.bots[2]
    assert bing.verified_requests == 1 and bing.verifiable
    assert not gpt.verifiable and gpt.unverified_requests == 1 and gpt.kind == "ai"
    assert "10.0.0.5" not in resolver and resolver["66.249.66.1"] == 1  # cached per IP
    assert d.uncrawled_urls == ["https://example.com/never"] and d.urls_checked == 2
    ids = [f.id for f in res.findings]
    assert ids == [
        "logs-spoofed-bot",
        "logs-crawl-waste",
        "logs-bot-server-errors",
        "logs-uncrawled-urls",
        "logs-unparsable-lines",
    ]
    waste = next(f for f in res.findings if f.id == "logs-crawl-waste")
    assert "threshold 30%" in (waste.evidence or "") and waste.count == 3
    assert res.findings[0].count == 4


async def test_a_page_crawled_only_by_spoofed_bots_counts_as_never_crawled(resolver: dict[str, int]) -> None:
    lines = [line("1.2.3.4", "/a"), line("66.249.66.1", "/b/")]
    res = await _run(lines, urls=["https://example.com/a", "https://example.com/b/", "https://example.com/b/"])
    assert res.data.uncrawled_urls == ["https://example.com/a"]  # type: ignore[union-attr]
    off = await _run(lines, urls=["https://example.com/a"], verify_bots=False)
    assert off.data.uncrawled_urls == [] and resolver.get("1.2.3.4") == 1  # type: ignore[union-attr]
    assert off.data.bots[0].unverified_requests == 2 and off.data.bots[0].spoofed_requests == 0  # type: ignore[union-attr]


async def test_the_verification_cap_takes_the_most_active_ips_first(resolver: dict[str, int]) -> None:
    lines = [line("66.249.66.1", "/")] * 3 + [line("66.249.66.2", "/")] * 2 + [line("1.2.3.4", "/")]
    res = await _run(lines, max_verify_ips=2)
    d = res.data
    assert d is not None and d.ips_verified == 2 and d.ips_not_verified == 1
    assert set(resolver) == {"66.249.66.1", "66.249.66.2"}
    assert d.bots[0].verified_requests == 5 and d.bots[0].unverified_requests == 1
    assert res.status == "partial" and [n.reason_code for n in res.not_checked] == ["verification-cap"]


async def test_a_resolver_timeout_leaves_the_ip_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(self: Any, sockaddr: Any, flags: int = 0) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getnameinfo", slow)
    from seoprobe.features.monitoring import logs

    defaults = dict(logs._defaults()["analyze_logs"], dns_timeout_s=0.05)
    monkeypatch.setattr(logs, "_defaults", lambda: {"analyze_logs": defaults})
    res = await _run([line("66.249.66.1", "/")])
    b = res.data.bots[0]  # type: ignore[union-attr]
    assert b.unverified_requests == 1 and b.verified_requests == 0 and b.spoofed_requests == 0


async def test_async_iterables_and_json_format(resolver: dict[str, int]) -> None:
    async def stream() -> AsyncIterator[str]:
        for i in range(12_001):  # more than two chunks
            yield json.dumps({"client_ip": "66.249.66.1", "uri": f"/p{i % 3}", "status": 200, "user_agent": GOOGLE_UA})

    res = await _run(stream(), format="json")
    d = res.data
    assert d is not None and d.lines_total == 12_001 and d.lines_parsed == 12_001
    assert d.bots[0].verified_requests == 12_001
    assert [(p.path, p.requests) for p in d.bots[0].top_paths] == [("/p0", 4001), ("/p1", 4000), ("/p2", 4000)]


async def test_output_is_deterministic_and_json(resolver: dict[str, int]) -> None:
    lines = [line("66.249.66.1", f"/{i % 5}") for i in range(50)] + [line("157.55.39.1", "/x", ua=BING_UA)] * 50
    a, b = await _run(lines), await _run(list(reversed(lines)))
    da, db = a.data.model_dump(), b.data.model_dump()  # type: ignore[union-attr]
    assert da == db
    assert [x["name"] for x in da["bots"]] == ["Bingbot", "Googlebot"]  # tie broken by name
    assert json.loads(a.to_json())["data"]["bots"][1]["top_paths"][0] == {"path": "/0", "requests": 10}


async def test_caps_bound_paths_and_ips(resolver: dict[str, int]) -> None:
    from seoprobe.features.monitoring import logs

    lines = [line(f"66.249.{i // 256 % 256}.{i % 256}", f"/u/{i}") for i in range(300)]
    defaults = dict(logs._defaults()["analyze_logs"], max_paths_per_bot=100, max_tracked_ips=50, top_paths=3)
    orig = logs._defaults
    try:
        logs._defaults = lambda: {"analyze_logs": defaults}  # type: ignore[assignment]
        res = await _run(lines, verify_bots=False)
    finally:
        logs._defaults = orig  # type: ignore[assignment]
    b = res.data.bots[0]  # type: ignore[union-attr]
    assert b.requests == 300 and b.paths_truncated and len(b.top_paths) == 3 and b.distinct_ips == 50
    assert res.data.ips_truncated  # type: ignore[union-attr]


async def test_watched_urls_respect_the_batch_limit(resolver: dict[str, int]) -> None:
    urls = [f"https://example.com/{i}" for i in range(5)]
    res = await _run([line("66.249.66.1", "/0")], urls=urls, config=Config(max_batch_urls=3))
    d = res.data
    assert d is not None and d.urls_checked == 3 and d.urls_skipped == 2
    assert d.uncrawled_urls == ["https://example.com/1", "https://example.com/2"]


async def test_low_volume_bots_raise_no_ratio_findings(resolver: dict[str, int]) -> None:
    res = await _run([line("66.249.66.1", "/x?y=1", 503)])
    assert [f.id for f in res.findings] == []  # under min_bot_requests


# ----------------------------------------------------------------------------- scale


def _big_log(n: int) -> Iterator[str]:
    """
    A generated combined log: verified and spoofed Googlebot, Bingbot, GPTBot, browsers and
    garbage; every Googlebot path is distinct so the path cap is exercised.
    """
    for i in range(n):
        k = i % 10
        if k == 0:
            yield line(f"66.249.66.{1 + i % 2}", f"/p/{i}")
        elif k == 1:
            yield line("1.2.3.4", f"/p?x={i % 7}")
        elif k == 2:
            yield line("157.55.39.1", "/old", 301, BING_UA)
        elif k == 3:
            yield line("20.15.240.1", "/missing", 404, "GPTBot/1.1")
        elif k == 4:
            yield "garbage line"
        else:
            yield line(f"10.0.{i % 200}.1", "/", 200, BROWSER_UA)


async def test_a_million_lines_in_bounded_memory_with_a_responsive_loop(resolver: dict[str, int]) -> None:
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tracemalloc.start()
    try:
        tick = asyncio.create_task(ticker())
        res = await _run(_big_log(1_000_000))
        done.set()
        await tick
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    d = res.data
    assert d is not None and d.lines_total == 1_000_000 and d.lines_unparsable == 100_000
    assert d.bot_requests == 400_000 and d.other_requests == 500_000
    g = next(b for b in d.bots if b.name == "Googlebot")
    assert g.verified_requests == 100_000 and g.spoofed_requests == 100_000 and g.paths_truncated
    assert peak < 6 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"
    assert len(gaps) > 50 and max(gaps) < 0.1, f"event loop blocked for {max(gaps):.3f}s"
