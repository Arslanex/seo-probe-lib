"""
Turn Search Console answers into the audit's data: property resolution, collection, findings
and keyword rankings.

``resolve_property`` is pure and **fail-closed**: it either finds a property the user owns for
exactly this site, or returns ``property=None`` with a reason. It never falls back to "the first
property" or any other site, so one customer's data can never be shown for another's domain.

``collect_gsc`` never raises: every failure becomes a ``GscData.status`` or a per-section
``error``, so a quota hit on URL Inspection still leaves the Performance numbers in the report.

URL Inspection quota is 2000 queries/day per property (600/min): an audit inspects the entry
URL plus at most ``inspect_limit`` sample URLs (default ``Config.gsc_inspect_limit``).

Keyword rankings: only the owner's verified property can answer "where do I rank": no paid SERP
provider is used and nobody else's ranking is available. Everything there is informational and
never part of the score.

Request budget: one ``query x page`` request per keyword (at most ``MAX_KEYWORDS``) plus one
``query`` request for the opportunity lists, so at most ``MAX_KEYWORDS + 1`` = 11 requests.
A 429 stops the remaining keyword requests (they are recorded as ``rate-limited``).

Heuristics (they are Search Console averages, not guarantees):

* **opportunity**: average position 4 to 20 with at least ``MIN_IMPRESSIONS`` impressions, the
  queries closest to page one or to the top three, most impressions first;
* **low CTR**: position <= 10, at least ``MIN_IMPRESSIONS`` impressions and a CTR under
  ``LOW_CTR_TOP3`` (positions 1-3) or ``LOW_CTR_TOP10`` (positions 4-10).

Search Console anonymises rare queries, so a keyword without data is "no data in the last 28
days", not "not ranking".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from seoprobe.core.models import CheckOutput
from seoprobe.core.parse.urls import url_key
from seoprobe.core.rules.catalog import make_finding

from .models import (
    LIST_LIMIT,
    LOW_CTR_TOP3,
    LOW_CTR_TOP10,
    MAX_KEYWORDS,
    MIN_IMPRESSIONS,
    GscData,
    GscInspection,
    GscRow,
    GscSitemap,
    KeywordRankings,
    KeywordRankRow,
    RankingQuery,
)
from .oauth import GscError as GscError  # re-exported: the analysis raises and catches it

TOTAL_TIMEOUT_S = 90.0
TOP_N = 20
QUERY_ROWS = 250  # rows fetched for the opportunity lists
PAGE_ROWS = 25  # rows per keyword (query x page)
_NO_ACCESS = {"siteUnverifiedUser"}


# --------------------------------------------------------------------------- ports


class SearchConsolePort(Protocol):
    """The Search Console calls the analysis needs (``gsc.GscClient``)."""

    oauth: Any  # exposes ``status(account)`` -> {"configured": bool, "connected": bool, "email": ...}
    account: str

    async def list_properties(self) -> list[dict[str, str]]: ...
    async def search_analytics(
        self,
        property: str,
        *,
        days: int = 28,
        dimensions: list[str] | None = None,
        row_limit: int = 25,
        query: str | None = None,
        query_operator: str = "contains",
    ) -> list[dict[str, Any]]: ...
    async def inspect_url(self, property: str, url: str) -> dict[str, Any]: ...
    async def list_sitemaps(self, property: str) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- property


@dataclass(frozen=True)
class Resolution:
    property: str | None
    reason: str | None = None  # property-not-found | no-permission (when property is None)
    permission: str | None = None


def _host(domain_or_url: str) -> str:
    raw = domain_or_url.strip()
    p = urlparse(raw if "://" in raw else f"//{raw}")
    return (p.hostname or "").lower().rstrip(".")


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _property_host(site_url: str) -> tuple[str, bool]:
    """(host, is_domain_property) of a property identifier."""
    if site_url.startswith("sc-domain:"):
        return site_url[len("sc-domain:") :].lower().rstrip("."), True
    return _host(site_url), False


def resolve_property(domain_or_url: str, properties: list[dict[str, Any]]) -> Resolution:
    """
    Pick the property that covers ``domain_or_url``.

    Priority: a Domain property (``sc-domain:``, which covers every subdomain) over an exact
    URL-prefix property (https before http, www variants of the same host). Properties with
    ``siteUnverifiedUser`` do not count. No match -> ``property-not-found``; matches that are all
    unverified -> ``no-permission``.
    """
    host = _host(domain_or_url)
    if not host:
        return Resolution(None, "property-not-found")
    bare = _bare(host)

    domain_hits: list[tuple[int, dict[str, Any]]] = []
    prefix_hits: list[tuple[int, dict[str, Any]]] = []
    unverified = False
    for prop in properties:
        site = str(prop.get("site_url") or prop.get("siteUrl") or "")
        if not site:
            continue
        phost, is_domain = _property_host(site)
        if not phost:
            continue
        if is_domain:
            # covers the apex and every subdomain: "notexample.com" must not match
            if not (bare == phost or bare.endswith("." + phost)):
                continue
            rank = 0 if phost == bare else 1  # exact apex beats a parent domain
        else:
            if _bare(phost) != bare:
                continue
            p = urlparse(site)
            path_ok = p.path in ("", "/")
            if not path_ok:  # a directory property does not cover the whole site
                continue
            rank = (0 if p.scheme == "https" else 2) + (0 if phost == host else 1)
        perm = str(prop.get("permission") or prop.get("permissionLevel") or "")
        if perm in _NO_ACCESS:
            unverified = True
            continue
        (domain_hits if is_domain else prefix_hits).append((rank, {**prop, "site_url": site}))

    for hits in (domain_hits, prefix_hits):
        if hits:
            _rank, best = min(hits, key=lambda h: (h[0], h[1]["site_url"]))
            return Resolution(
                best["site_url"],
                None,
                str(best.get("permission") or best.get("permissionLevel") or ""),
            )
    return Resolution(None, "no-permission" if unverified else "property-not-found")


# --------------------------------------------------------------------------- collector


def _row(r: dict[str, Any]) -> GscRow:
    keys = r.get("keys") or [""]
    return GscRow(
        key=str(keys[0]),
        clicks=int(r.get("clicks") or 0),
        impressions=int(r.get("impressions") or 0),
        ctr=float(r.get("ctr") or 0.0),
        position=float(r.get("position") or 0.0),
    )


def _inspection(url: str, result: dict[str, Any]) -> GscInspection:
    s = result.get("indexStatusResult")
    s = s if isinstance(s, dict) else {}
    coverage = s.get("coverageState")
    verdict = s.get("verdict")
    indexed: bool | None = None
    if isinstance(coverage, str):
        low = coverage.lower()
        indexed = "indexed" in low and "not indexed" not in low and "not-indexed" not in low
        if "currently not indexed" in low:
            indexed = False
    elif verdict == "PASS":
        indexed = True
    return GscInspection(
        url=url,
        verdict=verdict,
        coverage_state=coverage,
        indexing_state=s.get("indexingState"),
        robots_txt_state=s.get("robotsTxtState"),
        page_fetch_state=s.get("pageFetchState"),
        google_canonical=s.get("googleCanonical"),
        user_canonical=s.get("userCanonical"),
        last_crawl_time=s.get("lastCrawlTime"),
        crawled_as=s.get("crawledAs"),
        indexed=indexed,
    )


def _sitemap(s: dict[str, Any]) -> GscSitemap:
    return GscSitemap(
        path=str(s.get("path", "")),
        last_submitted=s.get("lastSubmitted"),
        last_downloaded=s.get("lastDownloaded"),
        is_pending=bool(s.get("isPending")),
        errors=int(s.get("errors") or 0),
        warnings=int(s.get("warnings") or 0),
    )


async def _inspect_all(client: SearchConsolePort, prop: str, urls: list[str]) -> list[GscInspection]:
    sem = asyncio.Semaphore(3)

    async def one(u: str) -> GscInspection:
        async with sem:
            try:
                return _inspection(u, await client.inspect_url(prop, u))
            except GscError as exc:
                return GscInspection(url=u, error=exc.label)

    return list(await asyncio.gather(*(one(u) for u in urls)))


async def _collect(
    client: SearchConsolePort,
    entry_url: str,
    sample_urls: list[str],
    inspect_limit: int,
    days: int,
    keywords: list[str] | None = None,
) -> GscData:
    status = await client.oauth.status(client.account)
    if not status["configured"]:
        return GscData(status="not-configured", period_days=days)
    if not status["connected"]:
        return GscData(status="not-connected", period_days=days)
    data = GscData(status="ok", account=status.get("email"), period_days=days)
    try:
        props = await client.list_properties()
    except GscError as exc:
        if exc.code == "not-connected":
            return GscData(status="not-connected", period_days=days)
        return GscData(status="error", account=data.account, period_days=days, error=exc.label)
    res = resolve_property(entry_url, props)
    if res.property is None:
        reason = "no-permission" if res.reason == "no-permission" else "property-not-found"
        return GscData(status=reason, account=data.account, period_days=days)  # type: ignore[arg-type]
    data.property, data.permission = res.property, res.permission
    prop = res.property

    urls: list[str] = []
    seen: set[str] = set()
    for u in [entry_url, *sample_urls][: inspect_limit + 1]:
        k = url_key(u)
        if k not in seen:
            seen.add(k)
            urls.append(u)

    async def perf() -> None:
        try:
            tot = await client.search_analytics(prop, days=days, row_limit=1)
            if tot:
                t = _row(tot[0])
                data.totals = {
                    "clicks": t.clicks,
                    "impressions": t.impressions,
                    "ctr": t.ctr,
                    "position": t.position,
                }
            else:
                data.totals = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        except GscError as exc:
            data.error = data.error or f"performance: {exc.label}"
            return
        try:
            data.top_queries = [
                _row(r) for r in await client.search_analytics(prop, days=days, dimensions=["query"], row_limit=TOP_N)
            ]
            data.top_pages = [
                _row(r) for r in await client.search_analytics(prop, days=days, dimensions=["page"], row_limit=TOP_N)
            ]
        except GscError as exc:
            data.error = data.error or f"performance: {exc.label}"

    async def sitemaps() -> None:
        try:
            data.sitemaps = [_sitemap(s) for s in await client.list_sitemaps(prop)]
        except GscError as exc:
            data.error = data.error or f"sitemaps: {exc.label}"

    async def inspect() -> None:
        data.inspections = await _inspect_all(client, prop, urls)

    async def rankings() -> None:
        data.rankings = await collect_rankings(client, prop, keywords, days)

    await asyncio.gather(perf(), sitemaps(), inspect(), rankings())
    return data


async def collect_gsc(
    client: SearchConsolePort,
    *,
    entry_url: str,
    sample_urls: list[str],
    inspect_limit: int = 10,
    days: int = 28,
    keywords: list[str] | None = None,
) -> GscData:
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            return await _collect(client, entry_url, sample_urls, inspect_limit, days, keywords)
    except TimeoutError:
        return GscData(status="error", period_days=days, error="timeout")
    except Exception as exc:  # noqa: BLE001  one integration must never break the audit
        return GscData(status="error", period_days=days, error=f"internal-error: {type(exc).__name__}")


def findings(gsc: GscData) -> CheckOutput:
    out = CheckOutput()
    if gsc.status != "ok":
        return out
    for i in gsc.inspections:
        if i.error:
            continue
        if i.indexed is False:
            out.findings.append(make_finding("gsc-page-not-indexed", url=i.url, evidence=i.coverage_state))
        if i.google_canonical and i.user_canonical and url_key(i.google_canonical) != url_key(i.user_canonical):
            out.findings.append(
                make_finding(
                    "gsc-canonical-mismatch",
                    url=i.url,
                    evidence=f"declared {i.user_canonical}; Google chose {i.google_canonical}",
                )
            )
        if (i.robots_txt_state or "").upper() == "DISALLOWED":
            out.findings.append(make_finding("gsc-robots-blocked", url=i.url, evidence=i.robots_txt_state))
        fetch = (i.page_fetch_state or "").upper()
        if fetch and fetch not in ("SUCCESSFUL", "PAGE_FETCH_STATE_UNSPECIFIED"):
            out.findings.append(make_finding("gsc-fetch-failed", url=i.url, evidence=fetch))
    bad = [s for s in gsc.sitemaps if s.errors > 0]
    if bad:
        out.findings.append(
            make_finding(
                "gsc-sitemap-errors",
                evidence=f"{bad[0].path}: {bad[0].errors} errors",
                count=len(bad),
            )
        )
    if not gsc.sitemaps and "sitemaps" not in (gsc.error or ""):
        out.findings.append(make_finding("gsc-sitemap-not-submitted"))
    if gsc.totals and gsc.totals.get("impressions", 1) == 0 and "performance" not in (gsc.error or ""):
        out.findings.append(make_finding("gsc-no-impressions", evidence=f"{gsc.period_days} days", confidence="medium"))
    out.facts["gsc_property"] = gsc.property
    return out


# --------------------------------------------------------------------------- rankings


def _fold(text: str) -> str:
    return text.strip().casefold()


def clean_keywords(keywords: list[str] | None) -> list[str]:
    """Trimmed, de-duplicated (case-insensitive) keywords, at most ``MAX_KEYWORDS``."""
    out: list[str] = []
    seen: set[str] = set()
    for k in keywords or []:
        k = " ".join(str(k).split())[:80]
        if k and _fold(k) not in seen:
            seen.add(_fold(k))
            out.append(k)
    return out[:MAX_KEYWORDS]


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _aggregate(keyword: str, rows: list[dict[str, Any]]) -> KeywordRankRow:
    """
    Collapse query x page rows into one row for ``keyword``.

    The exact query (case-insensitive) wins. Without one, the containing query with the most
    impressions is used and named in ``matched_query``. Position is impression-weighted.
    """
    by_query: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        keys = r.get("keys")
        if not isinstance(keys, list) or len(keys) < 2:
            continue
        by_query.setdefault(str(keys[0]), []).append(r)
    if not by_query:
        return KeywordRankRow(keyword=keyword, status="no-data")

    want = _fold(keyword)
    exact = [q for q in by_query if _fold(q) == want]
    if exact:
        chosen, matched = exact, None
    else:
        best = max(by_query, key=lambda q: (sum(_num(r.get("impressions")) for r in by_query[q]), q))
        chosen, matched = [best], best
    pages = [r for q in chosen for r in by_query[q]]
    impressions = sum(int(_num(r.get("impressions"))) for r in pages)
    clicks = sum(int(_num(r.get("clicks"))) for r in pages)
    if impressions > 0:
        position = sum(_num(r.get("position")) * _num(r.get("impressions")) for r in pages)
        position /= impressions
    else:
        position = _num(pages[0].get("position")) if pages else 0.0
    top = max(
        pages,
        key=lambda r: (_num(r.get("impressions")), _num(r.get("clicks")), str(r["keys"][1])),
    )
    return KeywordRankRow(
        keyword=keyword,
        matched_query=matched,
        impressions=impressions,
        clicks=clicks,
        ctr=(clicks / impressions) if impressions else 0.0,
        position=round(position, 2),
        top_page=str(top["keys"][1]),
        status="ok",
    )


def _query(r: dict[str, Any]) -> RankingQuery | None:
    keys = r.get("keys")
    if not isinstance(keys, list) or not keys:
        return None
    return RankingQuery(
        query=str(keys[0]),
        impressions=int(_num(r.get("impressions"))),
        clicks=int(_num(r.get("clicks"))),
        ctr=_num(r.get("ctr")),
        position=_num(r.get("position")),
    )


def opportunity_lists(rows: list[dict[str, Any]]) -> tuple[list[RankingQuery], list[RankingQuery]]:
    """(opportunities, low-CTR queries) from the property's query rows."""
    queries = [q for q in (_query(r) for r in rows) if q is not None]
    opps = [q for q in queries if 4 <= q.position <= 20 and q.impressions >= MIN_IMPRESSIONS]
    low = [
        q
        for q in queries
        if 0 < q.position <= 10
        and q.impressions >= MIN_IMPRESSIONS
        and q.ctr < (LOW_CTR_TOP3 if q.position <= 3 else LOW_CTR_TOP10)
    ]
    opps.sort(key=lambda q: (-q.impressions, q.query))
    low.sort(key=lambda q: (-q.impressions, q.query))
    return opps[:LIST_LIMIT], low[:LIST_LIMIT]


async def collect_rankings(
    client: SearchConsolePort, prop: str, keywords: list[str] | None, days: int
) -> KeywordRankings:
    """Never raises. A failing keyword becomes ``status="error"``, a failing list ``error``."""
    kws = clean_keywords(keywords)
    out = KeywordRankings(period_days=days, keywords_requested=len(kws))
    limited = False
    sem = asyncio.Semaphore(3)

    async def one(kw: str) -> KeywordRankRow:
        nonlocal limited
        async with sem:
            if limited:
                return KeywordRankRow(keyword=kw, status="error", error="rate-limited")
            try:
                rows = await client.search_analytics(
                    prop,
                    days=days,
                    dimensions=["query", "page"],
                    row_limit=PAGE_ROWS,
                    query=kw,
                    query_operator="contains",
                )
            except GscError as exc:
                if exc.code == "rate-limited":
                    limited = True
                return KeywordRankRow(keyword=kw, status="error", error=exc.label)
            except Exception as exc:  # noqa: BLE001  one keyword must not lose the others
                return KeywordRankRow(keyword=kw, status="error", error=f"internal-error: {type(exc).__name__}")
            return _aggregate(kw, rows)

    async def lists() -> None:
        try:
            rows = await client.search_analytics(prop, days=days, dimensions=["query"], row_limit=QUERY_ROWS)
        except GscError as exc:
            out.error = exc.label
            return
        except Exception as exc:  # noqa: BLE001
            out.error = f"internal-error: {type(exc).__name__}"
            return
        out.opportunities, out.low_ctr = opportunity_lists(rows)

    rank_rows, _ = await asyncio.gather(asyncio.gather(*(one(k) for k in kws)), lists())
    out.rows = list(rank_rows)
    return out
