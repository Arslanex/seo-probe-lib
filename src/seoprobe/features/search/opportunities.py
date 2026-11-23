"""
Search Console opportunities: pure analysis of Search Analytics rows (``gsc.opportunities``).

Four lists, each deterministic for the same rows and thresholds (defaults in
``opportunities.json``, every one overridable by a keyword parameter of the operation):

* **striking**: (query, page) pairs at an average position between ``striking_min_position`` and
  ``striking_max_position`` with at least ``min_impressions`` impressions, most impressions first;
* **low_ctr**: pairs with at least ``min_impressions`` impressions whose CTR is below
  ``expected_ctr * low_ctr_ratio``; the expected CTR comes from the position curve in
  ``ctr-curve.json`` (heuristic: SERP features, brand and intent move real CTRs), most missed
  clicks first;
* **cannibalization**: queries with at least ``min_impressions`` impressions where at least
  ``cannibal_min_pages`` pages each hold ``cannibal_min_share`` of the impressions;
* **declining**: pages whose clicks fell from the previous period (``compare_days`` long, scaled
  to ``days``) by at least ``decline_min_drop_clicks`` and ``decline_min_ratio``, among pages with
  at least ``decline_min_previous_clicks`` previous clicks; largest drop first.

Positions are Search Console averages. Rows anonymised by Google never appear, and a request
that hits ``row_limit`` drops the rarest rows (``truncated``).
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.models import Finding

from .models import Ga4LandingPage, Opportunities, OpportunityPage, OpportunityRow, OpportunityThresholds


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


@cache
def default_thresholds() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("opportunities.json").read_text(encoding="utf-8"))
    return data


@cache
def ctr_curve() -> tuple[str, tuple[tuple[int, float], ...]]:
    """(source, ((position, expected CTR), ...)) from ``ctr-curve.json``."""
    data = json.loads(files(__package__).joinpath("ctr-curve.json").read_text(encoding="utf-8"))
    points = tuple(sorted((int(k), float(v)) for k, v in data["positions"].items()))
    return str(data["source"]), points


def expected_ctr(position: float, points: tuple[tuple[int, float], ...] | None = None) -> float:
    """Expected CTR at an average position: linear between curve points, flat outside them."""
    pts = points or ctr_curve()[1]
    if position <= pts[0][0]:
        return pts[0][1]
    for (p0, c0), (p1, c1) in zip(pts, pts[1:], strict=False):
        if position <= p1:
            return c0 + (c1 - c0) * (position - p0) / (p1 - p0)
    return pts[-1][1]


def _pair(r: dict[str, Any]) -> OpportunityRow | None:
    keys = r.get("keys")
    if not isinstance(keys, list) or len(keys) < 2:
        return None
    impressions = int(_num(r.get("impressions")))
    clicks = int(_num(r.get("clicks")))
    return OpportunityRow(
        query=str(keys[0]),
        page=str(keys[1]),
        position=round(_num(r.get("position")), 2),
        impressions=impressions,
        clicks=clicks,
        ctr=_num(r.get("ctr")) if r.get("ctr") is not None else (clicks / impressions if impressions else 0.0),
    )


def _by_volume(r: OpportunityRow) -> tuple[Any, ...]:
    return (-r.impressions, r.query or "", r.page or "")


def striking(pairs: list[OpportunityRow], t: OpportunityThresholds) -> list[OpportunityRow]:
    out = [
        r
        for r in pairs
        if t.striking_min_position <= r.position <= t.striking_max_position and r.impressions >= t.min_impressions
    ]
    return sorted(out, key=_by_volume)


def low_ctr(pairs: list[OpportunityRow], t: OpportunityThresholds) -> list[OpportunityRow]:
    out: list[OpportunityRow] = []
    for r in pairs:
        if r.impressions < t.min_impressions or r.position <= 0:
            continue
        exp = expected_ctr(r.position)
        if r.ctr < exp * t.low_ctr_ratio:
            missed = round((exp - r.ctr) * r.impressions)
            out.append(r.model_copy(update={"expected_ctr": round(exp, 4), "missed_clicks": missed}))
    return sorted(out, key=lambda r: (-(r.missed_clicks or 0), *_by_volume(r)))


def cannibalization(pairs: list[OpportunityRow], t: OpportunityThresholds) -> list[OpportunityRow]:
    by_query: dict[str, list[OpportunityRow]] = {}
    for r in pairs:
        by_query.setdefault(r.query or "", []).append(r)
    out: list[OpportunityRow] = []
    for query, rows in by_query.items():
        total = sum(r.impressions for r in rows)
        if total < t.min_impressions or total == 0:
            continue
        holders = [r for r in rows if r.impressions / total >= t.cannibal_min_share]
        if len(holders) < t.cannibal_min_pages:
            continue
        holders.sort(key=lambda r: (-r.impressions, r.page or ""))
        clicks = sum(r.clicks for r in rows)
        out.append(
            OpportunityRow(
                query=query,
                page=holders[0].page,
                position=round(sum(r.position * r.impressions for r in rows) / total, 2),
                impressions=total,
                clicks=clicks,
                ctr=clicks / total,
                pages=[
                    OpportunityPage(
                        page=r.page or "",
                        impressions=r.impressions,
                        clicks=r.clicks,
                        position=r.position,
                        share=round(r.impressions / total, 4),
                    )
                    for r in holders
                ],
            )
        )
    return sorted(out, key=_by_volume)


def declining(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    days: int,
    compare_days: int,
    t: OpportunityThresholds,
    *,
    current_complete: bool,
) -> list[OpportunityRow]:
    """
    Pages losing clicks. A page missing from ``current`` counts as 0 clicks only when the
    current request was not truncated (otherwise it may just be beyond ``row_limit``).
    """
    cur: dict[str, dict[str, Any]] = {}
    for r in current:
        keys = r.get("keys")
        if isinstance(keys, list) and keys:
            cur[str(keys[0])] = r
    scale = days / compare_days if compare_days else 1.0
    out: list[OpportunityRow] = []
    for r in previous:
        keys = r.get("keys")
        if not isinstance(keys, list) or not keys:
            continue
        page = str(keys[0])
        if page not in cur and not current_complete:
            continue
        before = _num(r.get("clicks")) * scale
        now = cur.get(page, {})
        clicks = int(_num(now.get("clicks")))
        drop = before - clicks
        if before < t.decline_min_previous_clicks or drop < t.decline_min_drop_clicks:
            continue
        if drop / before < t.decline_min_ratio:
            continue
        impressions = int(_num(now.get("impressions")))
        out.append(
            OpportunityRow(
                page=page,
                position=round(_num(now.get("position")), 2),
                impressions=impressions,
                clicks=clicks,
                ctr=clicks / impressions if impressions else 0.0,
                previous_clicks=round(before, 2),
                change=round(clicks - before, 2),
                change_ratio=round(-drop / before, 4),
            )
        )
    return sorted(out, key=lambda r: (r.change or 0.0, r.page or ""))


def analyse(
    prop: str,
    pairs_raw: list[dict[str, Any]],
    current_pages: list[dict[str, Any]],
    previous_pages: list[dict[str, Any]],
    *,
    days: int,
    compare_days: int,
    thresholds: OpportunityThresholds,
) -> Opportunities:
    pairs = [p for p in (_pair(r) for r in pairs_raw) if p is not None]
    t = thresholds
    lim = t.list_limit
    full = len(current_pages) < t.row_limit
    return Opportunities(
        property=prop,
        days=days,
        compare_days=compare_days,
        thresholds=t,
        ctr_curve_source=ctr_curve()[0],
        striking=striking(pairs, t)[:lim],
        low_ctr=low_ctr(pairs, t)[:lim],
        cannibalization=cannibalization(pairs, t)[:lim],
        declining=declining(current_pages, previous_pages, days, compare_days, t, current_complete=full)[:lim],
        rows_analysed=len(pairs_raw),
        truncated=max(len(pairs_raw), len(current_pages), len(previous_pages)) >= t.row_limit,
    )


def _path(url: str | None) -> str:
    return urlparse(url or "").path or "/"


def rank_by_value(data: Opportunities, pages: list[Ga4LandingPage]) -> Opportunities:
    """
    Attach GA4 key events / revenue of each row's landing page and sort by that value first
    (revenue, then key events); the search order breaks ties.
    """
    value = {p.page: p for p in pages}

    def attach(rows: list[OpportunityRow]) -> list[OpportunityRow]:
        out = []
        for i, r in enumerate(rows):
            lp = value.get(_path(r.page))
            out.append(
                (
                    -(lp.revenue if lp else 0.0),
                    -(lp.key_events if lp else 0.0),
                    i,
                    r.model_copy(
                        update={
                            "ga4_key_events": lp.key_events if lp else 0.0,
                            "ga4_revenue": lp.revenue if lp else 0.0,
                        }
                    ),
                )
            )
        return [x[3] for x in sorted(out, key=lambda x: x[:3])]

    return data.model_copy(
        update={
            "striking": attach(data.striking),
            "low_ctr": attach(data.low_ctr),
            "cannibalization": attach(data.cannibalization),
            "declining": attach(data.declining),
            "sorted_by": "ga4-value",
        }
    )


def _describe(r: OpportunityRow) -> str:
    who = f"'{r.query}' {r.page}" if r.query else str(r.page)
    return f"{who} pos {r.position}, {r.impressions} impressions, {r.clicks} clicks"


def findings(data: Opportunities, make: Any) -> list[Finding]:
    """One site-level finding per non-empty list; evidence = the top row and the thresholds."""
    t = data.thresholds
    out: list[Finding] = []
    if data.striking:
        out.append(
            make(
                "gsc-striking-distance",
                count=len(data.striking),
                evidence=f"top: {_describe(data.striking[0])}; threshold: position "
                f"{t.striking_min_position:g}-{t.striking_max_position:g}, impressions >= {t.min_impressions}",
            )
        )
    if data.low_ctr:
        r = data.low_ctr[0]
        out.append(
            make(
                "gsc-low-ctr",
                count=len(data.low_ctr),
                evidence=f"top: {_describe(r)}, CTR {r.ctr:.2%} vs expected {r.expected_ctr or 0:.2%}; threshold: "
                f"CTR < expected x {t.low_ctr_ratio:g}, impressions >= {t.min_impressions}",
                confidence="medium",
            )
        )
    if data.cannibalization:
        r = data.cannibalization[0]
        out.append(
            make(
                "gsc-cannibalization",
                count=len(data.cannibalization),
                evidence=f"top: '{r.query}' split over {len(r.pages)} pages ({r.impressions} impressions); "
                f"threshold: >= {t.cannibal_min_pages} pages with >= {t.cannibal_min_share:.0%} share each",
                confidence="medium",
            )
        )
    if data.declining:
        r = data.declining[0]
        out.append(
            make(
                "gsc-declining-clicks",
                count=len(data.declining),
                evidence=f"top: {r.page} {r.previous_clicks:g} -> {r.clicks} clicks ({(r.change_ratio or 0):.0%}); "
                f"threshold: drop >= {t.decline_min_drop_clicks:g} clicks and >= {t.decline_min_ratio:.0%}, "
                f"previous >= {t.decline_min_previous_clicks:g}",
            )
        )
    return out
