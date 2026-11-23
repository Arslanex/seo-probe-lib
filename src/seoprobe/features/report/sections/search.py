"""
Report sections from Search Console: the property's data and keyword rankings (informational, never
scored). Query and page text comes from Search Console but is attacker-influenceable (anyone can
search for anything), so it goes through ``clean_text``.
"""

from __future__ import annotations

import re
from typing import Any

from seoprobe.core.rules.catalog import has_text, t
from seoprobe.features.audit import Audit
from seoprobe.features.search import (
    LIST_LIMIT,
    LOW_CTR_TOP3,
    LOW_CTR_TOP10,
    MAX_KEYWORDS,
    MIN_IMPRESSIONS,
    KeywordRankRow,
    RankingQuery,
)

from .. import models as s
from .contract import _c, _code, _day, _vis_error, not_measured

GSC_ROWS = 20


GSC_INSPECTIONS = 200


GSC_SITEMAPS = 50


_GSC_REASONS = {
    "not-connected": "gsc-not-connected",
    "not-configured": "gsc-not-configured",
    "property-not-found": "gsc-property-not-found",
    "no-permission": "gsc-no-permission",
    "error": "gsc-error",
}


def _gsc_row(r: Any) -> s.GscMetricRow:
    return s.GscMetricRow(
        key=_c(r.key, 2048),
        clicks=int(r.clicks),
        impressions=int(r.impressions),
        ctr_pct=round(float(r.ctr) * 100, 1),
        position=round(float(r.position), 1),
    )


def _same_canonical(a: str, b: str) -> bool:
    return a.strip().rstrip("/") == b.strip().rstrip("/")


def _inspection_row(i: Any, lang: str) -> s.GscInspectionRow:
    g, u = i.google_canonical, i.user_canonical
    code = _code(i.error)
    return s.GscInspectionRow(
        url=_c(i.url, 2048),
        indexed=i.indexed,
        coverage_state=_c(i.coverage_state, 120) if i.coverage_state else None,
        verdict=_c(i.verdict, 40) if i.verdict else None,
        google_canonical=_c(g, 2048) if g else None,
        user_canonical=_c(u, 2048) if u else None,
        canonical_mismatch=bool(g and u and not _same_canonical(g, u)),
        last_crawl=_day(i.last_crawl_time) if i.last_crawl_time else None,
        crawled_as=_c(i.crawled_as, 40) if i.crawled_as else None,
        robots_state=_c(i.robots_txt_state, 60) if i.robots_txt_state else None,
        fetch_state=_c(i.page_fetch_state, 60) if i.page_fetch_state else None,
        error=code,
        error_text=_vis_error(lang, i.error) if code else None,
    )


def gsc_section(rec: Audit, lang: str) -> s.GscSection:
    g = rec.gsc
    status = g.status if g is not None else "not-connected"
    if g is None or status != "ok":
        nm = not_measured(lang, _GSC_REASONS[status])
        if status == "error" and g is not None and g.error:
            nm["not_measured_text"] = f"{nm['not_measured_text']} ({_c(_code(g.error) or '', 60)})"
        return s.GscSection(
            gsc_status=status,
            can_connect=status == "not-connected",
            account=_c(g.account, 254) if g is not None and g.account else None,
            property=_c(g.property, 2048) if g is not None and g.property else None,
            **nm,
        )
    ins = [_inspection_row(i, lang) for i in g.inspections[:GSC_INSPECTIONS]]
    return s.GscSection(
        status="measured",
        gsc_status="ok",
        account=_c(g.account, 254) if g.account else None,
        property=_c(g.property, 2048) if g.property else None,
        permission=_c(g.permission, 40) if g.permission else None,
        period_days=g.period_days,
        totals={_c(k, 20): float(v) for k, v in list(g.totals.items())[:8]},
        top_queries=[_gsc_row(r) for r in g.top_queries[:GSC_ROWS]],
        top_pages=[_gsc_row(r) for r in g.top_pages[:GSC_ROWS]],
        inspections=ins,
        inspections_total=len(g.inspections),
        indexed_count=sum(1 for i in g.inspections if i.indexed is True),
        not_indexed_count=sum(1 for i in g.inspections if i.indexed is False),
        mismatch_count=sum(1 for i in ins if i.canonical_mismatch),
        sitemaps=[
            s.GscSitemapRow(
                path=_c(m.path, 2048),
                last_submitted=_day(m.last_submitted) if m.last_submitted else None,
                last_downloaded=_day(m.last_downloaded) if m.last_downloaded else None,
                pending=m.is_pending,
                errors=m.errors,
                warnings=m.warnings,
            )
            for m in g.sitemaps[:GSC_SITEMAPS]
        ],
        note=t(lang, "gsc.note"),
        delay_note=t(lang, "gsc.delay", days=g.period_days),
        truncated=len(g.inspections) > GSC_INSPECTIONS
        or len(g.top_queries) > GSC_ROWS
        or len(g.top_pages) > GSC_ROWS
        or len(g.sitemaps) > GSC_SITEMAPS,
    )


ROWS = 20


_CODE = re.compile(r"^[a-z0-9][a-z0-9 _:-]{0,40}$")


def _err(lang: str, code: str | None) -> str:
    code = code or ""
    key = f"rank.error.{code}"
    if has_text(key):
        return t(lang, key)
    return code if _CODE.match(code) else ""


def _row(lang: str, days: int, r: KeywordRankRow) -> s.RankingRow:
    if r.status == "ok":
        text = t(lang, "rank.status.ok")
    elif r.status == "no-data":
        text = t(lang, "rank.status.no-data", days=days)
    else:
        text = f"{t(lang, 'rank.status.error')}: {_err(lang, r.error)}".rstrip(": ")
    return s.RankingRow(
        keyword=_c(r.keyword, 80),
        matched_query=_c(r.matched_query, 200) if r.matched_query else None,
        impressions=r.impressions,
        clicks=r.clicks,
        ctr_pct=round(r.ctr * 100, 1),
        position=round(r.position, 1) if r.position is not None else None,
        top_page=_c(r.top_page, 2048) if r.top_page else None,
        status=r.status,
        status_text=text,
    )


def _q(r: RankingQuery) -> s.RankingQueryRow:
    return s.RankingQueryRow(
        query=_c(r.query, 200),
        impressions=r.impressions,
        clicks=r.clicks,
        ctr_pct=round(r.ctr * 100, 1),
        position=round(r.position, 1),
    )


def rankings_section(rec: Audit, lang: str) -> s.RankingsSection:
    g = rec.gsc
    if g is None or g.status != "ok":
        reason = (
            "rankings-gsc-required"
            if g is None or g.status in ("not-connected", "not-configured")
            else _GSC_REASONS[g.status]
        )
        return s.RankingsSection(**not_measured(lang, reason))
    rk = g.rankings
    if rk is None:
        return s.RankingsSection(**not_measured(lang, "rankings-not-run"))
    if not rk.rows and not rk.opportunities and not rk.low_ctr:
        return s.RankingsSection(**not_measured(lang, "rankings-no-keywords"))
    return s.RankingsSection(
        status="measured",
        period_days=rk.period_days,
        keywords_requested=rk.keywords_requested,
        rows=[_row(lang, rk.period_days, r) for r in rk.rows[:MAX_KEYWORDS]],
        opportunities=[_q(r) for r in rk.opportunities[:LIST_LIMIT]],
        low_ctr=[_q(r) for r in rk.low_ctr[:LIST_LIMIT]],
        keywords_note=t(lang, "rank.keywords.missing") if not rk.rows else "",
        list_error=t(lang, "rank.list.error", code=_err(lang, rk.error) or "error") if rk.error else "",
        note=t(lang, "rank.note", days=rk.period_days),
        rules_note=" ".join(
            [
                t(lang, "rank.opp.rule", min=MIN_IMPRESSIONS),
                t(
                    lang,
                    "rank.low.rule",
                    top3=round(LOW_CTR_TOP3 * 100, 1),
                    top10=round(LOW_CTR_TOP10 * 100, 1),
                    min=MIN_IMPRESSIONS,
                ),
            ]
        ),
    )
