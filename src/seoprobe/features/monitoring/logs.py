"""
Crawl analysis of web server access logs, in constant memory.

Lines are read as a stream and processed in fixed-size chunks in a worker thread, so a log of
any length never sits in memory and the event loop keeps running. Every counter that could grow
with the log is capped (distinct paths per bot, tracked bot IPs, IPs per watched URL); a cap
that was hit is reported in the output.

A request is attributed to a bot by its user agent. Search engine bots are verified the way the
vendors document it: the reverse DNS name of the client IP must end in one of the vendor's
domains, and a forward lookup of that name must return the same IP. Lookups use the event
loop's resolver, are cached per IP for the call, run with bounded concurrency and are capped in
number (the most active IPs first).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections import Counter
from collections.abc import AsyncIterable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from itertools import islice
from typing import Any, Literal
from urllib.parse import urlsplit

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.parse.urls import normalize_url

from .models import BotStats, LogAnalysis, PathCount, WastedCrawl

LogFormat = Literal["combined", "common", "json"]

_COMMON = r'(?P<ip>\S+) \S+ \S+ \[[^\]]*\] "(?P<request>[^"]*)" (?P<status>\d{3}) \S+'
_PATTERNS = {
    "common": re.compile(_COMMON),
    "combined": re.compile(_COMMON + r' "(?P<referer>(?:[^"\\]|\\.)*)" "(?P<ua>(?:[^"\\]|\\.)*)"'),
}
_UA_CACHE, _UA_CACHE_CHARS = 2048, 512
_JSON_FIELDS = {
    "ip": ("remote_addr", "client_ip", "clientip", "remote_ip", "ip"),
    "ua": ("http_user_agent", "user_agent", "userAgent", "ua", "agent"),
    "status": ("status", "status_code", "statusCode", "response_status"),
    "path": ("request_uri", "uri", "path", "url", "request"),
}


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("thresholds.json").read_text(encoding="utf-8"))
    return data


@dataclass(frozen=True)
class BotDef:
    name: str
    kind: str  # search | ai
    vendor: str
    match: tuple[str, ...]
    domains: tuple[str, ...]


@lru_cache(maxsize=1)
def bot_catalogue() -> tuple[BotDef, ...]:
    """Search bots of ``search-bots.json`` first, then the AI crawlers of the core catalogue."""
    search = json.loads(files(__package__).joinpath("search-bots.json").read_text(encoding="utf-8"))
    ai = json.loads(files("seoprobe.core").joinpath("data/bots.json").read_text(encoding="utf-8"))
    out = [
        BotDef(b["name"], "search", b["vendor"], tuple(m.lower() for m in b["match"]), tuple(b["domains"]))
        for b in search["bots"]
    ]
    known = {m for b in out for m in b.match}
    out += [
        BotDef(c["token"], "ai", c["vendor"], (c["token"].lower(),), ())
        for c in ai["crawlers"]
        if c["token"].lower() not in known
    ]
    return tuple(out)


@lru_cache(maxsize=1)
def _bot_matcher() -> tuple[re.Pattern[str], dict[str, BotDef]]:
    by_token = {m: b for b in bot_catalogue() for m in b.match}
    tokens = sorted(by_token, key=lambda t: (-len(t), t))  # longest first at the same position
    return re.compile("|".join(re.escape(t) for t in tokens), re.I), by_token


def identify_bot(user_agent: str) -> BotDef | None:
    pattern, by_token = _bot_matcher()
    m = pattern.search(user_agent)
    return by_token[m.group(0).lower()] if m else None


@dataclass
class Request:
    ip: str
    path: str
    status: int
    user_agent: str


def _target_path(target: str) -> str:
    if target.startswith(("http://", "https://")):
        parts = urlsplit(target)
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return target


def _from_request_line(request: str) -> str | None:
    parts = request.split(" ")
    if len(parts) < 2 or not parts[1]:
        return None
    return _target_path(parts[1])


def _json_value(row: dict[str, Any], key: str) -> Any:
    for name in _JSON_FIELDS[key]:
        if row.get(name) not in (None, ""):
            return row[name]
    return None


def parse_line(line: str, fmt: LogFormat) -> Request | None:
    """One access log line; None when it cannot be read."""
    line = line.strip()
    if not line:
        return None
    if fmt == "json":
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        ip, raw_path, status = _json_value(row, "ip"), _json_value(row, "path"), _json_value(row, "status")
        if ip is None or raw_path is None or status is None:
            return None
        raw_path = str(raw_path)
        path = _from_request_line(raw_path) if " " in raw_path else _target_path(raw_path)
        try:
            code = int(status)
        except (TypeError, ValueError):
            return None
        if path is None or not 100 <= code <= 599:
            return None
        return Request(str(ip), path, code, str(_json_value(row, "ua") or ""))
    m = _PATTERNS[fmt].match(line)
    if m is None:
        return None
    path = _from_request_line(m.group("request"))
    if path is None:
        return None
    ua = m.group("ua") if fmt == "combined" else ""
    return Request(m.group("ip"), path, int(m.group("status")), (ua or "").replace('\\"', '"'))


@dataclass
class _BotAcc:
    bot: BotDef
    requests: int = 0
    status: Counter[int] = field(default_factory=Counter)
    paths: Counter[str] = field(default_factory=Counter)
    paths_truncated: bool = False
    parameterised: int = 0
    redirect: int = 0
    client_error: int = 0
    wasted: int = 0
    untracked: int = 0  # requests from IPs over the tracking cap


@dataclass
class _Stats:
    """Everything kept while streaming; each container is capped."""

    fmt: LogFormat
    limits: dict[str, Any]
    url_keys: dict[str, int]  # watched URL key -> index
    lines_total: int = 0
    lines_parsed: int = 0
    lines_unparsable: int = 0
    other_requests: int = 0
    bots: dict[str, _BotAcc] = field(default_factory=dict)
    ip_requests: dict[tuple[str, str], int] = field(default_factory=dict)  # (bot, ip) -> requests
    ips_truncated: bool = False
    url_hits: dict[int, dict[tuple[str, str], None]] = field(default_factory=dict)  # url -> (bot, ip)
    ua_cache: dict[str, BotDef | None] = field(default_factory=dict)

    def consume(self, lines: Iterable[str]) -> int:
        """Process ``lines``; returns how many were read (runs in a worker thread)."""
        n = 0
        max_chars = int(self.limits["max_line_chars"])
        for line in lines:
            n += 1
            req = parse_line(line, self.fmt) if len(line) <= max_chars else None
            if req is None:
                self.lines_unparsable += 1 if line.strip() else 0
                continue
            self.lines_parsed += 1
            self._add(req)
        self.lines_total += n
        return n

    def consume_from(self, it: Iterator[str], size: int) -> int:
        return self.consume(islice(it, size))

    def _bot(self, user_agent: str) -> BotDef | None:
        """``identify_bot`` with a bounded cache (user agents repeat heavily in a log)."""
        if user_agent in self.ua_cache:
            return self.ua_cache[user_agent]
        bot = identify_bot(user_agent)
        if len(self.ua_cache) < _UA_CACHE and len(user_agent) <= _UA_CACHE_CHARS:
            self.ua_cache[user_agent] = bot
        return bot

    def _add(self, req: Request) -> None:
        bot = self._bot(req.user_agent) if req.user_agent else None
        if bot is None:
            self.other_requests += 1
            return
        acc = self.bots.get(bot.name)
        if acc is None:
            acc = self.bots[bot.name] = _BotAcc(bot)
        acc.requests += 1
        acc.status[req.status] += 1
        if req.path in acc.paths or len(acc.paths) < int(self.limits["max_paths_per_bot"]):
            acc.paths[req.path] += 1
        else:
            acc.paths_truncated = True
        query = "?" in req.path
        acc.parameterised += query
        acc.redirect += 300 <= req.status < 400
        acc.client_error += 400 <= req.status < 500
        acc.wasted += query or 300 <= req.status < 500
        key = (bot.name, req.ip)
        if key in self.ip_requests:
            self.ip_requests[key] += 1
        elif len(self.ip_requests) < int(self.limits["max_tracked_ips"]):
            self.ip_requests[key] = 1
        else:
            self.ips_truncated = True
            acc.untracked += 1
            return
        if self.url_keys and bot.kind == "search":
            idx = self.url_keys.get(_url_key("http://log.invalid" + req.path))
            if idx is not None:
                hits = self.url_hits.setdefault(idx, {})
                if len(hits) < int(self.limits["max_ips_per_url"]):
                    hits[key] = None


def _url_key(url: str) -> str:
    norm = normalize_url(url)
    if norm is None:
        return ""
    parts = urlsplit(norm)
    return parts.path + (f"?{parts.query}" if parts.query else "")


async def _lookup(ip: str, timeout_s: float) -> tuple[str, set[str]] | None:
    """
    (reverse name, forward addresses) of ``ip``; ``("", set())`` when there is no name, None
    when the resolver did not answer in time.
    """
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_s):
            name, _port = await loop.getnameinfo((ip, 0), socket.NI_NAMEREQD)
            name = name.rstrip(".").lower()
            infos = await loop.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except TimeoutError:
        return None
    except (OSError, UnicodeError):
        return "", set()
    forward = set()
    for info in infos:
        try:
            forward.add(str(ipaddress.ip_address(str(info[4][0]).split("%")[0])))
        except ValueError:
            continue
    return name, forward


def _domain_ok(name: str, domains: tuple[str, ...]) -> bool:
    return any(name == d or name.endswith("." + d) for d in domains)


async def _verify(
    stats: _Stats, *, max_ips: int, concurrency: int, timeout_s: float
) -> tuple[dict[tuple[str, str], bool | None], int, int]:
    """(bot, ip) -> True verified / False spoofed / None unknown; plus IPs looked up and skipped."""
    verifiable = {name for name, acc in stats.bots.items() if acc.bot.domains}
    pairs = sorted(((k, n) for k, n in stats.ip_requests.items() if k[0] in verifiable), key=lambda kv: (-kv[1], kv[0]))
    order: list[str] = []
    for (_bot, ip), _n in pairs:
        if ip not in order:
            order.append(ip)
    chosen, skipped = order[:max_ips], max(0, len(order) - max_ips)
    cache: dict[str, tuple[str, set[str]] | None] = {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def resolve(ip: str) -> None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            cache[ip] = ("", set())
            return
        if not addr.is_global:
            cache[ip] = ("", set())  # a private or reserved address is never a search engine
            return
        async with sem:
            cache[ip] = await _lookup(str(addr), timeout_s)

    await asyncio.gather(*(resolve(ip) for ip in chosen))
    verdict: dict[tuple[str, str], bool | None] = {}
    for (bot, ip), _n in pairs:
        if ip not in cache or cache[ip] is None:
            verdict[(bot, ip)] = None
            continue
        name, forward = cache[ip]  # type: ignore[misc]
        try:
            canonical_ip = str(ipaddress.ip_address(ip))
        except ValueError:
            canonical_ip = ip
        verdict[(bot, ip)] = bool(name) and _domain_ok(name, stats.bots[bot].bot.domains) and canonical_ip in forward
    return verdict, len(chosen), skipped


def _bot_stats(
    acc: _BotAcc, stats: _Stats, verdict: dict[tuple[str, str], bool | None], verify: bool, top: int, listed: int
) -> BotStats:
    verified = spoofed = 0
    spoofed_ips: list[str] = []
    ips = [(ip, n) for (bot, ip), n in stats.ip_requests.items() if bot == acc.bot.name]
    for ip, n in ips:
        v = verdict.get((acc.bot.name, ip)) if verify and acc.bot.domains else None
        if v is True:
            verified += n
        elif v is False:
            spoofed += n
            spoofed_ips.append(ip)
    paths = sorted(acc.paths.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return BotStats(
        name=acc.bot.name,
        kind=acc.bot.kind,
        vendor=acc.bot.vendor,
        verifiable=bool(acc.bot.domains),
        requests=acc.requests,
        verified_requests=verified,
        spoofed_requests=spoofed,
        unverified_requests=acc.requests - verified - spoofed,
        distinct_ips=len(ips),
        spoofed_ips=sorted(spoofed_ips)[:listed],
        status={str(code): n for code, n in sorted(acc.status.items())},
        top_paths=[PathCount(path=p, requests=n) for p, n in paths],
        paths_truncated=acc.paths_truncated,
        wasted=WastedCrawl(
            parameterised=acc.parameterised,
            redirect=acc.redirect,
            client_error=acc.client_error,
            total=acc.wasted,
            share=round(acc.wasted / acc.requests, 4) if acc.requests else 0.0,
        ),
    )


async def _stream(stats: _Stats, lines: Iterable[str] | AsyncIterable[str], chunk: int) -> None:
    if isinstance(lines, AsyncIterable):
        buf: list[str] = []
        async for line in lines:
            buf.append(line)
            if len(buf) >= chunk:
                await asyncio.to_thread(stats.consume, buf)
                buf = []
        if buf:
            await asyncio.to_thread(stats.consume, buf)
        return
    it = iter(lines)
    while await asyncio.to_thread(stats.consume_from, it, chunk) == chunk:
        pass


@operation(
    "analyze_logs",
    output=LogAnalysis,
    requires=Requirements(network=True, note="reverse and forward DNS lookups to verify search bots"),
    timeout_s=1800.0,
)
async def analyze_logs(
    ctx: Context,
    lines: Iterable[str] | AsyncIterable[str],
    *,
    format: LogFormat = "combined",
    urls: list[str] | None = None,
    verify_bots: bool = True,
    waste_share: float | None = None,
    server_error_share: float | None = None,
    unparsable_share: float | None = None,
    min_bot_requests: int | None = None,
    max_verify_ips: int | None = None,
) -> Outcome[LogAnalysis]:
    """
    Per bot: requests, verified vs spoofed, status distribution, top paths and wasted crawl;
    with ``urls``, the ones no verified search bot fetched. Unparsable lines are counted.
    """
    d = dict(_defaults()["analyze_logs"])
    for name, value in (
        ("waste_share", waste_share),
        ("server_error_share", server_error_share),
        ("unparsable_share", unparsable_share),
        ("min_bot_requests", min_bot_requests),
        ("max_verify_ips", max_verify_ips),
    ):
        if value is not None:
            d[name] = value
    watched, urls_skipped = (
        (urls or [])[: ctx.config.max_batch_urls],
        max(0, len(urls or []) - ctx.config.max_batch_urls),
    )
    url_keys: dict[str, int] = {}
    for i, u in enumerate(watched):
        url_keys.setdefault(_url_key(u), i)
    url_keys.pop("", None)
    stats = _Stats(fmt=format, limits=d, url_keys=url_keys)
    await _stream(stats, lines, int(d["chunk_lines"]))

    verdict: dict[tuple[str, str], bool | None] = {}
    looked_up = not_looked_up = 0
    if verify_bots:
        verdict, looked_up, not_looked_up = await _verify(
            stats,
            max_ips=int(d["max_verify_ips"]),
            concurrency=int(d["dns_concurrency"]),
            timeout_s=float(d["dns_timeout_s"]),
        )
    bots = sorted(
        (
            _bot_stats(acc, stats, verdict, verify_bots, int(d["top_paths"]), int(d["spoofed_ips_listed"]))
            for acc in stats.bots.values()
        ),
        key=lambda b: (-b.requests, b.name),
    )

    def crawled(url: str) -> bool:
        idx = url_keys.get(_url_key(url))
        hits = stats.url_hits.get(idx, {}) if idx is not None else {}
        return any((verdict.get(k) is True) if verify_bots else True for k in hits)

    uncrawled = [u for u in watched if not crawled(u)]
    thresholds = {k: float(d[k]) for k in ("waste_share", "server_error_share", "unparsable_share", "min_bot_requests")}
    data = LogAnalysis(
        format=format,
        lines_total=stats.lines_total,
        lines_parsed=stats.lines_parsed,
        lines_unparsable=stats.lines_unparsable,
        bot_requests=sum(b.requests for b in bots),
        other_requests=stats.other_requests,
        bots=bots,
        verify_bots=verify_bots,
        ips_verified=looked_up,
        ips_not_verified=not_looked_up,
        ips_truncated=stats.ips_truncated,
        urls_checked=len(watched),
        urls_skipped=urls_skipped,
        uncrawled_urls=uncrawled,
        thresholds=thresholds,
    )
    findings, not_checked = _findings(ctx, data, thresholds)
    return Outcome(data=data, findings=findings, not_checked=not_checked)


def _findings(ctx: Context, data: LogAnalysis, th: dict[str, float]) -> tuple[list[Finding], list[NotChecked]]:
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    for b in data.bots:
        if b.spoofed_requests:
            findings.append(
                ctx.finding(
                    "logs-spoofed-bot",
                    evidence=f"{b.name}: {b.spoofed_requests} requests from IPs failing reverse/forward DNS "
                    f"({', '.join(b.spoofed_ips)})",
                    count=b.spoofed_requests,
                )
            )
        if b.kind != "search" or b.requests < th["min_bot_requests"]:
            continue
        if b.wasted.share >= th["waste_share"]:
            findings.append(
                ctx.finding(
                    "logs-crawl-waste",
                    evidence=f"{b.name}: {b.wasted.total}/{b.requests} requests ({b.wasted.share:.1%}) to "
                    f"parameterised URLs ({b.wasted.parameterised}), redirects ({b.wasted.redirect}) or 4xx "
                    f"({b.wasted.client_error}); threshold {th['waste_share']:.0%}",
                    count=b.wasted.total,
                )
            )
        server = sum(n for code, n in b.status.items() if code.startswith("5"))
        if server and server / b.requests >= th["server_error_share"]:
            findings.append(
                ctx.finding(
                    "logs-bot-server-errors",
                    evidence=f"{b.name}: {server}/{b.requests} requests answered 5xx "
                    f"({server / b.requests:.1%}); threshold {th['server_error_share']:.0%}",
                    count=server,
                )
            )
    if data.uncrawled_urls:
        findings.append(
            ctx.finding(
                "logs-uncrawled-urls",
                evidence=f"{len(data.uncrawled_urls)}/{data.urls_checked} never fetched by a verified search bot: "
                + ", ".join(data.uncrawled_urls[:5]),
                count=len(data.uncrawled_urls),
            )
        )
    if data.lines_total and data.lines_unparsable / data.lines_total >= th["unparsable_share"]:
        findings.append(
            ctx.finding(
                "logs-unparsable-lines",
                evidence=f"{data.lines_unparsable}/{data.lines_total} lines not in the {data.format} format; "
                f"threshold {th['unparsable_share']:.0%}",
                count=data.lines_unparsable,
            )
        )
    if data.ips_not_verified:
        not_checked.append(ctx.not_checked("logs-spoofed-bot", "verification-cap"))
    return findings, not_checked
