"""Indexability table: one test per reason, a clean page, equivalent canonicals, lookups."""

from __future__ import annotations

import json

import pytest

from seoprobe.core.page import Page
from seoprobe.features.crawl import LinkSummary, PageLinkStats, indexability
from seoprobe.features.crawl.links import build_indexability
from seoprobe.features.site import RobotsReport, header_directives, parse_robots
from tests import fakes
from tests.features.crawl.support import with_parsed

HOST = "https://example.com"


def page(
    path: str = "/p",
    head: str = "",
    *,
    status: int | None = 200,
    headers: dict[str, str] | None = None,
    final: str | None = None,
    error: str | None = None,
) -> Page:
    url = f"{HOST}{path}"
    html = f"<html><head><title>t</title>{head}</head><body><main>x</main></body></html>"
    return Page(
        url=url,
        final_url=final or url,
        status=status,
        headers=headers or {},
        content_type="text/html",
        html=html,
        error=error,
    )


def row(p: Page, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("sitemap_urls", set())
    kw.setdefault("link_stats", {})
    kw.setdefault("robots_rules", None)
    kw.setdefault("bot_name", "SEOAnalyzerBot/0.1")
    return build_indexability([p], **kw)[0]


def test_clean_page_is_indexable() -> None:
    r = row(page(head=f'<link rel="canonical" href="{HOST}/p">'))
    assert r.indexable and r.reasons == [] and r.status == 200 and r.canonical == f"{HOST}/p"


@pytest.mark.parametrize(
    "meta",
    [
        '<meta name="robots" content="noindex">',
        '<meta name="robots" content="NOINDEX, nofollow">',
        '<meta name="googlebot" content="noindex">',
        '<meta name="robots" content="none">',
    ],
)
def test_noindex_meta(meta: str) -> None:
    r = row(page(head=meta))
    assert not r.indexable and r.reasons == ["noindex"]


def test_robots_meta_that_allows_indexing_is_fine() -> None:
    assert row(page(head='<meta name="robots" content="index, follow">')).indexable
    assert row(page(head='<meta name="bingbot" content="noindex">')).indexable  # not Google


@pytest.mark.parametrize(
    ("value", "blocks"),
    [
        ("noindex", True),
        ("noindex, nofollow", True),
        ("googlebot: noindex", True),
        ("none", True),
        ("bingbot: noindex", False),
        ("nofollow", False),
        ("max-snippet:20", False),
        ("unavailable_after: 25 Jun 2030 15:00:00 PST", False),
        ("googlebot: nofollow, noindex", True),  # the second token still belongs to googlebot
        ("bingbot: noindex, googlebot: index", False),
        ("noindex, bingbot: index", True),  # the general directive came first
    ],
)
def test_x_robots_tag(value: str, blocks: bool) -> None:
    r = row(page(headers={"x-robots-tag": value}))
    assert (r.reasons == ["noindex-header"]) is blocks and r.indexable is (not blocks)


def test_header_directives_parser() -> None:
    assert header_directives("noindex, googlebot: nofollow, bingbot: noindex") == {
        "noindex",
        "nofollow",
    }
    assert header_directives("") == set()


def test_canonical_elsewhere() -> None:
    r = row(page(head='<link rel="canonical" href="https://example.com/other">'))
    assert not r.indexable and r.reasons == ["canonical-elsewhere"] and r.canonical == f"{HOST}/other"


@pytest.mark.parametrize(
    "href",
    [
        f"{HOST}/p/",  # trailing slash
        "https://www.example.com/p",  # www counterpart
        "http://example.com/p",  # scheme only
        "/p",  # relative, self
        f"{HOST}/p#section",
        f"{HOST}/p?utm_source=x",  # tracking parameter
    ],
)
def test_canonicals_that_name_the_same_page_are_fine(href: str) -> None:
    r = row(page(head=f'<link rel="canonical" href="{href}">'))
    assert r.indexable, r.reasons


def test_canonical_matches_the_final_url_after_a_redirect() -> None:
    p = page("/old", head=f'<link rel="canonical" href="{HOST}/new">', final=f"{HOST}/new")
    assert row(p).indexable


def test_canonical_with_a_different_query_is_elsewhere() -> None:
    r = row(page(head=f'<link rel="canonical" href="{HOST}/p?id=2">'))
    assert r.reasons == ["canonical-elsewhere"]


def test_blocked_by_robots() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /p\n")
    r = row(page("/p"), robots_rules=rules)
    assert r.reasons == ["blocked-by-robots"] and not r.indexable
    assert row(page("/q"), robots_rules=rules).indexable


def test_robots_rule_for_a_named_bot_applies() -> None:
    rules = parse_robots("User-agent: SEOAnalyzerBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert row(page(), robots_rules=rules).reasons == ["blocked-by-robots"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [(404, "status-4xx"), (410, "status-4xx"), (500, "status-5xx"), (503, "status-5xx")],
)
def test_status_reasons(status: int, reason: str) -> None:
    r = row(page(status=status))
    assert r.reasons == [reason] and not r.indexable and r.status == status


def test_fetch_error() -> None:
    r = row(Page(url=f"{HOST}/x", final_url="", status=None, error="timeout"))
    assert r.reasons == ["fetch-error"] and not r.indexable and r.status is None


def test_several_reasons_are_all_listed() -> None:
    # the source test parsed the 404 page too (an audit never does: Page.parsed is None for >= 400)
    r = row(
        with_parsed(
            page(
                head='<meta name="robots" content="noindex"><link rel="canonical" href="https://example.com/z">',
                status=404,
                headers={"x-robots-tag": "noindex"},
            ),
            extract_main_text=True,
        ),
        robots_rules=parse_robots("User-agent: *\nDisallow: /p\n"),
    )
    assert r.reasons == [
        "status-4xx",
        "noindex",
        "noindex-header",
        "canonical-elsewhere",
        "blocked-by-robots",
    ]


def test_sitemap_membership_and_link_stats() -> None:
    p = page("/p")
    stats = {p.url: PageLinkStats(url=p.url, inlinks=3, depth=2)}
    r = row(p, sitemap_urls={f"{HOST}/p/"}, link_stats=stats)  # key ignores the trailing slash
    assert r.in_sitemap is True and r.inlinks == 3 and r.depth == 2
    assert row(p, sitemap_urls={f"{HOST}/other"}).in_sitemap is False
    assert row(p).in_sitemap is None and row(p).inlinks is None and row(p).depth is None


def test_output_keeps_input_order_and_length() -> None:
    pages = [page("/c"), page("/a"), page("/b")]
    rows = build_indexability(pages, sitemap_urls=set(), link_stats={}, robots_rules=None, bot_name="")
    assert [r.url for r in rows] == [p.url for p in pages]


def test_page_without_parsed_html_does_not_crash() -> None:
    p = Page(url=f"{HOST}/x", final_url=f"{HOST}/x", status=200)
    r = row(p)
    assert r.indexable and r.canonical is None


# ----------------------------------------------------------------------------- the operation


async def test_indexability_operation_uses_link_stats_sitemap_and_robots_without_network() -> None:
    pages = [page("/p"), page("/q", head='<meta name="robots" content="noindex">')]
    summary = LinkSummary(pages=[PageLinkStats(url=f"{HOST}/p", inlinks=4, depth=1)])
    rb = RobotsReport(status="ok", rules=parse_robots("User-agent: *\nDisallow: /q\n"))
    async with fakes.session({}) as s:
        res = await indexability(pages, links=summary, sitemap_urls=[f"{HOST}/p"], robots=rb, session=s)
        assert s.http.requested == []  # type: ignore[attr-defined]
    assert res.ok and res.data is not None
    p, q = res.data.items
    assert p.indexable and p.inlinks == 4 and p.depth == 1 and p.in_sitemap is True
    assert q.reasons == ["noindex", "blocked-by-robots"] and q.in_sitemap is False and q.inlinks is None
    json.loads(res.to_json())
