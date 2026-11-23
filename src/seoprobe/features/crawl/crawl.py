"""
Discovery and sampling of a site's pages, then fetching the sample.

Stratified, seeded sampling (core vs exploration). Deterministic: the same candidates + seed give
the same sample.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import zlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import parse_html
from seoprobe.core.parse.urls import (
    in_scope,
    is_crawlable_page,
    is_low_value,
    normalize_url,
    origin_of,
    template_key,
    url_key,
)
from seoprobe.features.site import RobotsReport, SitemapReport
from seoprobe.features.site import robots as robots_op
from seoprobe.features.site import sitemap as sitemap_op

from .models import CrawlReport

FETCH_CONCURRENCY = 6
MAX_EXTRA_URLS = 20


@dataclass(frozen=True)
class Candidate:
    url: str
    lastmod: str | None = None
    in_sitemap: bool = False
    linked: bool = False


@dataclass(frozen=True)
class Sampled:
    url: str
    sample_kind: Literal["core", "exploration"]
    template_key: str


def _stable(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def select_sample(
    candidates: list[Candidate],
    *,
    n: int,
    seed: int,
    mandatory: list[str] | None = None,
    max_template_share: float = 1 / 3,
) -> tuple[list[Sampled], dict[str, dict[str, int]]]:
    """Returns (sample, template_stats). ``mandatory`` URLs are always core."""
    mandatory = mandatory or []
    seen: set[str] = set()
    out: list[Sampled] = []

    def add(url: str, kind: Literal["core", "exploration"]) -> bool:
        k = url_key(url)
        if k in seen or len(out) >= n:
            return False
        seen.add(k)
        out.append(Sampled(url, kind, template_key(url)))
        return True

    for u in mandatory:
        add(u, "core")

    by_tpl: dict[str, list[Candidate]] = defaultdict(list)
    uniq: dict[str, Candidate] = {}
    for c in sorted(candidates, key=lambda c: c.url):
        uniq.setdefault(url_key(c.url), c)
    for c in uniq.values():
        by_tpl[template_key(c.url)].append(c)
    stats = {t: {"discovered": len(cs), "sampled": 0} for t, cs in by_tpl.items()}

    # 1) one fixed representative per template -> core (stable across seeds)
    templates = sorted(by_tpl, key=lambda t: (-len(by_tpl[t]), t))
    templates.sort(key=lambda t: is_low_value(by_tpl[t][0].url))  # low-value templates last
    for t in templates:
        rep = min(by_tpl[t], key=lambda c: _stable(c.url))
        add(rep.url, "core")

    # 2) exploration quota proportional to template size, seeded, capped per template
    remaining = n - len(out)
    if remaining > 0:
        cap = max(1, int(n * max_template_share))
        weights = {t: len(cs) * (0.3 if is_low_value(cs[0].url) else 1.0) for t, cs in by_tpl.items()}
        total_w = sum(weights.values()) or 1.0
        rng = random.Random(seed)
        quota = {t: min(cap, max(0, round(remaining * w / total_w))) for t, w in weights.items()}
        for t in sorted(by_tpl):
            pool = [c for c in by_tpl[t] if url_key(c.url) not in seen]
            newest = sorted(pool, key=lambda c: (c.lastmod or "", c.url), reverse=True)
            take: list[Candidate] = newest[: max(0, quota[t] // 2)]
            rest = [c for c in pool if c not in take]
            rng.shuffle(rest)
            take += rest[: max(0, quota[t] - len(take))]
            for c in take:
                add(c.url, "exploration")
        # top up deterministically if rounding left slots; the per-template cap holds
        # unless no other template has candidates left
        if len(out) < n:
            counts: dict[str, int] = defaultdict(int)
            for x in out:
                counts[x.template_key] += 1
            leftovers = [c for c in uniq.values() if url_key(c.url) not in seen]
            rng.shuffle(leftovers)
            for relax in (False, True):
                for c in leftovers:
                    if len(out) >= n:
                        break
                    t = template_key(c.url)
                    if not relax and counts[t] >= cap:
                        continue
                    if add(c.url, "exploration"):
                        counts[t] += 1

    for s in out:
        if s.template_key in stats:
            stats[s.template_key]["sampled"] += 1
        else:
            stats[s.template_key] = {"discovered": 1, "sampled": 1}
    return out, stats


@operation("crawl", output=CrawlReport, requires=Requirements(network=True), timeout_s=600.0)
async def crawl(
    ctx: Context,
    url: str,
    *,
    entry: Page | None = None,
    robots: RobotsReport | None = None,
    sitemap: SitemapReport | None = None,
    max_pages: int | None = None,
    extra_urls: list[str] | None = None,
    seed: int | None = None,
) -> Outcome[CrawlReport]:
    """
    Discover the site's pages (sitemaps and the entry page's links), pick a stratified seeded
    sample within the page limit and fetch it.
    """
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    entry_page = entry if entry is not None else await ctx.page(url)
    origin = origin_of(entry_page.final_url)

    # ---- discovery
    if robots is None:
        rb = await robots_op(url, origin=origin)
        findings += rb.findings
        not_checked += rb.not_checked
        robots = rb.data if rb.data is not None else RobotsReport(status="unreachable")
    if sitemap is None:
        sm = await sitemap_op(origin, robots_sitemaps=list(robots.sitemaps))
        findings += sm.findings
        not_checked += sm.not_checked
        sitemap = sm.data if sm.data is not None else SitemapReport()
    cands: dict[str, Candidate] = {}
    for u in sitemap.urls:
        n = normalize_url(u.loc)
        if n:
            cands[n] = Candidate(n, lastmod=u.lastmod, in_sitemap=True)
    if entry_page.html:
        parsed = await asyncio.to_thread(parse_html, entry_page.html, entry_page.final_url)
        for ln in parsed.links:
            n = normalize_url(ln.resolved) if ln.resolved else None
            if n and n not in cands:
                cands[n] = Candidate(n, linked=True)
    candidates = list(cands.values())
    sitemap_set = {n for u in sitemap.urls if (n := normalize_url(u.loc))}
    await ctx.emit("progress", stage="discovery", message_key="discovery.found", pages_planned=len(cands))

    # ---- sample
    host = urlparse(origin).hostname or ""
    skipped: dict[str, int] = defaultdict(int)
    usable: list[Candidate] = []
    for c in candidates:
        if not in_scope(c.url, host):
            skipped["out_of_scope"] += 1
        elif not is_crawlable_page(c.url):
            skipped["not_a_page"] += 1
        elif not robots.allows(c.url, ctx.config.bot_name):
            skipped["robots"] += 1
        else:
            usable.append(c)
    n_pages = max(1, min(max_pages or ctx.config.max_pages, ctx.config.max_pages_cap))
    extra = [u for u in (normalize_url(x) for x in (extra_urls or [])[:MAX_EXTRA_URLS]) if u and in_scope(u, host)]
    rng_seed = seed if seed is not None else zlib.crc32(url.encode())
    sample, stats = select_sample(usable, n=n_pages, seed=rng_seed, mandatory=[url, *extra])
    coverage = {
        "pages_discovered": len(candidates) + 1,
        "pages_planned": len(sample),
        "pages_fetched": 0,
        "pages_rendered": 0,
        "sitemap_urls": len(sitemap.urls),
        "templates": [{"key": k, **v} for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["discovered"])][:30],
        "skipped": dict(skipped),
    }
    await ctx.emit("progress", stage="sample", message_key="sample.selected", pages_planned=len(sample))

    # ---- fetch the sample
    todo = [s for s in sample if s.url != url]
    planned = len(sample)
    done = {"n": 1}
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    fetched: dict[str, Page] = {}

    async def one(s: Sampled) -> None:
        async with sem:
            fr = await ctx.http.fetch(s.url)
            page = Page.from_fetch(fr, sample_kind=s.sample_kind)
            await page.parse()
            fetched[s.url] = page
            done["n"] += 1
            await ctx.emit(
                "progress",
                stage="fetch_pages",
                message_key="fetch.page",
                pages_fetched=done["n"],
                pages_planned=planned,
            )

    await asyncio.gather(*(one(s) for s in todo))
    pages = [entry_page, *(fetched[s.url] for s in todo)]
    entry_tpl = next((s for s in sample if s.url == url), None)
    templates: dict[str, str | None] = {url: entry_tpl.template_key if entry_tpl else None}
    templates.update({s.url: s.template_key for s in todo})
    n_fetched = sum(1 for p in pages if p.error is None and p.status is not None)
    coverage["pages_fetched"] = n_fetched
    report = CrawlReport(
        entry_url=url,
        pages=pages,
        templates=templates,
        coverage=coverage,
        sitemap_urls=sorted(sitemap_set),
        partial_sample=n_fetched < planned,
    )
    return Outcome(data=report, findings=findings, not_checked=not_checked)
