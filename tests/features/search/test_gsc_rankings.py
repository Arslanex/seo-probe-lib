"""Keyword rankings from Search Console: collection and aggregation."""

from __future__ import annotations

from typing import Any

from seoprobe.features.search.gsc import SITES_URL, GscClient
from seoprobe.features.search.models import MAX_KEYWORDS, GscData, KeywordRankings
from seoprobe.features.search.oauth import HttpResult
from seoprobe.features.search.summary import (
    _aggregate,
    clean_keywords,
    collect_gsc,
    collect_rankings,
    opportunity_lists,
)
from tests.features.search.support import FakeGoogle, make_oauth

SITE = "sc-domain:example.com"
SITES = {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]}


def _row(q: str, page: str | None, imp: int, clk: int, pos: float) -> dict[str, Any]:
    keys = [q] if page is None else [q, page]
    return {"keys": keys, "impressions": imp, "clicks": clk, "ctr": clk / imp, "position": pos}


def _client(analytics: Any) -> tuple[GscClient, FakeGoogle]:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == SITES_URL:
            return HttpResult(200, SITES)
        if "searchAnalytics" in u:
            return analytics(kw["json_body"])  # type: ignore[no-any-return]
        if u.endswith("/sitemaps"):
            return HttpResult(200, {})
        return HttpResult(200, {"inspectionResult": {}})

    http = FakeGoogle(h)
    return GscClient(make_oauth(http, connected=True), http=http), http


def _filter(body: dict[str, Any]) -> str | None:
    groups = body.get("dimensionFilterGroups")
    return groups[0]["filters"][0]["expression"] if groups else None


# --------------------------------------------------------------------------- pure parts


def test_clean_keywords_dedupes_trims_and_caps() -> None:
    kws = clean_keywords(["  Widgets ", "widgets", "", "a  b"] + [f"k{i}" for i in range(20)])
    assert kws[:2] == ["Widgets", "a b"] and len(kws) == MAX_KEYWORDS
    assert clean_keywords(None) == []


def test_aggregate_prefers_exact_query_and_weights_position() -> None:
    rows = [
        _row("Widgets", "https://e.com/a", 100, 10, 4.0),
        _row("widgets", "https://e.com/b", 300, 30, 8.0),
        _row("buy widgets", "https://e.com/c", 9000, 90, 2.0),
    ]
    r = _aggregate("widgets", rows)
    assert r.status == "ok" and r.matched_query is None
    assert r.impressions == 400 and r.clicks == 40 and r.position == 7.0
    assert r.top_page == "https://e.com/b" and r.ctr == 0.1


def test_aggregate_falls_back_to_containing_query_and_names_it() -> None:
    rows = [
        _row("buy widgets", "https://e.com/c", 50, 1, 9.0),
        _row("big widgets", "https://e.com/d", 80, 2, 12.0),
    ]
    r = _aggregate("widgets", rows)
    assert r.matched_query == "big widgets" and r.impressions == 80


def test_aggregate_empty_is_no_data() -> None:
    assert _aggregate("x", []).status == "no-data"
    assert _aggregate("x", [{"keys": ["only-query"]}]).status == "no-data"


def test_turkish_dotted_i_is_not_folded_into_a_match_by_accident() -> None:
    r = _aggregate("istanbul", [_row("İstanbul otel", "https://e.com/", 10, 1, 3.0)])
    assert r.matched_query == "İstanbul otel"


def test_opportunity_and_low_ctr_rules() -> None:
    rows = [
        _row("close", None, 500, 15, 6.0),  # opportunity, CTR 3% is fine
        _row("page2", None, 200, 1, 15.0),  # opportunity
        _row("tiny", None, 10, 0, 6.0),  # too few impressions
        _row("top", None, 400, 40, 1.5),  # fine CTR (10%)
        _row("bad-top", None, 1000, 20, 2.0),  # 2% at position 2: low
        _row("bad-mid", None, 600, 3, 7.0),  # 0.5% at position 7: low
        _row("deep", None, 900, 9, 40.0),  # far away: neither
    ]
    opps, low = opportunity_lists(rows)
    assert [o.query for o in opps] == ["bad-mid", "close", "page2"]
    assert [q.query for q in low] == ["bad-top", "bad-mid"]


# --------------------------------------------------------------------------- collection


async def test_collect_rankings_request_budget_and_filters() -> None:
    def analytics(body: dict[str, Any]) -> HttpResult:
        kw = _filter(body)
        if kw is None:
            return HttpResult(200, {"rows": [_row("close", None, 500, 5, 6.0)]})
        return HttpResult(200, {"rows": [_row(kw, "https://example.com/p", 100, 5, 3.0)]})

    c, http = _client(analytics)
    out = await collect_rankings(c, SITE, [f"kw{i}" for i in range(15)], 28)
    assert out.keywords_requested == MAX_KEYWORDS and len(out.rows) == MAX_KEYWORDS
    calls = [x for x in http.calls if "searchAnalytics" in x[1]]
    assert len(calls) == MAX_KEYWORDS + 1  # one per keyword + one opportunity query
    filtered = [x[2]["json_body"] for x in calls if "dimensionFilterGroups" in x[2]["json_body"]]
    assert all(b["dimensions"] == ["query", "page"] for b in filtered)
    assert all(b["dimensionFilterGroups"][0]["filters"][0]["operator"] == "contains" for b in filtered)
    assert [o.query for o in out.opportunities] == ["close"]


async def test_rate_limit_stops_remaining_keywords_without_crashing() -> None:
    def analytics(body: dict[str, Any]) -> HttpResult:
        kw = _filter(body)
        if kw == "k0":
            return HttpResult(429)
        return HttpResult(200, {"rows": []})

    c, http = _client(analytics)
    out = await collect_rankings(c, SITE, ["k0", "k1", "k2", "k3", "k4", "k5"], 28)
    by = {r.keyword: r for r in out.rows}
    assert by["k0"].status == "error" and by["k0"].error == "rate-limited"
    assert any(r.error == "rate-limited" for r in out.rows)
    assert all(r.status in ("error", "no-data") for r in out.rows)


async def test_failing_opportunity_list_keeps_keyword_rows() -> None:
    def analytics(body: dict[str, Any]) -> HttpResult:
        if _filter(body) is None:
            return HttpResult(500)
        return HttpResult(200, {"rows": [_row("kw", "https://example.com/", 10, 1, 5.0)]})

    c, _ = _client(analytics)
    out = await collect_rankings(c, SITE, ["kw"], 28)
    assert out.rows[0].status == "ok" and out.error == "http-500" and not out.opportunities


async def test_collect_gsc_attaches_rankings_and_works_without_keywords() -> None:
    def analytics(body: dict[str, Any]) -> HttpResult:
        if _filter(body) is not None:
            return HttpResult(200, {"rows": [_row("seo", "https://example.com/", 90, 9, 4.0)]})
        return HttpResult(200, {"rows": [_row("seo", None, 90, 9, 4.0)]})

    c, _ = _client(analytics)
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[], keywords=["seo"])
    assert d.status == "ok" and d.rankings is not None
    assert d.rankings.rows[0].status == "ok" and d.rankings.rows[0].position == 4.0
    d2 = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d2.rankings is not None and d2.rankings.keywords_requested == 0 and d2.rankings.rows == []


def test_old_gsc_blob_without_rankings_still_loads() -> None:
    d = GscData.model_validate_json('{"status": "ok", "period_days": 28}')
    assert d.rankings is None
    assert KeywordRankings.model_validate({}).rows == []
