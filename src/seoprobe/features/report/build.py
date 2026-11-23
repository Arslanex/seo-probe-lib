"""
Audit -> AuditReport (``build_report``, a pure function: no I/O, no clock, no network), and the
``report.build`` and ``report.findings`` operations.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import SEVERITY_ORDER, AuditMode, Category, Finding, Layer, Requirements, Severity
from seoprobe.core.parse.text import clean_text
from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import norm_lang, reason_text, t
from seoprobe.features.audit import Audit, AuditStatus

from . import models as s
from . import sections
from .compare import HistoryInput, history_input, history_section
from .models import AuditReport, FindingList
from .plan import EFFORT_HOURS, build_action_plan, is_quick_win, priority_score
from .quality import check_report


def _msg(key: str, lang: str, fallback: str) -> str:
    if catalog.has_text(key):
        return catalog.message(key, lang)
    return fallback


def _title_of(body: str) -> str:
    first = body.split(". ")[0].strip()
    return first if len(first) <= 110 else first[:107].rstrip() + "…"


def _sev_rank(sev: Severity) -> int:
    return SEVERITY_ORDER.index(sev)


class _Group:
    def __init__(self, check_id: str, severity: Severity) -> None:
        self.check_id = check_id
        self.severity = severity
        self.items: list[Finding] = []

    @property
    def urls(self) -> list[str]:
        seen: dict[str, None] = {}
        for f in self.items:
            if f.url:
                seen.setdefault(f.url, None)
        return list(seen)


def _group_findings(findings: list[Finding]) -> list[_Group]:
    groups: dict[tuple[str, Severity], _Group] = {}
    for f in findings:
        groups.setdefault((f.id, f.severity), _Group(f.id, f.severity)).items.append(f)
    return sorted(
        groups.values(),
        key=lambda g: (_sev_rank(g.severity), -max(len(g.urls), 1), g.check_id),
    )


def _band(value: float | None) -> Literal["good", "attention", "poor"] | None:
    if value is None:
        return None
    return "good" if value >= 80 else "attention" if value >= 50 else "poor"


def _gsc_status(record: Audit) -> Literal["ok", "missing", "failed"]:
    g = record.gsc
    if g is not None and g.status == "ok":
        return "ok"
    return "failed" if g is not None and g.status == "error" else "missing"


def _crux_status(record: Audit) -> Literal["ok", "missing", "failed"]:
    f = record.field
    if f is not None and f.metrics:
        return "ok"
    code = (f.error or "").split(":")[0].strip() if f is not None else ""
    return "failed" if code and code not in ("no-data", "no-api-key") else "missing"


def _error_code(error: str | None) -> str | None:
    if not error:
        return None
    return error.split(":")[0].strip().split()[0] if error.strip() else None


def build_report(record: Audit, lang: str = "en", *, history: HistoryInput | None = None) -> s.AuditReport:
    lang = norm_lang(lang)
    sampled_pages = [p for p in record.pages if p.error is None and p.status is not None]
    n_sampled = len(sampled_pages) or int(record.coverage.get("pages_fetched") or 0) or len(record.pages)

    # ------------------------------------------------------------------ findings
    groups = _group_findings(record.findings)
    fnd_id = {(g.check_id, g.severity): f"fnd_{i + 1:04d}" for i, g in enumerate(groups)}
    rec_id = {(g.check_id, g.severity): f"rec_{i + 1:04d}" for i, g in enumerate(groups)}
    gate = record.scores.gate if record.scores else None

    findings: list[s.FindingReport] = []
    for g in groups:
        first = g.items[0]
        ev_src = next((f.evidence for f in g.items if f.evidence), None)
        ev = None
        if ev_src is not None:
            val = clean_text(ev_src)[:200]
            ev = s.Evidence(value=val, truncated=len(ev_src) >= 200)
        urls = g.urls
        count = len(urls) if urls else 1
        counts = [f.count for f in g.items if f.count is not None]
        findings.append(
            s.FindingReport(
                id=fnd_id[(g.check_id, g.severity)],
                check_id=g.check_id,
                rule_version=first.rule_version,
                category=first.category,
                layer=first.layer,
                severity=g.severity,
                severity_original=first.severity_original,
                severity_reason=first.severity_reason,
                scope=first.scope,
                url=first.url,
                urls=urls,
                affected=s.Affected(
                    count=count,
                    share_of_sampled_pages=(
                        round(count / n_sampled, 3) if first.scope == "page" and n_sampled else None
                    ),
                    examples=urls[:5],
                ),
                message_key=first.message_key,
                message=clean_text(_msg(first.message_key, lang, first.message)),
                heuristic=any(f.heuristic for f in g.items),
                evidence=ev,
                count=sum(counts) if counts else None,
                confidence=first.confidence,
                recommendation_ids=[rec_id[(g.check_id, g.severity)]],
                is_gate=bool(gate and gate.check_id == g.check_id),
            )
        )

    # ------------------------------------------------------------------ recommendations
    rec_by_check: dict[str, list[str]] = defaultdict(list)
    for g in groups:
        rec_by_check[g.check_id].append(rec_id[(g.check_id, g.severity)])

    recs: list[s.Recommendation] = []
    for g in groups:
        first = g.items[0]
        rule = catalog.rules().get(g.check_id)
        rkey = first.recommendation_key or (rule.recommendation_key if rule else f"rec.{g.check_id}")
        body = _msg(rkey, lang, _msg(first.message_key, lang, first.message))
        effort_level = first.effort or (rule.effort_default if rule else "M")
        impact_level = first.impact or (rule.impact_default if rule else "medium")
        lo, hi = EFFORT_HOURS[effort_level]
        deps: list[str] = []
        if rule:
            for dep_check in rule.depends_on:
                deps.extend(x for x in rec_by_check.get(dep_check, []) if x != rec_id[(g.check_id, g.severity)])
        fkey = rule.failure_check_key if rule else ""
        lkey = rule.leading_indicator_key if rule else ""
        recs.append(
            s.Recommendation(
                id=rec_id[(g.check_id, g.severity)],
                finding_ids=[fnd_id[(g.check_id, g.severity)]],
                check_ids=[g.check_id],
                title=_title_of(body),
                body=body,
                severity=g.severity,
                effort=s.Effort(level=effort_level, hours_min=lo, hours_max=hi),
                impact=s.Impact(level=impact_level),
                confidence=first.confidence,
                depends_on=sorted(set(deps)),
                priority_score=priority_score(impact_level, first.confidence, effort_level),
                failure_check=_msg(fkey, lang, "") or None if fkey else None,
                leading_indicator=_msg(lkey, lang, "") or None if lkey else None,
            )
        )

    gate_rec_ids = {rec_id[(g.check_id, g.severity)] for g in groups if gate and g.check_id == gate.check_id}
    plan, phase_of = build_action_plan(recs, gate_rec_ids, lang)
    for r in recs:
        r.phase = phase_of[r.id]

    # ------------------------------------------------------------------ scores
    sc = record.scores
    cats: list[s.CategoryReport] = []
    if sc is not None:
        measured_w = sum(c.weight for c in sc.categories if c.measured and c.score is not None)
        for c in sc.categories:
            ok = c.measured and c.score is not None
            penalties = []
            for p in c.penalties:
                key = next((k for k in fnd_id if k[0] == p.check_id), None)
                key = next((k for k in fnd_id if k == (p.check_id, p.severity)), key)
                penalties.append(
                    s.Penalty(
                        finding_id=fnd_id.get(key) if key else None,
                        check_id=p.check_id,
                        severity=p.severity,
                        points=p.points,
                    )
                )
            cats.append(
                s.CategoryReport(
                    id=c.category,
                    label=t(lang, f"cat.{c.category.value}"),
                    weight_default=c.weight,
                    weight_effective=round(c.weight / measured_w * 100, 1) if ok and measured_w else None,
                    status="measured" if ok else "not_measured",
                    score=round(c.score, 1) if ok and c.score is not None else None,
                    confidence=None,
                    penalties=penalties,
                    not_measured_reason=None if ok else (c.reason or "no-data"),
                    not_measured_text=None if ok else reason_text(lang, c.reason or "no-data"),
                    finding_ids=[fnd_id[(g.check_id, g.severity)] for g in groups if g.items[0].category == c.category],
                    checks=sections.check_rows(c.checks, lang) if ok else [],
                    checks_total=c.checks_total if ok else 0,
                    checks_passed=c.checks_passed if ok else 0,
                    checks_failed=c.checks_failed if ok else 0,
                )
            )
        insufficient = sc.insufficient_data or sc.overall is None
        gate_model = None
        if sc.gate:
            gkey = next((k for k in fnd_id if k[0] == sc.gate.check_id), None)
            gate_model = s.Gate(
                id=sc.gate.check_id,
                cap=sc.gate.cap,
                finding_id=fnd_id.get(gkey) if gkey else None,
                url=next((f.url for f in record.findings if f.id == sc.gate.check_id and f.url), None),
                message=clean_text(sc.gate.reason),
            )
        value = None if insufficient else round(float(sc.overall), 1)  # type: ignore[arg-type]
        overall = s.OverallScore(
            value=value,
            display="insufficient_data" if insufficient else "number",
            raw_value=None if insufficient or sc.raw_overall is None else round(sc.raw_overall, 1),
            coverage_pct=round(sc.coverage_pct, 1),
            confidence=sc.confidence,
            band=_band(value),
            gate=gate_model,
        )
        scores = s.Scores(
            kind="page_card" if record.mode == AuditMode.PAGE else "health",
            overall=overall,
            categories=cats,
            p25=sc.p25 if n_sampled >= 5 else None,
            core_overall=sc.core_overall,
        )
        trend = s.Trend(core_overall=sc.core_overall)
    else:
        scores = s.Scores(
            kind="page_card" if record.mode == AuditMode.PAGE else "health",
            overall=s.OverallScore(value=None, display="insufficient_data", coverage_pct=0, confidence="low"),
            categories=[],
        )
        trend = None

    # ------------------------------------------------------------------ not checked
    nc_seen: set[tuple[str, str, str | None]] = set()
    not_checked: list[s.NotCheckedReport] = []
    for n in record.not_checked:
        nkey = (n.check_id, n.reason_code, n.url)
        if nkey in nc_seen:
            continue
        nc_seen.add(nkey)
        not_checked.append(
            s.NotCheckedReport(
                check_id=n.check_id,
                reason_code=n.reason_code,
                reason=reason_text(lang, n.reason_code),
                url=n.url,
            )
        )

    # ------------------------------------------------------------------ pages / crawl
    pages = [
        s.PageSummary(
            url=p.url,
            final_url=p.final_url,
            status=p.status,
            role=p.sample_kind,
            template_key=p.template_key,
            title=clean_text(p.title)[:200] if p.title else None,
            page_score=p.page_score,
            rendered=p.rendered,
            error=p.error,
        )
        for p in record.pages
    ]
    cov = dict(record.coverage)
    templates = list(cov.pop("templates", []) or [])
    seed = cov.pop("sample_seed", None)
    crawl = s.CrawlInfo(
        limits={
            "max_pages_requested": record.budget.get("requested_pages"),
            "max_pages_applied": record.budget.get("max_pages"),
            "reason": record.budget.get("tier"),
        },
        coverage=cov,
        templates=templates,
        sample_seed=None if seed is None else str(seed),
    )

    # ------------------------------------------------------------------ limitations / sources
    codes = {n.reason_code for n in record.not_checked}
    perf = next((c for c in cats if c.id == Category.PERFORMANCE), None)
    planned, fetched = cov.get("pages_planned"), cov.get("pages_fetched")
    req, applied = record.budget.get("requested_pages"), record.budget.get("max_pages")
    tmpl_keys = {p.template_key for p in sampled_pages if p.template_key}
    lim_codes: list[tuple[str, dict[str, Any]]] = []
    if isinstance(planned, int) and isinstance(fetched, int) and fetched < planned:
        lim_codes.append(("partial-sample", {}))
    skipped = cov.get("skipped") or {}
    if record.mode == AuditMode.SITE and isinstance(fetched, int) and fetched <= 3:
        lim_codes.append(
            (
                "few-pages",
                {
                    "fetched": fetched,
                    "sitemap": cov.get("sitemap_urls", 0),
                    "other": skipped.get("out_of_scope", 0),
                },
            )
        )
    if "language-unsupported" in codes:
        lim_codes.append(("language-unsupported", {}))
    if "psi-unavailable" in codes or (
        perf is not None and perf.status == "not_measured" and perf.not_measured_reason == "psi-unavailable"
    ):
        lim_codes.append(("psi-unavailable", {}))
    if "robots-disallows-entry" in codes or any(f.id == "robots-disallows-entry" for f in record.findings):
        lim_codes.append(("robots-disallows-entry", {}))
    if "render-failed" in codes:
        lim_codes.append(("render-failed", {}))
    if isinstance(req, int) and isinstance(applied, int) and req > applied:
        lim_codes.append(("tier-capped", {"applied": applied, "requested": req}))
    if record.mode == AuditMode.SITE and len(sampled_pages) >= 5 and len(tmpl_keys) == 1:
        lim_codes.append(("single-template", {}))
    limitations = [s.Limitation(code=c, message=t(lang, f"lim.{c}", **p)) for c, p in lim_codes]

    fetch_failed = record.status == AuditStatus.FAILED or (record.pages and not sampled_pages)
    data_sources = [
        s.DataSource(id="fetch", label=t(lang, "src.fetch"), status="failed" if fetch_failed else "ok"),
        s.DataSource(
            id="render",
            label=t(lang, "src.render"),
            status="failed"
            if "render-failed" in codes
            else (
                "ok"
                if any(p.rendered for p in record.pages) or any(p.error is None for p in record.perf)
                else "missing"
            ),
        ),
        s.DataSource(
            id="lab",
            label=t(lang, "src.lab"),
            status="ok" if any(p.error is None for p in record.perf) else "missing",
        ),
        s.DataSource(
            id="psi",
            label=t(lang, "src.psi"),
            status="ok" if record.coverage.get("psi") else "missing",  # field data only when PSI really ran
        ),
        s.DataSource(
            id="gsc",
            label=t(lang, "src.gsc"),
            status=_gsc_status(record),
        ),
        s.DataSource(
            id="crux",
            label=t(lang, "src.crux"),
            status=_crux_status(record),
        ),
    ]

    # ------------------------------------------------------------------ summary
    sev_counts = {sev.value: 0 for sev in SEVERITY_ORDER}
    for g in groups:
        sev_counts[g.severity.value] += 1
    rec_by_id = {r.id: r for r in recs}
    top = [f.id for f in findings[:5]]
    quick = sorted(
        (r for r in recs if is_quick_win(r) and r.severity != Severity.INFO),
        key=lambda r: (-r.priority_score, _sev_rank(r.severity), r.check_ids[0]),
    )
    summary = s.ExecutiveSummary(
        counts=sev_counts,
        top_finding_ids=top,
        quick_win_recommendation_ids=[r.id for r in quick[:5]],
        zero_findings=not groups,
    )
    del rec_by_id

    n_checks = int(record.coverage.get("checks_run", 0) or 0)
    notes = [
        t(lang, "meth.measured", n=n_checks or "—", m=len(groups)),
        t(lang, "meth.notchecked", n=len(not_checked)),
        t(lang, "meth.heuristic"),
        t(lang, "meth.noweights"),
        t(lang, "meth.coverage"),
        t(lang, "meth.gate"),
        t(lang, "meth.score2"),
    ]
    if record.perf:
        notes.append(t(lang, "meth.lab"))
    if record.visibility is not None:
        notes.append(t(lang, "meth.visibility"))
    if record.mode == AuditMode.SITE:
        notes.append(
            t(
                lang,
                "meth.sampling",
                planned=planned if planned is not None else "—",
                fetched=fetched if fetched is not None else "—",
            )
        )

    code = _error_code(record.error)
    info = s.AuditInfo(
        id=record.id,
        mode=record.mode,
        status=record.status,
        locale="en" if lang == "en" else "tr",
        requested_url=record.target_url,
        final_url=record.final_origin,
        business_type=record.business_type,
        language=record.lang,
        started_at=record.created_at,
        finished_at=record.finished_at,
        error=code,
        error_text=reason_text(lang, code) if code else None,
    )
    return s.AuditReport(
        audit=info,
        versions=s.Versions(
            score_version=record.scores.score_version if record.scores else record.score_version,
            rule_version=record.rule_version,
        ),
        crawl=crawl,
        scores=scores,
        scope_note=sections.scope_note(lang, record),
        trend=trend,
        summary=summary,
        findings=findings,
        not_checked=not_checked,
        recommendations=recs,
        action_plan=plan,
        pages=pages,
        **sections.build_deep(record, lang),
        **sections.build_suite(record, lang),
        history=history_section(record, lang, history),
        competitors=sections.competitors_section(record, lang),
        **sections.build_extra(record, lang),
        data_sources=data_sources,
        limitations=limitations,
        methodology=s.Methodology(notes=notes),
        usage=dict(record.cost),
    )


@operation("report.build", output=AuditReport, requires=Requirements(network=False), timeout_s=30)
async def build(
    ctx: Context, audit: Audit, *, lang: str | None = None, previous: Audit | None = None
) -> Outcome[AuditReport]:
    """
    The full JSON report of an audit, with its quality-gate result in ``quality``.

    ``previous``: the previous audit of the same site; when given it fills the history section.
    """
    history = history_input(audit, previous) if previous is not None else None
    report = build_report(audit, lang or ctx.lang, history=history)
    report.quality = check_report(report)
    if report.quality.status == "FAIL":
        ctx.log.warning("report quality gate failed: %s", [c.id for c in report.quality.checks])
    return Outcome(data=report)


@operation("report.findings", output=FindingList, requires=Requirements(network=False), timeout_s=30)
async def findings(
    ctx: Context,
    report: AuditReport,
    *,
    severity: Severity | None = None,
    category: Category | None = None,
    layer: Layer | None = None,
    scope: Literal["page", "site"] | None = None,
    check_id: str | None = None,
    sort: Literal["severity", "priority"] = "severity",
) -> Outcome[FindingList]:
    """The report's findings, filtered; ``sort="priority"`` orders them by priority score."""
    items = report.findings
    if severity:
        items = [f for f in items if f.severity == severity]
    if category:
        items = [f for f in items if f.category == category]
    if layer:
        items = [f for f in items if f.layer == layer]
    if scope:
        items = [f for f in items if f.scope == scope]
    if check_id:
        items = [f for f in items if f.check_id == check_id]
    if sort == "priority":
        prio = {r.finding_ids[0]: r.priority_score for r in report.recommendations}
        items = sorted(items, key=lambda f: -prio.get(f.id, 0))
    return Outcome(data=FindingList(items=list(items)))
