"""Report sections for speed: the lab measurement, images, and real-user (CrUX) field data."""

from __future__ import annotations

from typing import Any, Literal

from seoprobe.core.rules.catalog import has_text, message, reason_text, t
from seoprobe.features.audit import Audit
from seoprobe.features.performance import FieldMetric

from .. import models as s
from .contract import _c, _code, _day, not_measured
from .crawl import DOMAINS

PERF_ROWS = 50


IMAGE_ROWS = 100


LIST_CAP = 30


# Core Web Vitals (web.dev) + Lighthouse guidance. (good <=, poor >). INP is field-only, not here.
THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp_ms": (2500, 4000),
    "cls": (0.1, 0.25),
    "fcp_ms": (1800, 3000),
    "ttfb_ms": (800, 1800),
    "tbt_ms": (200, 600),
}


def rate(metric: str, value: float | None) -> str | None:
    if value is None or metric not in THRESHOLDS:
        return None
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else ("needs-improvement" if value <= poor else "poor")


def _kb(n: int | float | None) -> float:
    return round((n or 0) / 1024, 1)


def _perf_error(lang: str, error: str | None) -> tuple[str | None, str | None]:
    code = _code(error)
    return code, (reason_text(lang, code) if code else None)


def performance_section(rec: Audit, lang: str) -> s.PerfSection:
    if not rec.perf:
        return s.PerfSection(**not_measured(lang, "lab-unavailable"))
    rows: list[s.PerfRow] = []
    ordered = sorted(rec.perf, key=lambda p: (p.url, p.device != "desktop"))
    ok = [p for p in ordered if p.error is None]
    for p in ordered[:PERF_ROWS]:
        code, text = _perf_error(lang, p.error)
        by_type = [
            s.TypeShare(type=_c(k, 20), count=int(v.get("count", 0)), kb=_kb(v.get("bytes", 0)))
            for k, v in sorted(p.by_type.items(), key=lambda kv: -int(kv[1].get("bytes", 0)))
        ]
        mobile = None
        if p.mobile is not None:
            mobile = {_c(k, 40): (v if isinstance(v, (bool, int, float)) else _c(v, 80)) for k, v in p.mobile.items()}
        metrics = {
            "ttfb_ms": p.ttfb_ms,
            "fcp_ms": p.fcp_ms,
            "lcp_ms": p.lcp_ms,
            "cls": p.cls,
            "tbt_ms": p.tbt_ms,
        }
        rows.append(
            s.PerfRow(
                url=_c(p.url, 2048),
                device=p.device,
                ttfb_ms=p.ttfb_ms,
                fcp_ms=p.fcp_ms,
                lcp_ms=p.lcp_ms,
                cls=p.cls,
                tbt_ms=p.tbt_ms,
                dom_content_loaded_ms=p.dom_content_loaded_ms,
                load_ms=p.load_ms,
                ratings={k: r for k, v in metrics.items() if (r := rate(k, v)) is not None},
                requests=p.total_requests,
                total_kb=_kb(p.total_bytes),
                html_kb=_kb(p.html_bytes),
                render_blocking=p.render_blocking,
                by_type=by_type,
                mobile=mobile,
                error=code,
                error_text=text,
            )
        )
    if not ok:
        code = next((c for c in (_code(p.error) for p in ordered) if c), "lab-unavailable")
        return s.PerfSection(**not_measured(lang, code))

    basis = [p for p in ok if p.device == "desktop"] or ok
    agg: dict[str, list[int]] = {}
    hosts: dict[str, list[int]] = {}
    for p in basis:
        for k, v in p.by_type.items():
            cur = agg.setdefault(_c(k, 20), [0, 0])
            cur[0] += int(v.get("count", 0))
            cur[1] += int(v.get("bytes", 0))
        for tp in p.third_party:
            host = _c(tp.get("host", ""), 253)
            if not host:
                continue
            cur = hosts.setdefault(host, [0, 0])
            cur[0] += int(tp.get("requests", 0))
            cur[1] += int(tp.get("bytes", 0))
    unc = list(dict.fromkeys(_c(u, 2048) for p in ok for u in p.uncompressed_text))
    nocache = list(dict.fromkeys(_c(u, 2048) for p in ok for u in p.no_cache_static))
    return s.PerfSection(
        status="measured",
        rows=rows,
        rows_total=len(ordered),
        by_type=[
            s.TypeShare(type=k, count=v[0], kb=_kb(v[1])) for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])
        ],
        third_party=[
            s.HostShare(host=h, requests=v[0], kb=_kb(v[1]))
            for h, v in sorted(hosts.items(), key=lambda kv: -kv[1][1])[:DOMAINS]
        ],
        uncompressed=unc[:LIST_CAP],
        no_cache=nocache[:LIST_CAP],
        lab_note=t(lang, "perf.lab"),
        truncated=len(ordered) > PERF_ROWS or len(unc) > LIST_CAP or len(nocache) > LIST_CAP,
    )


def _issue_text(check_id: str, lang: str) -> str:
    key = f"finding.{check_id}"
    return _c(message(key, lang) if has_text(key) else check_id, 200)


def images_section(rec: Audit, lang: str) -> s.ImagesSection:
    ok = [p for p in rec.perf if p.error is None]
    if not ok:
        code = next((c for c in (_code(p.error) for p in rec.perf) if c), "lab-unavailable")
        return s.ImagesSection(**not_measured(lang, code))
    basis = [p for p in ok if p.device == "desktop"] or ok
    seen: dict[tuple[str, str], Any] = {}
    for p in basis:
        for im in p.images:
            seen.setdefault((im.page_url, im.src), im)
    imgs = sorted(seen.values(), key=lambda i: -(i.bytes or 0))

    def size(w: int | None, h: int | None) -> str | None:
        return f"{w}x{h}" if w and h else None

    rows = [
        s.ImageRow(
            page_url=_c(i.page_url, 2048),
            src=_c(i.src, 2048),
            format=_c(i.served_format, 12) if i.served_format else None,
            kb=_kb(i.bytes) if i.bytes is not None else None,
            natural=size(i.natural_w, i.natural_h),
            displayed=size(i.displayed_w, i.displayed_h),
            category=i.category,
            above_fold=i.above_fold,
            lazy=i.lazy,
            has_alt=i.has_alt,
            issues=[_c(x, 60) for x in i.issues[:8]],
            issue_texts=[_issue_text(x, lang) for x in i.issues[:8]],
        )
        for i in imgs[:IMAGE_ROWS]
    ]
    fmt: dict[str, list[int]] = {}
    for i in imgs:
        cur = fmt.setdefault(_c(i.served_format or "unknown", 12), [0, 0])
        cur[0] += 1
        cur[1] += int(i.bytes or 0)
    return s.ImagesSection(
        status="measured",
        rows=rows,
        rows_total=len(imgs),
        total_kb=_kb(sum(int(i.bytes or 0) for i in imgs)),
        by_format=[
            s.FormatShare(format=k, count=v[0], kb=_kb(v[1])) for k, v in sorted(fmt.items(), key=lambda kv: -kv[1][1])
        ],
        thresholds_note=t(lang, "img.thresholds"),
        truncated=len(imgs) > IMAGE_ROWS,
    )


# Same Core Web Vitals thresholds as ``integrations.psi.THRESHOLDS`` (+ web.dev values for FCP/TTFB).
FIELD_THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp": (2500.0, 4000.0),
    "inp": (200.0, 500.0),
    "cls": (0.1, 0.25),
    "fcp": (1800.0, 3000.0),
    "ttfb": (800.0, 1800.0),
}


_FIELD_ORDER = ("lcp", "inp", "cls", "fcp", "ttfb")


_FIELD_REASONS = {"no-data": "field-no-data", "no-api-key": "field-no-api-key"}


def _field_rating(metric: str, p75: float | None) -> Literal["good", "needs-improvement", "poor"] | None:
    if p75 is None or metric not in FIELD_THRESHOLDS:
        return None
    good, poor = FIELD_THRESHOLDS[metric]
    return "good" if p75 <= good else ("needs-improvement" if p75 <= poor else "poor")


def _pct(v: float | None) -> float | None:
    return None if v is None else round(max(0.0, min(1.0, float(v))) * 100, 1)


def _field_value(metric: str, p75: float | None) -> str:
    if p75 is None:
        return "—"
    return f"{p75:.2f}" if metric == "cls" else f"{round(p75)} ms"


def field_section(rec: Audit, lang: str) -> s.FieldSection:
    f = rec.field
    if f is None:
        return s.FieldSection(**not_measured(lang, "field-not-run"))
    if not f.metrics:
        code = _code(f.error) or "no-data"
        reason = _FIELD_REASONS.get(code, "field-error")
        nm = not_measured(lang, reason)
        if reason == "field-error" and f.error:
            nm["not_measured_text"] = f"{nm['not_measured_text']} ({_c(code, 60)})"
        return s.FieldSection(**nm)
    by: dict[str, FieldMetric] = {m.metric: m for m in f.metrics}
    rows = []
    for key in _FIELD_ORDER:
        m = by.get(key)
        if m is None:
            continue
        rows.append(
            s.FieldMetricRow(
                metric=m.metric,
                label=t(lang, f"field.m.{m.metric}"),
                p75=m.p75,
                p75_text=_field_value(m.metric, m.p75),
                rating=_field_rating(m.metric, m.p75) or m.rating,
                good_pct=_pct(m.good_share),
                needs_improvement_pct=_pct(m.needs_improvement_share),
                poor_pct=_pct(m.poor_share),
            )
        )
    period = f.period or {}
    return s.FieldSection(
        status="measured",
        scope=f.scope,
        form_factor=_c(f.form_factor, 20) if f.form_factor else None,
        period_first=_day(period["first"]) if period.get("first") else None,
        period_last=_day(period["last"]) if period.get("last") else None,
        metrics=rows,
        note=t(lang, "field.note"),
    )
