"""``gsc.opportunities``: the four lists, thresholds, GA4 ranking and the operation plumbing."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from seoprobe.core.errors import ErrorCode
from seoprobe.core.result import Status
from seoprobe.features.search import gsc
from seoprobe.features.search import opportunities as opp
from seoprobe.features.search.gsc import SITES_URL
from seoprobe.features.search.models import Ga4LandingPage, Opportunities, OpportunityThresholds
from seoprobe.features.search.oauth import SCOPE_GA4_READ, SCOPE_GSC_READ, HttpResult, MemoryTokenStore
from tests.features.search.support import FakeGoogle, connected_store, gsc_session, use_google

SITE = "sc-domain:example.com"
E = "https://example.com"


def T(**over: Any) -> OpportunityThresholds:
    return OpportunityThresholds(**{**opp.default_thresholds(), **over})


def qp(q: str, page: str, imp: int, clk: int, pos: float) -> dict[str, Any]:
    return {"keys": [q, page], "impressions": imp, "clicks": clk, "ctr": clk / imp if imp else 0.0, "position": pos}


def pg(page: str, clk: int, imp: int = 1000, pos: float = 5.0) -> dict[str, Any]:
    return {"keys": [page], "impressions": imp, "clicks": clk, "ctr": clk / imp, "position": pos}


# ------------------------------------------------------------------ pure parts


def test_defaults_and_curve_come_from_data_files() -> None:
    d = opp.default_thresholds()
    assert d["min_impressions"] == 100 and d["striking_min_position"] == 8 and d["striking_max_position"] == 20
    source, points = opp.ctr_curve()
    assert "CTR" in source and points[0] == (1, 0.28) and points[-1][0] == 20


def test_expected_ctr_interpolates_and_is_flat_outside() -> None:
    assert opp.expected_ctr(1) == 0.28 and opp.expected_ctr(0.5) == 0.28
    assert opp.expected_ctr(1.5) == pytest.approx(0.22)
    assert opp.expected_ctr(25) == opp.expected_ctr(20) == 0.007


def test_striking_range_and_order() -> None:
    pairs = [
        opp._pair(qp("a", f"{E}/a", 500, 5, 9.0)),
        opp._pair(qp("b", f"{E}/b", 900, 5, 20.0)),
        opp._pair(qp("c", f"{E}/c", 900, 5, 7.9)),  # above the range
        opp._pair(qp("d", f"{E}/d", 90, 1, 12.0)),  # too few impressions
        opp._pair(qp("e", f"{E}/e", 900, 1, 20.5)),  # below the range
    ]
    out = opp.striking([p for p in pairs if p], T())
    assert [r.query for r in out] == ["b", "a"]
    assert [r.query for r in opp.striking([p for p in pairs if p], T(min_impressions=50))] == ["b", "a", "d"]


def test_low_ctr_against_the_curve() -> None:
    pairs = [
        opp._pair(qp("low", f"{E}/l", 1000, 50, 1.0)),  # 5% at #1, expected 28%: low
        opp._pair(qp("fine", f"{E}/f", 1000, 200, 1.0)),  # 20%: not below 14%
        opp._pair(qp("deep", f"{E}/d", 1000, 1, 15.0)),  # 0.1% at #15, expected 1%: low
    ]
    out = opp.low_ctr([p for p in pairs if p], T())
    assert [r.query for r in out] == ["low", "deep"]
    assert out[0].expected_ctr == 0.28 and out[0].missed_clicks == 230
    assert out[1].expected_ctr == 0.01 and out[1].missed_clicks == 9


def test_cannibalization_needs_two_pages_with_a_share() -> None:
    rows = [
        qp("shoes", f"{E}/a", 600, 30, 4.0),
        qp("shoes", f"{E}/b", 300, 6, 9.0),
        qp("shoes", f"{E}/c", 100, 1, 30.0),  # 10%: under the share, not listed
        qp("hats", f"{E}/h", 900, 10, 3.0),  # one page only
        qp("rare", f"{E}/r1", 40, 1, 5.0),
        qp("rare", f"{E}/r2", 40, 1, 5.0),  # too few impressions in total
    ]
    out = opp.cannibalization([p for p in (opp._pair(r) for r in rows) if p], T())
    assert len(out) == 1
    r = out[0]
    assert r.query == "shoes" and r.page == f"{E}/a" and r.impressions == 1000 and r.clicks == 37
    assert [p.page for p in r.pages] == [f"{E}/a", f"{E}/b"] and r.pages[1].share == 0.3
    assert r.position == pytest.approx((600 * 4 + 300 * 9 + 100 * 30) / 1000)


def test_declining_scales_the_previous_period() -> None:
    previous = [pg(f"{E}/a", 100), pg(f"{E}/b", 100), pg(f"{E}/c", 15), pg(f"{E}/gone", 40)]
    current = [pg(f"{E}/a", 40), pg(f"{E}/b", 90), pg(f"{E}/c", 0)]
    out = opp.declining(current, previous, 28, 28, T(), current_complete=True)
    assert [r.page for r in out] == [f"{E}/a", f"{E}/gone"]  # b: -10%; c: under 20 previous
    a = out[0]
    assert a.previous_clicks == 100 and a.clicks == 40 and a.change == -60 and a.change_ratio == -0.6
    # previous period twice as long: halved before comparing
    out = opp.declining(current, previous, 28, 56, T(), current_complete=True)
    assert [r.page for r in out] == [f"{E}/gone"]
    # a truncated current list: a missing page may just be beyond the row limit
    assert [r.page for r in opp.declining(current, previous, 28, 28, T(), current_complete=False)] == [f"{E}/a"]


def test_rank_by_value_sorts_by_revenue_then_key_events() -> None:
    rows = [qp("x", f"{E}/x", 900, 5, 10.0), qp("y", f"{E}/y?utm=1", 500, 5, 10.0), qp("z", f"{E}/z", 400, 5, 10.0)]
    data = opp.analyse(SITE, rows, [], [], days=28, compare_days=28, thresholds=T())
    assert [r.query for r in data.striking] == ["x", "y", "z"]
    ga = [Ga4LandingPage(page="/y", revenue=50.0, key_events=1), Ga4LandingPage(page="/z", key_events=3)]
    ranked = opp.rank_by_value(data, ga)
    assert ranked.sorted_by == "ga4-value"
    assert [r.query for r in ranked.striking] == ["y", "z", "x"]
    assert ranked.striking[0].ga4_revenue == 50.0 and ranked.striking[2].ga4_key_events == 0.0


# ------------------------------------------------------------------ operation


def _google(*, ga4: Any = None) -> FakeGoogle:
    end = date.today() - timedelta(days=3)

    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == SITES_URL:
            return HttpResult(200, {"siteEntry": []})
        if "runReport" in u:
            return ga4 or HttpResult(
                200,
                {
                    "rows": [
                        {
                            "dimensionValues": [{"value": "/z"}],
                            "metricValues": [{"value": v} for v in ("10", "5", "0.5", "4", "120.5")],
                        }
                    ]
                },
            )
        body = kw["json_body"]
        dims = body.get("dimensions")
        if dims == ["query", "page"]:
            rows = [
                qp("near", f"{E}/x", 900, 5, 11.0),
                qp("near2", f"{E}/z", 300, 2, 12.0),
                qp("brand", f"{E}/", 2000, 40, 1.2),  # 2% at ~#1: low CTR
                qp("shoes", f"{E}/a", 600, 30, 4.0),
                qp("shoes", f"{E}/b", 400, 6, 6.0),
            ]
            return HttpResult(200, {"rows": rows})
        if body["endDate"] == end.isoformat():
            return HttpResult(200, {"rows": [pg(f"{E}/a", 30)]})
        return HttpResult(200, {"rows": [pg(f"{E}/a", 100)]})  # previous period

    return FakeGoogle(h)


async def test_opportunities_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.opportunities(SITE, session=s)
    assert res.status is Status.OK and isinstance(res.data, Opportunities)
    d = res.data
    assert [r.query for r in d.striking] == ["near", "near2"]
    assert [r.query for r in d.low_ctr][0] == "brand" and d.low_ctr[0].expected_ctr is not None
    assert [r.query for r in d.cannibalization] == ["shoes"]
    assert [r.page for r in d.declining] == [f"{E}/a"] and d.declining[0].change == -70
    assert d.thresholds.min_impressions == 100 and d.ctr_curve_source and d.sorted_by == "search"
    by = {f.id: f for f in res.findings}
    assert set(by) == {"gsc-striking-distance", "gsc-low-ctr", "gsc-cannibalization", "gsc-declining-clicks"}
    assert by["gsc-striking-distance"].count == 2 and "position 8-20" in (by["gsc-striking-distance"].evidence or "")
    assert "impressions >= 100" in (by["gsc-low-ctr"].evidence or "") and by["gsc-low-ctr"].heuristic
    assert "30%" in (by["gsc-declining-clicks"].evidence or "")
    # three Search Analytics requests: query x page, pages now, pages in the previous period
    bodies = [c[2]["json_body"] for c in google.calls if "searchAnalytics" in c[1]]
    assert len(bodies) == 3 and all(b["rowLimit"] == 5000 for b in bodies)
    end = date.today() - timedelta(days=3)
    assert bodies[2]["endDate"] == (end - timedelta(days=28)).isoformat()
    assert (date.fromisoformat(bodies[2]["endDate"]) - date.fromisoformat(bodies[2]["startDate"])).days == 27
    out = json.loads(res.to_json())
    assert out["operation"] == "gsc.opportunities" and Opportunities.model_validate(out["data"]) == d


async def test_thresholds_are_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        d = (await gsc.opportunities(SITE, min_impressions=500, striking_max_position=11.5, session=s)).unwrap()
    assert d.thresholds.min_impressions == 500 and d.thresholds.striking_max_position == 11.5
    assert [r.query for r in d.striking] == ["near"]


async def test_ga4_ranking_and_its_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    store = connected_store()
    await store.set("default", {**(await store.get("default") or {}), "scope": f"{SCOPE_GSC_READ} {SCOPE_GA4_READ}"})
    async with gsc_session(store=store) as s:
        d = (await gsc.opportunities(SITE, ga4_property="123", session=s)).unwrap()
    assert d.ga4_property == "properties/123" and d.sorted_by == "ga4-value" and d.ga4_error is None
    assert [r.query for r in d.striking] == ["near2", "near"] and d.striking[0].ga4_revenue == 120.5

    async with gsc_session(store=connected_store()) as s:  # no Analytics scope: partial, search order
        res = await gsc.opportunities(SITE, ga4_property="properties/123", session=s)
    assert res.status is Status.PARTIAL and res.data is not None
    assert res.data.ga4_error == "scope-not-granted" and res.data.sorted_by == "search"

    use_google(monkeypatch, _google(ga4=HttpResult(403)))
    async with gsc_session(store=store) as s:
        res = await gsc.opportunities(SITE, ga4_property="123", session=s)
        bad = await gsc.opportunities(SITE, ga4_property="UA-1", session=s)
    assert res.status is Status.PARTIAL and res.data is not None and res.data.ga4_error == "no-permission"
    assert bad.error is not None and bad.error.code is ErrorCode.INVALID_INPUT
    assert bad.error.details["reason"] == "invalid-property"


async def test_opportunities_needs_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=MemoryTokenStore()) as s:
        res = await gsc.opportunities(SITE, session=s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED
    use_google(monkeypatch, FakeGoogle(lambda *_: HttpResult(429)))
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.opportunities(SITE, session=s)
    assert res.error is not None and res.error.code is ErrorCode.API_QUOTA_EXCEEDED
