"""
Report quality gate over the JSON report: FAIL marks a broken report, WARN is shown.

A message_key missing from the *report language* catalog is WARN, not FAIL, as long as the
English text exists: a missing translation must not hide a valid report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import Catalog, norm_lang

from . import models as s
from .models import AuditReport, Quality, QualityCheck
from .plan import find_cycles

QualityResult = Quality
Status = Literal["PASS", "WARN", "FAIL"]


def _check_contract(report: AuditReport, add: _Adder) -> None:
    ov = report.scores.overall
    if ov.gate and ov.value is not None and ov.value > ov.gate.cap:
        add("contract.gate", "FAIL", "score exceeds the gate cap")
    if ov.display == "insufficient_data" and ov.value is not None:
        add("contract.insufficient", "FAIL", "insufficient data but a number is shown")
    if ov.coverage_pct < 60 and ov.value is not None:
        add("contract.coverage60", "FAIL", f"coverage {ov.coverage_pct} < 60 but a number is shown")
    measured = [c for c in report.scores.categories if c.status != "not_measured"]
    for c in report.scores.categories:
        if c.status == "not_measured" and c.score is not None:
            add("contract.category", "FAIL", f"{c.id.value}: not_measured with a score")
    if measured and abs(sum(c.weight_effective or 0 for c in measured) - 100) > 1.0:
        add("contract.weights", "WARN", "effective weights do not sum to 100")
    for f in report.findings:
        if not f.message.strip():
            add("contract.message", "FAIL", f"{f.check_id}: empty message")
    for c in report.scores.categories:
        if c.status != "not_measured" and c.checks_total != c.checks_passed + c.checks_failed:
            add("contract.checks", "FAIL", f"{c.id.value}: passed + failed != total")
        if c.status == "not_measured" and c.checks:
            add("contract.checks", "FAIL", f"{c.id.value}: not measured but lists checks")
    _check_deep(report, add)


def _suite_sections(report: AuditReport) -> dict[str, tuple[s.SectionState, int, int]]:
    tech, setup, prev = report.techstack, report.site_setup, report.previews
    shots, read, hl = report.screenshots, report.readability, report.hreflang
    sug, ag = report.schema_suggestions, report.agentic
    hist, comp = report.history, report.competitors
    n_tech = sum(len(g.items) for g in tech.groups)
    return {
        "techstack": (tech, n_tech, max(tech.total, n_tech)),
        "site_setup": (
            setup,
            len(setup.redirects) + len(setup.files),
            len(setup.redirects) + len(setup.files),
        ),
        "previews": (
            prev,
            len(prev.serp) + len(prev.social) + len(prev.social_image),
            len(prev.serp) + len(prev.social) + len(prev.social_image),
        ),
        "screenshots": (shots, len(shots.shots), len(shots.shots)),
        "readability": (
            read,
            len(read.scores) + len(read.terms),
            len(read.scores) + len(read.terms),
        ),
        "hreflang": (hl, len(hl.rows), hl.rows_total),
        "schema_suggestions": (sug, len(sug.items), sug.items_total),
        "agentic": (ag, len(ag.rows), len(ag.rows)),
        "history": (hist, len(hist.rows), hist.rows_total),
        "competitors": (comp, len(comp.columns), len(comp.columns)),
        "local": (
            report.local,
            len(report.local.phones) + len(report.local.addresses) + len(report.local.issues),
            len(report.local.phones) + len(report.local.addresses) + len(report.local.issues),
        ),
        "sitemap_gen": (
            report.sitemap_gen,
            report.sitemap_gen.urls_shown,
            report.sitemap_gen.urls_total,
        ),
        "authority": (report.authority, len(report.authority.items), len(report.authority.items)),
        "rankings": (
            report.rankings,
            len(report.rankings.rows) + len(report.rankings.opportunities) + len(report.rankings.low_ctr),
            len(report.rankings.rows) + len(report.rankings.opportunities) + len(report.rankings.low_ctr),
        ),
    }


def _deep_sections(report: AuditReport) -> dict[str, tuple[s.SectionState, int, int]]:
    """name -> (section, rows shown, rows total)."""
    lk, pf, im, ix = report.links, report.performance, report.images, report.indexability
    gs, fd = report.gsc, report.field
    return {
        "links": (lk, len(lk.links) + len(lk.pages), lk.links_total + lk.pages_total),
        "performance": (pf, len(pf.rows), pf.rows_total),
        "images": (im, len(im.rows), im.rows_total),
        "indexability": (ix, len(ix.rows), ix.rows_total),
        "visibility": (
            report.visibility,
            len(report.visibility.items),
            len(report.visibility.items),
        ),
        "gsc": (
            gs,
            len(gs.top_queries) + len(gs.top_pages) + len(gs.inspections) + len(gs.sitemaps),
            gs.inspections_total + len(gs.top_queries) + len(gs.top_pages) + len(gs.sitemaps),
        ),
        "field": (fd, len(fd.metrics), len(fd.metrics)),
        **_suite_sections(report),
    }


def _check_deep(report: AuditReport, add: _Adder) -> None:
    """A deep section is measured-with-data or not-measured-with-a-reason; never an empty table."""
    for name, (sec, shown, total) in _deep_sections(report).items():
        if sec.status == "not_measured":
            if shown:
                add(f"deep.{name}", "FAIL", "not measured but carries rows")
            if not sec.not_measured_reason or not (sec.not_measured_text or "").strip():
                add(f"deep.{name}", "FAIL", "not measured without a reason")
        elif shown > total:
            add(f"deep.{name}", "FAIL", "more rows shown than the total")
    if report.visibility.counts_toward_overall is not False:
        add("deep.visibility", "FAIL", "visibility signals must not count toward the score")
    if report.images.rows_total < len(report.images.rows):
        add("deep.images", "FAIL", "image rows exceed the total")


def _check_refs(report: AuditReport, add: _Adder) -> None:
    fids = {f.id for f in report.findings}
    rids = {r.id for r in report.recommendations}
    bad = [r.id for r in report.recommendations if not set(r.finding_ids) <= fids]
    bad += [f.id for f in report.findings if not set(f.recommendation_ids) <= rids]
    bad += [r.id for r in report.recommendations if not set(r.depends_on) <= rids]
    if bad:
        add("refs.resolve", "FAIL", f"unresolved references: {sorted(set(bad))[:5]}")
    placed = [it.recommendation_id for ph in report.action_plan.phases for it in ph.items]
    if sorted(placed) != sorted(rids):
        add("refs.plan", "FAIL", "every recommendation must appear in exactly one phase")
    if len(report.action_plan.phases) != 4:
        add("refs.phases", "FAIL", "the plan must have 4 phases")
    cyc = find_cycles(report.recommendations)
    if cyc:
        add("refs.cycle", "FAIL", f"dependency cycle: {cyc[:5]}")


_UI = Catalog(__package__ or "seoprobe.features.report")


def missing_keys() -> set[str]:
    """UI text keys present in only one language."""
    return set(_UI.messages["tr"]) ^ set(_UI.messages["en"])


def _has_lang_text(key: str, lang: str) -> bool:
    return any(c.messages[lang].get(key) for c in catalog.installed())


def _check_text(report: AuditReport, lang: str, add: _Adder) -> None:
    if missing_keys():
        add("text.catalog", "FAIL", f"TR/EN UI catalogs differ: {sorted(missing_keys())[:5]}")
    gaps = []
    for f in report.findings:
        if not _has_lang_text(f.message_key, lang):
            gaps.append(f.message_key)
    if gaps:
        add(
            "text.translation",
            "WARN",
            f"no {lang} text for: {sorted(set(gaps))[:5]} (English fallback used)",
        )


class _Adder:
    def __init__(self) -> None:
        self.checks: list[QualityCheck] = []

    def __call__(self, check_id: str, status: Status, detail: str = "") -> None:
        self.checks.append(QualityCheck(id=check_id, status=status, detail=detail))


def check_report(report: AuditReport, *, lang: str | None = None) -> Quality:
    lang = norm_lang(lang or report.audit.locale)
    add = _Adder()
    try:
        AuditReport.model_validate(report.model_dump(mode="json"))
    except ValidationError as exc:
        add("schema", "FAIL", str(exc.errors()[0])[:200])
    else:
        add("schema", "PASS")
    _check_contract(report, add)
    _check_refs(report, add)
    _check_text(report, lang, add)
    statuses = {c.status for c in add.checks}
    overall: Status = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    return Quality(status=overall, checks=[c for c in add.checks if c.status != "PASS"] or [])
