"""
Indexability table: can this page be indexed, and if not, why?

Pure function over what the audit already fetched.
It answers the *measurable* half of "why does my site not show up": is anything on the page or
in the response blocking indexing? It does NOT say whether a page *is* indexed (that needs
Search Console).

Reasons (a page with any of them is not indexable):
``noindex`` (robots/googlebot meta), ``noindex-header`` (X-Robots-Tag), ``canonical-elsewhere``,
``blocked-by-robots``, ``status-4xx``, ``status-5xx``, ``fetch-error``.

A canonical that differs from the page only by trailing slash, ``www.`` or scheme names the
same page and is not a problem.
"""

from __future__ import annotations

from urllib.parse import urljoin

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.urls import url_key
from seoprobe.features.site import RobotsReport, RobotsRules, can_fetch, header_directives, meta_noindex, same_page

from ..models import IndexabilityRow, IndexabilityTable, LinkSummary, PageLinkStats


def build_indexability(
    pages: list[Page],
    *,
    sitemap_urls: set[str],
    link_stats: dict[str, PageLinkStats],
    robots_rules: RobotsRules | None,
    bot_name: str,
) -> list[IndexabilityRow]:
    sm_keys = {url_key(u) for u in sitemap_urls}
    stats_by_key = {url_key(u): s for u, s in link_stats.items()}
    rows: list[IndexabilityRow] = []
    for p in pages:
        reasons: list[str] = []
        if p.error or p.status is None:
            reasons.append("fetch-error")
        elif p.status >= 500:
            reasons.append("status-5xx")
        elif p.status >= 400:
            reasons.append("status-4xx")
        if meta_noindex(p):
            reasons.append("noindex")
        xrt = p.headers.get("x-robots-tag", "")
        if xrt and header_directives(xrt) & {"noindex", "none"}:
            reasons.append("noindex-header")
        canonical: str | None = None
        if p.parsed is not None and p.parsed.canonical:
            canonical = urljoin(p.effective_url, p.parsed.canonical)
            if not (same_page(canonical, p.effective_url) or same_page(canonical, p.url)):
                reasons.append("canonical-elsewhere")
        if robots_rules is not None and not can_fetch(robots_rules, bot_name or "*", p.url):
            reasons.append("blocked-by-robots")
        st = link_stats.get(p.url) or stats_by_key.get(url_key(p.url))
        rows.append(
            IndexabilityRow(
                url=p.url,
                status=p.status,
                indexable=not reasons,
                reasons=reasons,
                canonical=canonical,
                in_sitemap=((url_key(p.url) in sm_keys or url_key(p.effective_url) in sm_keys) if sm_keys else None),
                inlinks=st.inlinks if st else None,
                depth=st.depth if st else None,
            )
        )
    return rows


@operation("indexability", output=IndexabilityTable, requires=Requirements(network=False), timeout_s=30.0)
async def indexability(
    ctx: Context,
    pages: list[Page],
    *,
    links: LinkSummary | None = None,
    sitemap_urls: list[str] | None = None,
    robots: RobotsReport | None = None,
) -> Outcome[IndexabilityTable]:
    """Can each sampled page be indexed, and if not, why (no network)."""
    rows = build_indexability(
        pages,
        sitemap_urls=set(sitemap_urls or ()),
        link_stats={p.url: p for p in links.pages} if links else {},
        robots_rules=robots.rules if robots else None,
        bot_name=ctx.config.bot_name,
    )
    return Outcome(data=IndexabilityTable(items=rows))
