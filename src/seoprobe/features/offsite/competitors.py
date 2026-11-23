"""
Competitor comparison: a light, side-by-side look at the entry page of up to three competitors
the user named.

Light on purpose: one fetch per competitor (no render, no LLM, no crawl), the same parser, page
rules and page score as for the user's own page, so the numbers are comparable with each other
(they are NOT the audit's score: that one also uses rendering and site-wide data). It produces
no findings: competitor data never changes the user's score.

What it cannot say: traffic, rankings, backlinks, or anything beyond the competitor's entry page.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal
from urllib.parse import urljoin, urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Category, FetchResult, Finding, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage, parse_html
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.scoring import page_score
from seoprobe.features.content import readability_scores, run_schema_checks, supported_language
from seoprobe.features.page import run_page_checks
from seoprobe.features.site import MAX_HTML, detect, header_directives, same_page

from .models import CompetitorComparison, CompetitorRow, SkippedCompetitor

MAX_COMPETITORS = 3
TECH_CAP = 8
SCORE_CATEGORIES = (
    Category.ONPAGE,
    Category.CONTENT,
    Category.TECHNICAL,
    Category.SCHEMA,
    Category.IMAGES,
)


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _canonical_state(parsed: ParsedPage, final_url: str) -> Literal["self", "other", "none"]:
    if not parsed.canonical:
        return "none"
    target = urljoin(final_url, parsed.canonical)
    return "self" if same_page(target, final_url) else "other"


def _noindex(parsed: ParsedPage, headers: dict[str, str]) -> bool:
    for name, _prop, content in parsed.meta_tags:
        if name.lower() in ("robots", "googlebot") and {t.strip().lower() for t in content.split(",")} & {
            "noindex",
            "none",
        }:
            return True
    xrt = headers.get("x-robots-tag", "")
    return bool(xrt) and bool(header_directives(xrt) & {"noindex", "none"})


def _links(parsed: ParsedPage, host: str) -> tuple[int, int]:
    internal = external = 0
    for link in parsed.links:
        if not link.resolved:
            continue
        if _host(link.resolved) == host:
            internal += 1
        else:
            external += 1
    return internal, external


def measure(
    url: str,
    *,
    status: int | None,
    final_url: str | None,
    headers: dict[str, str],
    html: str | None,
    parsed: ParsedPage | None,
    fetch: FetchResult,
    ttfb_ms: int | None,
    is_you: bool = False,
) -> CompetitorRow:
    """One row from what was fetched. ``fetch`` is the FetchResult (needed by the page rules)."""
    final = final_url or url
    row = CompetitorRow(
        url=url,
        host=_host(final) or _host(url),
        is_you=is_you,
        final_url=final_url,
        status=status,
        ttfb_ms=ttfb_ms,
        html_bytes=len(html.encode("utf-8", "ignore")) if html else None,
    )
    if parsed is None or status is None or status >= 400:
        return row
    row.title_len = len(parsed.title.strip())
    row.meta_len = len(parsed.meta_description.strip())
    row.h1_count = len(parsed.h1)
    row.words = parsed.word_count
    row.schema_types = sorted(set(parsed.schema_types))[:12]
    row.images = len(parsed.images)
    if parsed.images:
        row.images_alt_pct = round(100 * sum(1 for i in parsed.images if i.has_alt_attr) / len(parsed.images), 1)
    row.canonical = _canonical_state(parsed, final)
    row.noindex = _noindex(parsed, headers)
    row.internal_links, row.external_links = _links(parsed, row.host)
    metas = [(n.lower(), c) for n, _p, c in parsed.meta_tags if n]
    stack = detect(headers, (html or "")[:MAX_HTML], metas)
    row.tech = [i.name for i in stack.items][:TECH_CAP]
    lang = supported_language(parsed.lang)
    text = parsed.main_text or parsed.visible_text
    if lang and text:
        r = readability_scores(text, lang)
        if r and r.get("score") is not None:
            row.readability_formula = str(r["formula"])
            row.readability_score = float(r["score"])
    findings: list[Finding] = []
    for out in (
        run_page_checks(fetch, parsed, lang=parsed.lang or None),
        run_schema_checks(parsed, final),
    ):
        findings += out.findings
    row.score = page_score(findings, SCORE_CATEGORIES, url=final)
    return row


def _normalize(raw: str) -> str | None:
    """``https://`` is added to a bare domain; anything that is not a plain web address is dropped."""
    value = raw.strip()
    if not value or len(value) > 300 or re.search(r"\s", value):
        return None
    if "://" not in value:
        if re.match(r"[a-z][a-z0-9+.\-]*:(?!\d)", value, re.I):  # javascript:, mailto:, ...
            return None
        value = "https://" + value
    try:
        p = urlparse(value)
        host = p.hostname or ""
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or "." not in host:
        return None
    return value


async def _fetch_row(ctx: Context, url: str) -> CompetitorRow:
    try:
        fr = await ctx.http.fetch(url)
    except Exception as exc:  # noqa: BLE001  one competitor failing must not stop the others
        ctx.log.warning("competitor fetch failed: %s", type(exc).__name__)
        return CompetitorRow(url=url, host=_host(url), error="unreachable")
    row = CompetitorRow(
        url=url,
        host=_host(fr.final_url or url),
        final_url=fr.final_url,
        status=fr.status,
        ttfb_ms=fr.ttfb_ms,
        error=fr.error,
    )
    if fr.error or fr.status is None:
        return row
    if fr.html is None:
        row.error = "not-html"
        return row
    parsed = await asyncio.to_thread(parse_html, fr.html, fr.final_url or url)
    return measure(
        url,
        status=fr.status,
        final_url=fr.final_url,
        headers=fr.headers,
        html=fr.html,
        parsed=parsed,
        fetch=fr,
        ttfb_ms=fr.ttfb_ms,
    )


@operation("competitors", output=CompetitorComparison, requires=Requirements(network=True), timeout_s=120.0)
async def competitors(
    ctx: Context,
    target: str | Page,
    *,
    competitors: list[str] | None = None,
    keywords: list[str] | None = None,
) -> Outcome[CompetitorComparison]:
    """
    Entry page of up to three competitors next to the user's own page.

    ``competitors``: URLs or bare domains. ``keywords`` is accepted for the audit's option set;
    the comparison does not use it.
    """
    raw = competitors or []
    if not any(str(x).strip() for x in raw):
        return Outcome(
            data=CompetitorComparison(),
            not_checked=[ctx.not_checked("competitors", "no-competitors")],
        )
    page = await ctx.page(target)
    own = _host(origin_of(page.effective_url) or page.url)
    data = CompetitorComparison()
    seen: set[str] = set()
    todo: list[str] = []
    for item in raw:
        url = _normalize(str(item))
        shown = str(item).strip()[:300]
        if url is None:
            data.skipped.append(SkippedCompetitor(url=shown, reason="invalid-url"))
        elif _host(url) == own:
            data.skipped.append(SkippedCompetitor(url=url, reason="same-site"))
        elif _host(url) in seen:
            data.skipped.append(SkippedCompetitor(url=url, reason="duplicate"))
        elif len(todo) >= MAX_COMPETITORS:
            data.skipped.append(SkippedCompetitor(url=url, reason="over-limit"))
        else:
            seen.add(_host(url))
            todo.append(url)
    if page.parsed is not None and page.status is not None:
        data.you = measure(
            page.url,
            status=page.status,
            final_url=page.final_url,
            headers=page.headers,
            html=page.html,
            parsed=page.parsed,
            fetch=page,
            ttfb_ms=page.ttfb_ms,
            is_you=True,
        )
    for url in todo:  # sequential: at most 3 requests, polite to every host
        data.rows.append(await _fetch_row(ctx, url))
    if not data.rows:
        return Outcome(data=data, not_checked=[ctx.not_checked("competitors", "no-valid-competitors")])
    return Outcome(data=data)
