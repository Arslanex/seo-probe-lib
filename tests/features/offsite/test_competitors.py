"""competitors: light comparison of the entry page of up to three competitors."""

from __future__ import annotations

import importlib
import json
from typing import Any

from seoprobe.core.models import FetchResult
from seoprobe.core.page import Page
from seoprobe.core.parse.html import parse_html
from seoprobe.core.rules.scoring import page_score
from seoprobe.core.session import Session
from seoprobe.features.content import readability_scores, run_schema_checks
from seoprobe.features.offsite import FEATURE, CompetitorComparison, competitors
from seoprobe.features.page import run_page_checks
from seoprobe.features.site import MAX_HTML, detect
from tests import fakes

SCORE_CATEGORIES = importlib.import_module("seoprobe.features.offsite.competitors").SCORE_CATEGORIES

HOST = "https://example.com"
GOOD = """<!doctype html><html lang="en"><head><title>Competitor home page title for tests</title>
<meta name="description" content="A description of a reasonable length that describes this page well enough.">
<link rel="canonical" href="https://rival.example/"><meta name="generator" content="WordPress 6.4">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Rival","url":"https://rival.example/"}</script>
</head><body><h1>Rival</h1><p>{text}</p><img src="a.png" alt="a"><img src="b.png">
<a href="/about">About</a><a href="https://other.example/">Other</a></body></html>""".replace(
    "{text}", "This is a sentence about our products. " * 30
)
MINE = "<!doctype html><html lang='en'><head><title>Mine</title></head><body><h1>Mine</h1><p>Short.</p></body></html>"
CTYPE = "text/html; charset=utf-8"
ENTRY = Page(
    url=f"{HOST}/", final_url=f"{HOST}/", status=200, headers={"content-type": CTYPE}, content_type=CTYPE, html=MINE
)


def _empty(status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {}, b""  # no content type, no body: html is None


async def _run(comps: list[Any], pages: dict[str, Any] | None = None, http: Any = None) -> tuple[Any, list[str]]:
    s = Session(http=http) if http is not None else fakes.session(pages or {})
    async with s:
        res = await competitors(ENTRY, competitors=comps, session=s)
    return res, s.http.requested


async def test_row_per_competitor_with_metrics_and_you_row() -> None:
    res, requested = await _run(
        ["https://rival.example/"], {"https://rival.example/": fakes.html_page(GOOD, headers={"Server": "nginx"})}
    )
    data = CompetitorComparison.model_validate(res.data.model_dump())
    assert res.findings == [] and res.not_checked == []  # competitors never affect the score
    assert requested == ["https://rival.example/"]
    (row,) = data.rows
    assert row.host == "rival.example" and row.status == 200 and row.score is not None
    assert row.h1_count == 1 and row.images == 2 and row.images_alt_pct == 50.0
    assert row.canonical == "self" and row.noindex is False
    assert row.internal_links == 1 and row.external_links == 1
    assert "Organization" in row.schema_types and row.words and row.words > 100
    assert any("WordPress" in n for n in row.tech)
    assert data.you is not None and data.you.is_you and data.you.h1_count == 1
    assert data.you.score is not None


async def test_scores_are_computed_the_same_way_for_both_sides() -> None:
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": fakes.html_page(MINE)})
    data = CompetitorComparison.model_validate(res.data.model_dump())
    assert data.you is not None and data.rows[0].score == data.you.score


async def test_no_competitors_is_not_checked_not_an_error() -> None:
    res, requested = await _run([])
    assert res.ok and res.data == CompetitorComparison() and requested == []
    assert [(n.check_id, n.reason_code) for n in res.not_checked] == [("competitors", "no-competitors")]


async def test_at_most_three_requests_and_skips_are_recorded() -> None:
    urls = [
        "https://a.example/",
        "https://a.example/other",  # same host: duplicate
        f"{HOST}/page",  # the audited site itself
        "javascript:alert(1)",
        "https://b.example/",
        "https://c.example/",
        "https://d.example/",  # over the limit
    ]
    res, requested = await _run(urls, {u: fakes.html_page(MINE) for u in urls})
    data = CompetitorComparison.model_validate(res.data.model_dump())
    assert len(requested) == 3
    assert [r.host for r in data.rows] == ["a.example", "b.example", "c.example"]
    reasons = {x.reason for x in data.skipped}
    assert reasons == {"duplicate", "same-site", "invalid-url", "over-limit"}


async def test_bare_domain_gets_https() -> None:
    _, requested = await _run(["rival.example"], {"https://rival.example": fakes.html_page(MINE)})
    assert requested == ["https://rival.example"]


async def test_error_status_and_rejected_target_keep_a_row_without_numbers() -> None:
    s = fakes.session({"https://gone.example/": _empty(404)}, errors={"https://private.example/": "target-rejected"})
    async with s:
        res = await competitors(ENTRY, competitors=["https://private.example/", "https://gone.example/"], session=s)
    data = CompetitorComparison.model_validate(res.data.model_dump())  # type: ignore[union-attr]
    rejected, gone = data.rows
    assert rejected.error == "target-rejected" and rejected.score is None
    assert gone.status == 404 and gone.score is None and gone.title_len is None


async def test_a_fetcher_that_raises_does_not_break_the_module() -> None:
    class Boom(fakes.FakeHttp):
        async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
            raise RuntimeError("secret detail")

    res, _ = await _run(["https://rival.example/"], http=Boom())
    assert res.data.rows[0].error == "unreachable"
    assert "secret" not in res.data.model_dump_json()


async def test_non_html_response_is_marked() -> None:
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": _empty()})
    assert res.data.rows[0].error == "not-html"


async def test_noindex_and_other_canonical_are_detected() -> None:
    page = (
        "<html><head><title>T</title><meta name='robots' content='noindex'>"
        "<link rel='canonical' href='https://elsewhere.example/'></head><body><h1>x</h1></body></html>"
    )
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": fakes.html_page(page)})
    row = res.data.rows[0]
    assert row.noindex is True and row.canonical == "other"
    assert parse_html(page, "https://rival.example/").title == "T"


def test_module_is_registered() -> None:
    assert competitors in FEATURE.operations and competitors.timeout_s == 120.0


async def test_only_skipped_competitors_is_no_valid_competitors() -> None:
    res, requested = await _run([f"{HOST}/x", "javascript:alert(1)"])
    assert requested == [] and res.data.you is not None and res.data.rows == []
    assert [n.reason_code for n in res.not_checked] == ["no-valid-competitors"]


async def test_entry_url_string_is_fetched() -> None:
    s = fakes.session({f"{HOST}/": fakes.html_page(MINE), "https://rival.example/": fakes.html_page(GOOD)})
    async with s:
        res = await competitors(f"{HOST}/", competitors=["rival.example/"], session=s)
    assert res.data is not None and res.data.you is not None and res.data.you.title_len == 4
    assert s.http.requested == [f"{HOST}/", "https://rival.example/"]


async def test_result_is_json_serializable_and_has_no_findings() -> None:
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": fakes.html_page(GOOD)})
    body = json.loads(res.to_json())
    assert body["operation"] == "competitors" and body["findings"] == []
    assert body["data"]["rows"][0]["host"] == "rival.example"


async def test_short_page_readability_is_scored_as_the_source() -> None:
    # The source scored readability on any text (no 50-word minimum as in content.readability).
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": fakes.html_page(MINE)})
    parsed = parse_html(MINE, "https://rival.example/")
    expected = readability_scores(parsed.main_text or parsed.visible_text, "en")
    assert expected is not None
    for row in (res.data.rows[0], res.data.you):
        assert row.readability_formula == "flesch" and row.readability_score == float(expected["score"])


async def test_you_row_techstack_reads_the_raw_entry_html() -> None:
    # As the source: the raw entry HTML for the markers, the (rendered) parsed page for the metas.
    raw = MINE.replace("</body>", "<script src='/wp-content/themes/t/app.js'></script></body>")
    rendered = MINE  # the marker is in the raw HTML only
    entry = ENTRY.model_copy(update={"html": raw}).with_rendered(rendered)
    s = fakes.session({"https://rival.example/": fakes.html_page(MINE)})
    async with s:
        res = await competitors(entry, competitors=["https://rival.example/"], session=s)
    you = res.data.you
    assert you is not None
    metas = [(n.lower(), c) for n, _p, c in entry.parsed.meta_tags if n]  # type: ignore[union-attr]
    expected = [i.name for i in detect(entry.headers, raw[:MAX_HTML], metas).items][:8]
    assert you.tech == expected and "WordPress" in you.tech
    assert "WordPress" not in [i.name for i in detect(entry.headers, rendered, metas).items]
    assert you.html_bytes == len(raw.encode())


async def test_score_is_always_computed_as_the_source() -> None:
    res, _ = await _run(["https://rival.example/"], {"https://rival.example/": fakes.html_page(GOOD)})
    row = res.data.rows[0]
    parsed = parse_html(GOOD, "https://rival.example/")
    fr = await fakes.FakeHttp({"https://rival.example/": fakes.html_page(GOOD)}).fetch("https://rival.example/")
    findings = [
        *run_page_checks(fr, parsed, lang="en").findings,
        *run_schema_checks(parsed, "https://rival.example/").findings,
    ]
    assert row.score == page_score(findings, SCORE_CATEGORIES, url="https://rival.example/")
