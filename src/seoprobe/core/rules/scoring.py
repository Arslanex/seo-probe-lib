"""
Deterministic scoring v2. Pure functions: same input -> same output.

A category score is the severity-weighted share of its *applicable* checks that passed
(the upstream project skills/seo-technical: "the share of that category's checks that passed, adjusted
for severity; a category you could not measure is reported as 'not measured'; show the checks
behind every score"). Only Layer A/B findings count.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from seoprobe.core.models import AuditMode, Category, Finding, Layer, NotChecked, Severity
from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import CheckDef


class Penalty(BaseModel):
    """Points a failing rule cost its category (score v2: 100 * w * (1 - s) / sum(w))."""

    check_id: str
    severity: Severity
    points: float
    count: int = 1
    pages_affected: int = 1


class CheckResult(BaseModel):
    """One applicable check behind a category score ("show the checks behind every score")."""

    check_id: str
    severity: Severity
    weight: float
    pass_ratio: float  # 1.0 = passed everywhere it applied
    pages_affected: int = 0
    pages_assessed: int = 1
    status: Literal["pass", "warn", "fail"] = "pass"


class CategoryScore(BaseModel):
    category: Category
    weight: float
    score: float | None  # None when not measured
    measured: bool
    reason: str | None = None  # why not measured
    penalties: list[Penalty] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0


class Gate(BaseModel):
    check_id: str
    cap: int
    reason: str


class Scores(BaseModel):
    score_version: str
    overall: float | None  # None when insufficient data
    raw_overall: float | None = None
    coverage_pct: float
    insufficient_data: bool = False
    gate: Gate | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    categories: list[CategoryScore] = Field(default_factory=list)
    core_overall: float | None = None  # trend score from the core sample only
    p25: float | None = None  # lower-quartile page score (site mode)


class PageRecord(BaseModel):
    """One audited page as the score (and the audit record) sees it."""

    id: str
    url: str
    final_url: str | None = None
    status: int | None = None
    sample_kind: Literal["core", "exploration"] = "core"
    template_key: str | None = None
    html_bytes: int | None = None
    ttfb_ms: int | None = None
    error: str | None = None
    rendered: bool = False
    is_spa: bool | None = None
    js_gap: dict[str, Any] | None = None
    title: str | None = None
    word_count: int | None = None
    page_score: float | None = None


SCORE_VERSION = "mvp-0.2"

WEIGHTS: dict[Category, float] = {
    Category.TECHNICAL: 20,
    Category.CONTENT: 20,
    Category.ONPAGE: 15,
    Category.LINKS: 10,
    Category.PERFORMANCE: 12,
    Category.SCHEMA: 8,
    Category.AI_READINESS: 8,
    Category.IMAGES: 7,
}

# Page analysis: five categories from the HTML; the rest only when that data exists.
PAGE_MODE_BASE = {
    Category.ONPAGE,
    Category.CONTENT,
    Category.TECHNICAL,
    Category.SCHEMA,
    Category.IMAGES,
}
PAGE_MODE_OPTIONAL = (Category.PERFORMANCE, Category.AI_READINESS, Category.LINKS)

SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.CRITICAL: 8,
    Severity.HIGH: 4,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}

COVERAGE_MIN_FOR_NUMBER = 60.0

# Requirements that decide whether a rule can apply at all (the others - headers, robots,
# sitemap - are handled through not_checked).
GATED_REQUIRES = {"lab", "psi", "rendered", "gsc"}
# Legacy rules registered before CheckDef.modes existed.
_SITE_ONLY_PREFIXES = ("sitemap-", "duplicate-", "orphan-", "deep-page")

# Rules that are variants of one question collapse into one check, so a family with seven
# variants does not dilute the score with seven vacuous passes. The family weight is its
# most severe member's; a failure costs in proportion to the worst finding actually present.
_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("tls-", "tls"),
    ("hsts-", "hsts"),
    ("csp-", "csp"),
    ("http-https-redirect", "https-redirect"),
    ("http-no-https-redirect", "https-redirect"),
    ("https-unavailable", "https-redirect"),
    ("robots-", "robots"),
    ("sitemap-", "sitemap"),
    ("cwv-lcp-", "cwv-lcp"),
    ("cwv-cls-", "cwv-cls"),
    ("cwv-inp-", "cwv-inp"),
)


def _family(rule_id: str) -> str | None:
    for prefix, fam in _FAMILY_PATTERNS:
        if rule_id.startswith(prefix):
            return fam
    return None


GATE_40 = {
    "noindex",
    "noindex-header",
    "tls-cert-expired",
    "tls-cert-hostname-mismatch",
    "tls-cert-not-trusted",
    "tls-handshake-failed",
}
GATE_70 = {"canonical-points-elsewhere", "robots-blocks-site"}

DEFAULT_AVAILABLE: frozenset[str] = frozenset({"headers", "robots", "sitemap", "rendered"})


@dataclass
class GateInputs:
    entry_url: str | None = None
    entry_status: int | None = None
    entry_fetch_error: str | None = None


def _rule_modes(d: CheckDef) -> tuple[str, ...]:
    if d.modes != ("page", "site"):
        return d.modes
    if d.id.startswith(_SITE_ONLY_PREFIXES):
        return ("site",)
    return d.modes


def applicable_rules(cat: Category, mode: AuditMode, available: Iterable[str]) -> list[CheckDef]:
    """Rules that can score in this category for this audit (Info and Layer C excluded)."""
    avail = set(available)
    out = []
    for d in catalog.rules().values():
        if not d.scored or d.category != cat or d.layer != Layer.A or d.severity == Severity.INFO:
            continue
        if mode.value not in _rule_modes(d):
            continue
        if not (set(d.requires) & GATED_REQUIRES) <= avail:
            continue
        out.append(d)
    return sorted(out, key=lambda d: d.id)


def _status(severity: Severity, s: float) -> str:
    if s >= 1.0:
        return "pass"
    if s < 0.5 or severity in (Severity.CRITICAL, Severity.HIGH):
        return "fail"
    return "warn"


def _category_checks(
    cat: Category,
    findings: list[Finding],
    assessed: list[str],
    nc_pages: dict[str, set[str] | None],
    mode: AuditMode,
    available: Iterable[str],
) -> tuple[list[CheckResult], list[Penalty], float | None]:
    """(check table, penalties, score or None when nothing is applicable)."""
    rules = applicable_rules(cat, mode, available)
    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_rule[f.id].append(f)
    assessed_set = set(assessed)
    checks: list[CheckResult] = []
    families: dict[str, list[CheckDef]] = defaultdict(list)
    singles: list[CheckDef] = []
    for d in rules:
        fam = _family(d.id)
        if fam and d.scope == "site":
            families[fam].append(d)
        else:
            singles.append(d)

    for fam, members in sorted(families.items()):
        if any(m.id in nc_pages for m in members):  # a member could not run: never "passed"
            continue
        max_w = max(SEVERITY_WEIGHT[m.severity] for m in members)
        present = [f for m in members for f in by_rule.get(m.id, [])]
        worst_w = max(
            (SEVERITY_WEIGHT[next(m for m in members if m.id == f.id).severity] for f in present),
            default=0.0,
        )
        worst = next(
            (f for f in present if SEVERITY_WEIGHT[next(m for m in members if m.id == f.id).severity] == worst_w),
            None,
        )
        s = 1.0 - (worst_w / max_w if max_w else 0.0) if present else 1.0
        sev = max(members, key=lambda m: SEVERITY_WEIGHT[m.severity]).severity
        checks.append(
            CheckResult(
                check_id=worst.id if worst else fam,
                severity=sev,
                weight=max_w,
                pass_ratio=round(s, 4),
                pages_affected=1 if present else 0,
                pages_assessed=1,
                status=_status(sev, s),  # type: ignore[arg-type]
            )
        )

    for d in singles:
        skip = nc_pages.get(d.id, set())
        if skip is None:  # not checked anywhere: not applicable, never "passed"
            continue
        fs = by_rule.get(d.id, [])
        w = SEVERITY_WEIGHT[d.severity]
        if d.scope == "site":
            if d.id in nc_pages:
                continue
            affected, total = (1 if fs else 0), 1
            s = 0.0 if fs else 1.0
        else:
            pages = [u for u in assessed if u not in skip]
            total = len(pages)
            if total == 0:
                continue
            hit = {f.url for f in fs if f.url in assessed_set and f.url not in skip}
            affected = len(hit)
            s = 1.0 - affected / total
        checks.append(
            CheckResult(
                check_id=d.id,
                severity=d.severity,
                weight=w,
                pass_ratio=round(s, 4),
                pages_affected=affected,
                pages_assessed=total,
                status=_status(d.severity, s),  # type: ignore[arg-type]
            )
        )
    checks.sort(key=lambda c: c.check_id)
    sum_w = sum(c.weight for c in checks)
    if not checks or sum_w == 0:
        return checks, [], None
    score = 100.0 * sum(c.weight * c.pass_ratio for c in checks) / sum_w
    penalties = [
        Penalty(
            check_id=c.check_id,
            severity=c.severity,
            points=round(100.0 * c.weight * (1 - c.pass_ratio) / sum_w, 2),
            count=max(c.pages_affected, 1),
            pages_affected=max(c.pages_affected, 1),
        )
        for c in checks
        if c.pass_ratio < 1.0
    ]
    penalties.sort(key=lambda p: (-p.points, p.check_id))
    return checks, penalties, round(score, 1)


def _nc_index(not_checked: list[NotChecked]) -> dict[str, set[str] | None]:
    """check id -> urls where it could not run; None = everywhere (no url given)."""
    idx: dict[str, set[str] | None] = {}
    for n in not_checked:
        if n.url is None:
            idx[n.check_id] = None
        else:
            cur = idx.setdefault(n.check_id, set())
            if cur is not None:
                cur.add(n.url)
    return idx


def _gate(findings: list[Finding], gi: GateInputs) -> Gate | None:
    entry = gi.entry_url
    if gi.entry_status is not None and gi.entry_status >= 400:
        return Gate(check_id="status-error", cap=40, reason=f"entry status {gi.entry_status}")
    if gi.entry_fetch_error in ("tls-handshake-failed",):
        return Gate(check_id="tls-handshake-failed", cap=40, reason="TLS handshake failed")
    for f in findings:
        on_entry = f.scope == "site" or entry is None or f.url in (None, entry)
        if f.id in GATE_40 and on_entry:
            return Gate(check_id=f.id, cap=40, reason=f.message_key)
    for f in findings:
        on_entry = f.scope == "site" or entry is None or f.url in (None, entry)
        if f.id in GATE_70 and on_entry:
            return Gate(check_id=f.id, cap=70, reason=f.message_key)
    return None


def _assessed(pages: list[PageRecord]) -> list[str]:
    return [p.url for p in pages if p.error is None and p.status is not None and p.status < 400]


def page_score(
    findings: Iterable[Finding],
    categories: Iterable[Category],
    *,
    mode: AuditMode = AuditMode.SITE,
    available: Iterable[str] = DEFAULT_AVAILABLE,
    url: str = "_page_",
) -> float:
    """Score of ONE page across the given categories (renormalised weights)."""
    fl = [f.model_copy(update={"url": url}) for f in findings if f.scope == "page"]
    acc = 0.0
    wsum = 0.0
    for c in categories:
        _, _, sc = _category_checks(c, fl, [url], {}, mode, available)
        if sc is None:
            continue
        acc += WEIGHTS[c] * sc
        wsum += WEIGHTS[c]
    return round(acc / wsum, 1) if wsum else 100.0


def compute_scores(
    findings: list[Finding],
    not_checked: list[NotChecked],
    *,
    mode: AuditMode,
    pages: list[PageRecord],
    measured: dict[Category, str | None],
    gate_inputs: GateInputs | None = None,
    weights: dict[Category, float] | None = None,
    available: Iterable[str] = DEFAULT_AVAILABLE,
) -> Scores:
    """``measured[cat]`` is None when measured, otherwise the reason it was not."""
    w = weights or WEIGHTS
    avail = set(available)
    scored = [f for f in findings if f.layer.value in ("A", "B")]
    fetched = [p for p in pages if p.error is None and p.status is not None]
    assessed = _assessed(pages) or [p.url for p in fetched]
    nc = _nc_index(not_checked)

    applicable: set[Category] = set(Category) if mode == AuditMode.SITE else set(PAGE_MODE_BASE)
    if mode == AuditMode.PAGE:
        for extra in PAGE_MODE_OPTIONAL:
            if measured.get(extra, "n/a") is None:
                applicable.add(extra)

    cats: list[CategoryScore] = []
    for cat in Category:
        if cat not in applicable:
            continue
        reason = measured.get(cat, "no-data")
        if reason is not None:
            cats.append(CategoryScore(category=cat, weight=w[cat], score=None, measured=False, reason=reason))
            continue
        checks, pens, score = _category_checks(cat, [f for f in scored if f.category == cat], assessed, nc, mode, avail)
        if score is None:
            cats.append(
                CategoryScore(
                    category=cat,
                    weight=w[cat],
                    score=None,
                    measured=False,
                    reason="no-applicable-checks",
                )
            )
            continue
        cats.append(
            CategoryScore(
                category=cat,
                weight=w[cat],
                score=score,
                measured=True,
                penalties=pens,
                checks=checks,
                checks_total=len(checks),
                checks_passed=sum(1 for c in checks if c.pass_ratio >= 1.0),
                checks_failed=sum(1 for c in checks if c.pass_ratio < 1.0),
            )
        )

    measured_w = sum(c.weight for c in cats if c.measured)
    applicable_w = sum(c.weight for c in cats) or 1.0
    coverage = round(100.0 * measured_w / applicable_w, 1)
    raw_overall: float | None = None
    if measured_w > 0:
        raw_overall = round(sum(c.weight / measured_w * (c.score or 0.0) for c in cats if c.measured), 1)

    gate = _gate(scored, gate_inputs or GateInputs())
    overall = raw_overall
    if overall is not None and gate is not None:
        overall = min(overall, float(gate.cap))
    insufficient = raw_overall is None or coverage < COVERAGE_MIN_FOR_NUMBER
    if insufficient:
        overall = None

    # confidence
    nc_ratio = len(nc) / max(1, len(nc) + sum(c.checks_total for c in cats))
    if insufficient or coverage < 75 or nc_ratio > 0.35:
        conf = "low"
    elif mode == AuditMode.SITE and len(fetched) < 30:
        conf = "medium"
    elif mode == AuditMode.PAGE and coverage < 90:
        conf = "medium"
    else:
        conf = "high"

    core_overall: float | None = None
    p25: float | None = None
    if mode == AuditMode.SITE and fetched:
        core_urls = [p.url for p in fetched if p.sample_kind == "core" and p.url in set(assessed)]
        if core_urls:
            core_f = [f for f in scored if f.scope == "site" or f.url in set(core_urls)]
            acc = 0.0
            wsum = 0.0
            for c in cats:
                if not c.measured:
                    continue
                _, _, sc = _category_checks(
                    c.category,
                    [f for f in core_f if f.category == c.category],
                    core_urls,
                    nc,
                    mode,
                    avail,
                )
                if sc is not None:
                    acc += c.weight * sc
                    wsum += c.weight
            if wsum:
                core_overall = round(acc / wsum, 1)
                if gate is not None:
                    core_overall = min(core_overall, float(gate.cap))
        by_url: dict[str, list[Finding]] = defaultdict(list)
        for f in scored:
            if f.url:
                by_url[f.url].append(f)
        page_cats = [c.category for c in cats if c.measured and c.category in PAGE_MODE_BASE]
        if page_cats and assessed:
            scs = sorted(page_score(by_url.get(u, []), page_cats, mode=mode, available=avail) for u in assessed)
            p25 = scs[max(0, math.ceil(0.25 * len(scs)) - 1)]

    return Scores(
        score_version=SCORE_VERSION,
        overall=overall,
        raw_overall=raw_overall,
        coverage_pct=coverage,
        insufficient_data=insufficient,
        gate=gate,
        confidence=conf,  # type: ignore[arg-type]
        categories=cats,
        core_overall=core_overall,
        p25=p25,
    )
