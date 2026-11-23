"""
Suggested sitemap.xml, site audits only.

Built from the sampled pages that are fit for a sitemap: HTTP 200, reached without a redirect
(the URL is the final URL), no ``noindex`` (meta robots/googlebot or ``X-Robots-Tag``), and a
canonical that names the page itself (or none). ``<lastmod>`` is written only when the server sent
a ``Last-Modified`` header: no date is invented. At most 50 000 URLs (the protocol limit).

The sample is a SAMPLE (at most the audit's page limit): the file lists what was seen, not the
whole site, and the comparison with the site's existing sitemap is limited the same way. Pages
blocked by robots.txt are NOT excluded here (the audit's own indexability table covers that).

No findings: this is data for the report (a copy-paste draft and a download).

The indexability helpers (``header_directives``, ``meta_noindex``, ``same_page``) are public:
the crawl feature's indexability table uses them too.
"""

from __future__ import annotations

from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import AuditMode, NotChecked, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import url_match_key
from seoprobe.core.parse.urls import url_key

from ..models import ExcludedPage, SitemapGenerated

MAX_URLS = 50_000
LIST_CAP = 100
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_VALUED_DIRECTIVES = frozenset({"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"})
_APPLIES = frozenset({"", "googlebot"})


def header_directives(value: str) -> set[str]:
    """
    Directives of an X-Robots-Tag value that apply to everyone or to Googlebot.

    ``"noindex, googlebot: nofollow, bingbot: noindex"`` -> ``{"noindex", "nofollow"}``.
    A token ``agent: directive`` switches the agent for the tokens that follow it.
    """
    agent = ""
    out: set[str] = set()
    for token in value.lower().split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            left, _, right = token.partition(":")
            left, right = left.strip(), right.strip()
            if left in _VALUED_DIRECTIVES:
                continue  # max-snippet:20 and friends carry a value, not an agent
            agent, token = left, right
        if agent in _APPLIES and token:
            out.add(token)
    return out


def meta_noindex(page: Page) -> bool:
    if page.parsed is None:
        return False
    for name, _prop, content in page.parsed.meta_tags:
        if name.lower() in ("robots", "googlebot"):
            tokens = {t.strip().lower() for t in content.split(",")}
            if tokens & {"noindex", "none"}:
                return True
    return False


def same_page(a: str, b: str) -> bool:
    """Equal after ignoring trailing slash, ``www.`` and scheme."""

    def norm(u: str) -> str:
        p = urlparse(url_match_key(u))
        host = p.hostname or ""
        host = host[4:] if host.startswith("www.") else host
        port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
        return f"{host}{port}{p.path}?{p.query}"

    return norm(a) == norm(b)


def _lastmod(headers: dict[str, str]) -> str | None:
    raw = headers.get("last-modified", "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


def exclusion_reason(p: Page) -> str | None:
    """Why this page does not belong in a sitemap; None when it does."""
    if p.error or p.status is None:
        return "fetch-error"
    if p.status >= 500:
        return "status-5xx"
    if p.status >= 400:
        return "status-4xx"
    if p.status != 200:
        return "redirected"
    if p.final_url and url_key(p.final_url) != url_key(p.url):
        return "redirected"
    if meta_noindex(p):
        return "noindex"
    xrt = p.headers.get("x-robots-tag", "")
    if xrt and header_directives(xrt) & {"noindex", "none"}:
        return "noindex-header"
    if p.parsed is not None and p.parsed.canonical:
        canonical = urljoin(p.effective_url, p.parsed.canonical)
        if not (same_page(canonical, p.effective_url) or same_page(canonical, p.url)):
            return "canonical-elsewhere"
    return None


def render_xml(entries: list[tuple[str, str | None]]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<urlset xmlns="{NS}">']
    for url, lastmod in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{escape(url)}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def build_sitemap(pages: list[Page], sitemap_urls: set[str]) -> SitemapGenerated:
    out = SitemapGenerated(existing_sitemap=bool(sitemap_urls))
    sm_keys = {url_key(u) for u in sitemap_urls}
    entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    excluded: list[ExcludedPage] = []
    not_indexable: list[ExcludedPage] = []
    missing: list[str] = []
    for p in pages:
        key = url_key(p.effective_url)
        if key in seen:
            continue
        seen.add(key)
        out.pages_in_sample += 1
        reason = exclusion_reason(p)
        in_sm = url_key(p.url) in sm_keys or key in sm_keys
        if reason is not None:
            excluded.append(ExcludedPage(url=p.url, reason=reason))
            if in_sm:
                not_indexable.append(ExcludedPage(url=p.url, reason=reason))
            continue
        if len(entries) >= MAX_URLS:
            out.truncated = True
            continue
        lm = _lastmod(p.headers)
        entries.append((p.effective_url, lm))
        if sm_keys and not in_sm:
            missing.append(p.effective_url)
    entries.sort(key=lambda e: e[0])
    out.xml = render_xml(entries)
    out.urls_total = len(entries)
    out.urls = [u for u, _ in entries[:LIST_CAP]]
    out.with_lastmod = sum(1 for _, lm in entries if lm)
    out.missing_total = len(missing)
    out.missing_from_sitemap = missing[:LIST_CAP]
    out.in_sitemap_not_indexable_total = len(not_indexable)
    out.in_sitemap_not_indexable = not_indexable[:LIST_CAP]
    out.excluded_total = len(excluded)
    out.excluded = excluded[:LIST_CAP]
    return out


@operation("generate_sitemap", output=SitemapGenerated, requires=Requirements(network=False), timeout_s=15.0)
async def generate_sitemap(
    ctx: Context,
    pages: list[Page],
    *,
    sitemap_urls: list[str] | None = None,
    mode: AuditMode = AuditMode.SITE,
) -> Outcome[SitemapGenerated]:
    """A suggested sitemap.xml from the sampled pages fit for one (site audits only)."""
    if mode != AuditMode.SITE:
        return Outcome(
            data=SitemapGenerated(),
            not_checked=[NotChecked(check_id="sitemap_generated", reason_code="site-mode-only")],
        )
    data = build_sitemap(pages, set(sitemap_urls or ()))
    if data.urls_total == 0:
        return Outcome(
            data=SitemapGenerated(),
            not_checked=[NotChecked(check_id="sitemap_generated", reason_code="no-eligible-pages")],
        )
    return Outcome(data=data)
