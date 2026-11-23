"""
Batch checks of URL lists: indexability per URL and an old -> new redirect map.

Both process at most ``Config.max_batch_urls`` rows (the rest is counted as skipped) with a
bounded number of rows in flight, on top of the client's per-host limiter. Every request goes
through ``ctx.http``, so each URL and each redirect hop passes the SSRF check.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import lru_cache
from importlib.resources import files
from typing import Any, TypeVar
from urllib.parse import urljoin, urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.client import RawResponse
from seoprobe.core.page import Page
from seoprobe.core.parse.html import url_match_key
from seoprobe.core.parse.urls import normalize_url, origin_of
from seoprobe.features.site import RobotsReport, header_directives, meta_noindex, robots, same_page

from .models import RedirectCheck, RedirectChecks, UrlCheck, UrlChecks

T = TypeVar("T")
R = TypeVar("R")

# reason code -> rule raised for it (several reasons share one rule)
_REASON_RULES = {
    "invalid-url": "url-check-broken",
    "fetch-error": "url-check-broken",
    "http-error": "url-check-broken",
    "redirected": "url-check-redirected",
    "robots-disallowed": "url-check-robots-blocked",
    "noindex-meta": "url-check-noindex",
    "noindex-header": "url-check-noindex",
    "canonical-elsewhere": "url-check-canonical-elsewhere",
}
_ISSUE_RULES = {
    "not-redirected": "redirect-not-redirected",
    "wrong-target": "redirect-wrong-target",
    "chain-too-long": "redirect-too-many-hops",
    "loop": "redirect-loop",
    "target-error": "redirect-target-error",
    "temporary-redirect": "redirect-temporary",
}


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("thresholds.json").read_text(encoding="utf-8"))
    return data


def _limit(rows: list[T], cap: int) -> tuple[list[T], int]:
    return rows[:cap], max(0, len(rows) - cap)


async def _bounded(rows: list[T], concurrency: int, fn: Callable[[T], Awaitable[R]]) -> list[R]:
    """``fn`` over ``rows`` with at most ``concurrency`` in flight; results in input order."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run(row: T) -> R:
        async with sem:
            return await fn(row)

    return list(await asyncio.gather(*(run(r) for r in rows)))


def _page_key(url: str) -> str:
    """The key ``same_page`` compares (scheme, ``www.`` and trailing slash ignored)."""
    p = urlparse(url_match_key(url))
    host = p.hostname or ""
    host = host[4:] if host.startswith("www.") else host
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{host}{port}{p.path}?{p.query}"


# ------------------------------------------------------------------ check_urls


def _verdict(row: UrlCheck) -> list[str]:
    reasons: list[str] = []
    if row.error:
        reasons.append("invalid-url" if row.error == "invalid-url" else "fetch-error")
    elif row.status is not None and row.status >= 400:
        reasons.append("http-error")
    if row.hops:
        reasons.append("redirected")
    if row.robots_allowed is False:
        reasons.append("robots-disallowed")
    if row.noindex_meta:
        reasons.append("noindex-meta")
    if row.noindex_header:
        reasons.append("noindex-header")
    if row.canonical_elsewhere:
        reasons.append("canonical-elsewhere")
    return reasons


def _evidence(row: UrlCheck, reason: str) -> str:
    if reason in ("fetch-error", "invalid-url"):
        return f"error={row.error}"
    if reason == "http-error":
        return f"status={row.status}"
    if reason == "redirected":
        return f"{row.hops} hop(s) -> {row.final_url} ({row.status})"
    if reason == "canonical-elsewhere":
        return f"canonical={row.canonical}"
    if reason == "noindex-header":
        return f"X-Robots-Tag: {row.x_robots_tag}"
    if reason == "noindex-meta":
        return "meta robots noindex"
    return reason


@operation("check_urls", output=UrlChecks, requires=Requirements(network=True), timeout_s=900.0)
async def check_urls(
    ctx: Context,
    urls: list[str],
    *,
    check_robots: bool = True,
    sitemap_urls: list[str] | None = None,
    user_agent: str | None = None,
    concurrency: int | None = None,
) -> Outcome[UrlChecks]:
    """
    Per URL: status, redirects, canonical, noindex (meta and header), robots.txt permission,
    sitemap membership and an indexable verdict with its reasons.
    """
    d = _defaults()["check_urls"]
    agent = user_agent or str(d["user_agent"])
    conc = concurrency or int(d["concurrency"])
    cleaned = [u.strip() for u in urls if u.strip()]
    unique = list(dict.fromkeys(cleaned))
    todo, skipped = _limit(unique, ctx.config.max_batch_urls)
    in_sitemap = {_page_key(u) for u in sitemap_urls} if sitemap_urls is not None else None
    robots_tasks: dict[str, asyncio.Task[RobotsReport | None]] = {}

    async def fetch_robots(url: str, origin: str) -> RobotsReport | None:
        res = await robots(url, origin=origin)
        return res.data if res.ok else None

    async def robots_of(url: str) -> RobotsReport | None:
        origin = origin_of(url)
        if origin not in robots_tasks:
            robots_tasks[origin] = asyncio.ensure_future(fetch_robots(url, origin))
        return await robots_tasks[origin]

    async def one(url: str) -> UrlCheck:
        if normalize_url(url) is None:
            return UrlCheck(url=url, error="invalid-url", reasons=["invalid-url"])
        fr = await ctx.http.fetch(url, max_bytes=int(d["max_page_bytes"]))
        page = Page.from_fetch(fr)
        row = UrlCheck(
            url=url,
            final_url=fr.final_url,
            status=fr.status,
            error=fr.error,
            redirect_chain=list(fr.redirect_chain),
            hops=len(fr.redirect_chain),
        )
        if fr.error is None:
            parsed = await page.parse()
            row.x_robots_tag = fr.headers.get("x-robots-tag") or None
            row.noindex_header = bool(header_directives(row.x_robots_tag or "") & {"noindex", "none"})
            row.noindex_meta = meta_noindex(page)
            if parsed is not None and parsed.canonical:
                row.canonical = parsed.canonical
                row.canonical_elsewhere = not same_page(parsed.canonical, page.effective_url)
        if check_robots and fr.error is None:
            report = await robots_of(url)
            row.robots_allowed = report.allows(url, agent) if report is not None and report.available else None
        if in_sitemap is not None:
            row.in_sitemap = _page_key(url) in in_sitemap or _page_key(page.effective_url) in in_sitemap
        row.reasons = _verdict(row)
        row.indexable = not row.reasons
        return row

    try:
        items = await _bounded(todo, conc, one)
    finally:
        for task in robots_tasks.values():
            task.cancel()

    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    for row in items:
        raised: set[str] = set()
        for reason in row.reasons:
            rule = _REASON_RULES[reason]
            if rule not in raised:
                raised.add(rule)
                findings.append(ctx.finding(rule, url=row.url, evidence=_evidence(row, reason)))
        if row.in_sitemap is True and not row.indexable:
            findings.append(
                ctx.finding("url-check-sitemap-not-indexable", url=row.url, evidence=", ".join(row.reasons))
            )
        elif row.in_sitemap is False and row.indexable:
            findings.append(ctx.finding("url-check-not-in-sitemap", url=row.url, evidence="not in the given sitemap"))
        if check_robots and row.robots_allowed is None and row.error is None:
            not_checked.append(ctx.not_checked("url-check-robots-blocked", "robots-unreachable", row.url))
    data = UrlChecks(
        items=items,
        checked=len(items),
        skipped=skipped,
        duplicates=len(cleaned) - len(unique),
        user_agent=agent,
        robots_checked=check_robots,
        sitemap_checked=sitemap_urls is not None,
        concurrency=conc,
    )
    return Outcome(data=data, findings=findings, not_checked=not_checked, partial=skipped > 0)


# ------------------------------------------------------------------ check_redirects


def _same_target(a: str, b: str) -> bool:
    return (normalize_url(a) or a) == (normalize_url(b) or b)


def _judge(old: str, expected: str, raw: RawResponse, max_hops: int, temporary: set[int]) -> RedirectCheck:
    chain = list(raw.redirect_chain or [])
    visited = [_hop_key(h.url) for h in chain] + [_hop_key(raw.final_url)]
    loop = len(set(visited)) < len(visited)
    failed = raw.error is not None or (raw.status is not None and raw.status >= 400)
    row = RedirectCheck(
        old=old,
        expected=expected,
        chain=chain,
        statuses=[h.status for h in chain] + ([raw.status] if raw.status is not None else []),
        final_url=None if loop else raw.final_url,
        final_status=raw.status,
        error=(raw.error or "").partition(":")[0] or None,
        hops=len(chain),
        loop=loop,
    )
    row.matches_expected = not loop and not failed and _same_target(raw.final_url, expected)
    issues: list[str] = []
    if loop:
        issues.append("loop")
    elif not chain:
        if raw.error is None and not _same_target(old, expected):
            issues.append("not-redirected")
        elif failed:
            issues.append("target-error")
    else:
        if not _same_target(raw.final_url, expected):
            issues.append("wrong-target")
        if failed:
            issues.append("target-error")
    if row.hops > max_hops and not loop:
        issues.append("chain-too-long")
    if any(h.status in temporary for h in chain):
        issues.append("temporary-redirect")
    row.issues = issues
    return row


def _hop_key(url: str) -> str:
    return normalize_url(url) or url


def _issue_evidence(row: RedirectCheck, issue: str, max_hops: int) -> str:
    chain = " -> ".join(f"{h.status} {h.url}" for h in row.chain)
    if issue == "not-redirected":
        return f"status={row.final_status}; expected {row.expected}"
    if issue == "wrong-target":
        return f"final {row.final_url}; expected {row.expected}"
    if issue == "chain-too-long":
        return f"{row.hops} hops (max {max_hops}): {chain}"
    if issue == "loop":
        return f"loop: {chain}"
    if issue == "target-error":
        return f"final {row.final_url}: status={row.final_status} error={row.error}"
    return f"temporary redirect status in the chain: {chain}"


@operation("check_redirects", output=RedirectChecks, requires=Requirements(network=True), timeout_s=900.0)
async def check_redirects(
    ctx: Context,
    mapping: list[tuple[str, str]] | dict[str, str],
    *,
    max_hops: int | None = None,
    temporary_statuses: list[int] | None = None,
    concurrency: int | None = None,
) -> Outcome[RedirectChecks]:
    """
    Per old -> expected row: the status chain, final URL, hop count, loop, whether it lands on
    the expected URL, and issue codes.
    """
    d = _defaults()["check_redirects"]
    hops_cap = int(d["max_hops"]) if max_hops is None else max_hops
    temporary = sorted(set(temporary_statuses if temporary_statuses is not None else d["temporary_statuses"]))
    conc = concurrency or int(d["concurrency"])
    pairs = list(mapping.items()) if isinstance(mapping, dict) else [tuple(p) for p in mapping]
    rows = [(old.strip(), urljoin(old.strip(), new.strip())) for old, new in pairs]
    todo, skipped = _limit(rows, ctx.config.max_batch_urls)

    async def one(pair: tuple[str, str]) -> RedirectCheck:
        old, expected = pair
        if normalize_url(old) is None:
            return RedirectCheck(old=old, expected=expected, error="invalid-url", issues=["target-error"])
        raw = await ctx.http.fetch_bytes(old, max_bytes=int(d["max_body_bytes"]))
        return _judge(old, expected, raw, hops_cap, set(temporary))

    items = await _bounded(todo, conc, one)
    findings = [
        ctx.finding(_ISSUE_RULES[issue], url=row.old, evidence=_issue_evidence(row, issue, hops_cap))
        for row in items
        for issue in row.issues
    ]
    data = RedirectChecks(
        items=items,
        checked=len(items),
        skipped=skipped,
        max_hops=hops_cap,
        temporary_statuses=temporary,
        concurrency=conc,
    )
    return Outcome(data=data, findings=findings, partial=skipped > 0)
