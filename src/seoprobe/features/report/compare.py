"""
Compare two audits of the same site: score change, new / resolved / persisting findings, and
metric changes; and the report's history section built on that comparison.

Pure functions over two ``Audit`` values. Scores are only compared when they are comparable: the
same ``score_version`` and the same mode. Otherwise the result says so (``warnings``) and carries
NO score numbers: a difference between two different scoring schemes would be meaningless.
Findings are keyed by (rule id, url); a site-wide finding has no url.

``history`` is not part of the score: it only tells how this audit differs from the previous one of
the same site. It needs data from other audits, so the caller passes it in (``HistoryInput``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, Requirements, Severity
from seoprobe.core.parse.text import clean_text
from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import t
from seoprobe.features.audit import Audit

from . import models as s
from .models import AuditSummary, CategoryDelta, Comparison, Delta, FindingRef, MetricDelta
from .sections import not_measured

LIST_CAP = 50
_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}

Better = Literal["up", "down"]  # which direction is an improvement


def _delta(old: float | None, new: float | None) -> Delta:
    d = round(new - old, 1) if old is not None and new is not None else None
    return Delta(old=old, new=new, delta=d)


def _ref(f: Finding) -> FindingRef:
    return FindingRef(rule_id=f.id, severity=f.severity.value, category=f.category.value, url=f.url)


def _key(f: Finding) -> tuple[str, str]:
    return (f.id, f.url or "")


def _metrics(rec: Audit) -> dict[str, float | None]:
    perf = next((p for p in rec.perf if not p.error), None)
    out: dict[str, float | None] = {
        "lcp_ms": perf.lcp_ms if perf else None,
        "ttfb_ms": perf.ttfb_ms if perf else None,
        "total_bytes": perf.total_bytes if perf else None,
        "requests": perf.total_requests if perf else None,
        "internal_links": None,
        "external_links": None,
        "broken_links": None,
        "indexable_pages": None,
    }
    if rec.links is not None:
        out["internal_links"] = rec.links.internal_total
        out["external_links"] = rec.links.external_total
        out["broken_links"] = rec.links.broken
    if rec.indexability:
        out["indexable_pages"] = sum(1 for r in rec.indexability if r.indexable)
    return out


# which way is better; links are not listed: more or fewer links is not good or bad by itself
_DIRECTION: dict[str, Better | None] = {
    "lcp_ms": "down",
    "ttfb_ms": "down",
    "total_bytes": "down",
    "requests": "down",
    "broken_links": "down",
    "indexable_pages": "up",
    "internal_links": None,
    "external_links": None,
}


def _score_version(rec: Audit) -> str:
    return rec.scores.score_version if rec.scores else rec.score_version


def compare_audits(old: Audit, new: Audit) -> Comparison:
    warnings: list[str] = []
    if _score_version(old) != _score_version(new):
        warnings.append("score-version-differs")
    if old.mode != new.mode:
        warnings.append("mode-differs")
    out = Comparison(old_id=old.id, new_id=new.id, comparable=not warnings, warnings=warnings)

    if out.comparable and old.scores is not None and new.scores is not None:
        out.overall = _delta(old.scores.overall, new.scores.overall)
        out.core_overall = _delta(old.scores.core_overall, new.scores.core_overall)
        before = {c.category.value: c.score if c.measured else None for c in old.scores.categories}
        for c in new.scores.categories:
            name = c.category.value
            d = _delta(before.get(name), c.score if c.measured else None)
            out.categories.append(CategoryDelta(category=name, **d.model_dump()))

    old_by = {_key(f): f for f in old.findings}
    new_by = {_key(f): f for f in new.findings}
    appeared = [f for k, f in new_by.items() if k not in old_by]
    gone = [f for k, f in old_by.items() if k not in new_by]
    # the worst first: that is what the reader looks at
    appeared.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.id, f.url or ""))
    gone.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.id, f.url or ""))
    out.new_total, out.resolved_total = len(appeared), len(gone)
    out.new_findings = [_ref(f) for f in appeared[:LIST_CAP]]
    out.resolved_findings = [_ref(f) for f in gone[:LIST_CAP]]
    out.persisting_count = len(new_by.keys() & old_by.keys())

    mo, mn = _metrics(old), _metrics(new)
    for key, direction in _DIRECTION.items():
        d = _delta(mo[key], mn[key])
        if d.old is None and d.new is None:
            continue
        improved: bool | None = None
        if d.delta and direction:
            improved = d.delta < 0 if direction == "down" else d.delta > 0
        out.metrics.append(MetricDelta(key=key, improved=improved, **d.model_dump()))
    return out


@operation("report.compare", output=Comparison, requires=Requirements(network=False), timeout_s=30)
async def compare(ctx: Context, old: Audit, new: Audit) -> Outcome[Comparison]:
    """Score, finding and metric changes between two audits of the same site."""
    return Outcome(data=compare_audits(old, new))


# --------------------------------------------------------------------------- history section


HISTORY_ROWS = 10
Direction = Literal["better", "worse", "same", "none"]
_FINISHED = ("completed", "partial")
_METRIC_ORDER = (
    "lcp_ms",
    "ttfb_ms",
    "total_bytes",
    "requests",
    "internal_links",
    "external_links",
    "broken_links",
    "indexable_pages",
)


@dataclass
class HistoryInput:
    """Other audits of the same site: the list (newest first) and the full previous audit."""

    entries: list[AuditSummary] = field(default_factory=list)
    previous: Audit | None = None


def summary_of(audit: Audit) -> AuditSummary:
    return AuditSummary(
        id=audit.id,
        target_url=audit.target_url,
        mode=audit.mode,
        status=audit.status,
        overall=audit.scores.overall if audit.scores else None,
        coverage_pct=audit.scores.coverage_pct if audit.scores else None,
        created_at=audit.created_at,
    )


def history_input(audit: Audit, previous: Audit) -> HistoryInput:
    """The history of ``audit`` when the caller has the previous audit of the same site."""
    entries = [summary_of(audit)] if previous.id == audit.id else [summary_of(audit), summary_of(previous)]
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return HistoryInput(entries=entries, previous=previous)


def pick_previous(audit_id: str, mode: str, entries: list[AuditSummary]) -> str | None:
    """Id of the closest older finished audit in the same mode (``entries`` is newest first)."""
    ids = [e.id for e in entries]
    if audit_id not in ids:
        return None
    for e in entries[ids.index(audit_id) + 1 :]:
        if e.mode.value == mode and e.status.value in _FINISHED and e.overall is not None:
            return e.id
    return None


def _c(text: object, limit: int = 300) -> str:
    return clean_text(str(text))[:limit]


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}".rstrip("0").rstrip(".")


def _ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{round(v)} ms"


def _kb(v: float | None) -> str:
    return "—" if v is None else f"{round(v / 1024):,} KB".replace(",", ".")


def _count(v: float | None) -> str:
    return "—" if v is None else str(round(v))


_FORMAT = {
    "lcp_ms": _ms,
    "ttfb_ms": _ms,
    "total_bytes": _kb,
    "requests": _count,
    "internal_links": _count,
    "external_links": _count,
    "broken_links": _count,
    "indexable_pages": _count,
}


def _signed(delta: float | None, fmt: Callable[[float | None], str] = _num) -> str:
    if delta is None:
        return "—"
    if delta == 0:
        return "0"
    return ("+" if delta > 0 else "−") + fmt(abs(delta))


def _score_change(label: str, d: Delta) -> s.HistoryChange:
    direction: Direction = "none"
    if d.delta is not None:
        direction = "same" if d.delta == 0 else ("better" if d.delta > 0 else "worse")
    return s.HistoryChange(label=label, old=_num(d.old), new=_num(d.new), delta=_signed(d.delta), direction=direction)


def _finding_row(ref: FindingRef, lang: str) -> s.HistoryFinding:
    rule = catalog.rules().get(ref.rule_id)
    title = catalog.message(rule.message_key, lang) if rule else ref.rule_id
    return s.HistoryFinding(
        rule_id=_c(ref.rule_id, 80),
        severity=ref.severity,
        title=_c(title, 200),
        url=_c(ref.url, 300) if ref.url else None,
    )


def _comparison_into(section: s.HistorySection, cmp: Comparison, lang: str) -> None:
    if not cmp.comparable:
        section.warning_text = " ".join(t(lang, f"hist.warn.{w}") for w in cmp.warnings)
    if cmp.overall is not None:
        section.scores.append(_score_change(t(lang, "hist.overall"), cmp.overall))
    if cmp.core_overall is not None and cmp.core_overall.new is not None:
        section.scores.append(_score_change(t(lang, "hist.core"), cmp.core_overall))
    for cd in cmp.categories:
        if cd.old is None and cd.new is None:
            continue
        section.scores.append(_score_change(t(lang, f"cat.{cd.category}"), cd))
    by_key = {m.key: m for m in cmp.metrics}
    for key in _METRIC_ORDER:
        m = by_key.get(key)
        if m is None:
            continue
        fmt = _FORMAT[key]
        direction: Direction = "none"
        if m.delta is not None:
            if m.delta == 0:
                direction = "same"
            elif m.improved is not None:
                direction = "better" if m.improved else "worse"
        section.metrics.append(
            s.HistoryChange(
                label=t(lang, f"hist.metric.{key}"),
                old=fmt(m.old),
                new=fmt(m.new),
                delta=_signed(m.delta, fmt),
                direction=direction,
            )
        )
    section.new_findings = [_finding_row(f, lang) for f in cmp.new_findings]
    section.resolved_findings = [_finding_row(f, lang) for f in cmp.resolved_findings]
    section.new_total, section.resolved_total = cmp.new_total, cmp.resolved_total
    section.persisting = cmp.persisting_count


def history_section(rec: Audit, lang: str, hist: HistoryInput | None) -> s.HistorySection:
    if hist is None:
        return s.HistorySection(**not_measured(lang, "history-unavailable"))
    older = [e for e in hist.entries if e.id != rec.id]
    if not older:
        return s.HistorySection(**not_measured(lang, "first-audit"))
    section = s.HistorySection(status="measured", note=t(lang, "hist.note"))
    shown = hist.entries[:HISTORY_ROWS]
    if rec.id not in {e.id for e in shown}:  # the current one is always in the table
        shown = [*shown[: HISTORY_ROWS - 1], *(e for e in hist.entries if e.id == rec.id)]
    section.rows = [
        s.HistoryRow(
            id=e.id,
            url=_c(e.target_url),
            created_at=e.created_at,
            mode=e.mode.value,
            status=e.status.value,
            overall=e.overall,
            coverage_pct=e.coverage_pct,
            is_current=e.id == rec.id,
        )
        for e in shown
    ]
    section.rows_total = len(hist.entries)
    section.truncated = len(hist.entries) > len(section.rows)
    prev = hist.previous
    if prev is not None and prev.id != rec.id:
        section.previous_id = prev.id
        section.previous_at = prev.created_at
        _comparison_into(section, compare_audits(prev, rec), lang)
    return section
