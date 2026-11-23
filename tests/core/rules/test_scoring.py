"""Score v2: severity-weighted share of applicable checks that passed (belge 11 s.2)."""

from __future__ import annotations

from seoprobe.core.models import AuditMode, Category, Finding, NotChecked
from seoprobe.core.rules import catalog as r
from seoprobe.core.rules.scoring import SCORE_VERSION, WEIGHTS, GateInputs, PageRecord, compute_scores, page_score

URL = "https://a.test/"
M = {c: None for c in Category}
M[Category.PERFORMANCE] = "no-data"
M[Category.LINKS] = "no-data"
M[Category.SCHEMA] = "no-data"


def _f(rule: str, url: str | None = URL) -> Finding:
    return r.make_finding(rule, url=url)


def _pages(n: int) -> list[PageRecord]:
    return [PageRecord(id=str(i), url=f"https://a.test/p{i}", status=200) for i in range(n)]


def _page_mode(findings, nc=None, measured=None):
    return compute_scores(
        findings,
        nc or [],
        mode=AuditMode.PAGE,
        pages=[PageRecord(id="1", url=URL, status=200)],
        measured=measured or M,
    )


def _cat(s, cat):
    return next(c for c in s.categories if c.category == cat)


def test_version_and_weights_sum_to_100() -> None:
    assert SCORE_VERSION == "mvp-0.2" and sum(WEIGHTS.values()) == 100 and Category.LINKS in WEIGHTS


def test_clean_page_passes_every_applicable_check_and_shows_them() -> None:
    s = _page_mode([])
    on = _cat(s, Category.ONPAGE)
    assert on.score == 100.0 and on.checks_total > 15 and on.checks_passed == on.checks_total and on.checks_failed == 0
    assert all(c.pass_ratio == 1.0 and c.status == "pass" for c in on.checks)


def test_a_failing_high_check_costs_its_share_and_is_listed() -> None:
    s = _page_mode([_f("missing-title")])
    on = _cat(s, Category.ONPAGE)
    assert on.score is not None and on.score < 100 and on.checks_failed == 1
    row = next(c for c in on.checks if c.check_id == "missing-title")
    assert row.status == "fail" and row.pass_ratio == 0.0 and row.weight == 4
    pen = next(p for p in on.penalties if p.check_id == "missing-title")
    assert abs(pen.points - (100 - on.score)) < 0.2  # the only failure explains the whole loss


def test_adding_a_failing_check_never_increases_the_score() -> None:
    a = _cat(_page_mode([_f("missing-title")]), Category.ONPAGE).score
    b = _cat(_page_mode([_f("missing-title"), _f("title-too-short")]), Category.ONPAGE).score
    assert a is not None and b is not None and b <= a


def test_passing_checks_count_so_one_failure_beats_two() -> None:
    one = _cat(_page_mode([_f("title-too-short")]), Category.ONPAGE).score
    two = _cat(_page_mode([_f("title-too-short"), _f("missing-meta-description")]), Category.ONPAGE).score
    assert one is not None and two is not None and one > two > 0


def test_page_rule_fails_in_proportion_to_pages_affected() -> None:
    pages = _pages(10)
    half = [_f("title-too-short", p.url) for p in pages[:5]]
    s = compute_scores(half, [], mode=AuditMode.SITE, pages=pages, measured=M)
    row = next(c for c in _cat(s, Category.ONPAGE).checks if c.check_id == "title-too-short")
    assert row.pass_ratio == 0.5 and row.pages_affected == 5 and row.pages_assessed == 10


def test_unmeasured_check_is_excluded_and_never_counted_as_passed() -> None:
    base = _cat(_page_mode([]), Category.ONPAGE)
    nc = [NotChecked(check_id="title-not-reflected-in-content", reason_code="language-unsupported")]
    s = _page_mode([], nc)
    assert "title-not-reflected-in-content" not in {c.check_id for c in _cat(s, Category.CONTENT).checks}
    assert _cat(s, Category.CONTENT).checks_total == _cat(_page_mode([]), Category.CONTENT).checks_total - 1
    assert base.checks_total == _cat(s, Category.ONPAGE).checks_total


def test_page_mode_ignores_site_only_rules_and_site_mode_keeps_them() -> None:
    page_ids = {c.check_id for c in _cat(_page_mode([]), Category.TECHNICAL).checks}
    assert not any(i.startswith("sitemap") for i in page_ids)
    site = compute_scores([], [], mode=AuditMode.SITE, pages=_pages(3), measured=M)
    assert any(c.check_id.startswith("sitemap") for c in _cat(site, Category.TECHNICAL).checks)


def test_lab_rules_apply_only_when_lab_data_is_available() -> None:
    m = dict(M)
    m[Category.PERFORMANCE] = None
    without = compute_scores([], [], mode=AuditMode.SITE, pages=_pages(3), measured=m)
    with_lab = compute_scores(
        [],
        [],
        mode=AuditMode.SITE,
        pages=_pages(3),
        measured=m,
        available={"headers", "robots", "sitemap", "rendered", "lab"},
    )
    pw = _cat(without, Category.PERFORMANCE)
    pl = _cat(with_lab, Category.PERFORMANCE)
    assert pl.checks_total >= pw.checks_total


def test_rule_families_count_once_and_scale_with_the_worst_member() -> None:
    clean = _cat(compute_scores([], [], mode=AuditMode.SITE, pages=_pages(3), measured=M), Category.TECHNICAL)
    tls = [c for c in clean.checks if c.check_id.startswith(("tls", "hsts", "csp"))]
    assert {c.check_id for c in tls} == {"tls", "hsts", "csp"}  # one check per family
    s = compute_scores(
        [r.make_finding("tls-cert-expiring-soon")],
        [],
        mode=AuditMode.SITE,
        pages=_pages(3),
        measured=M,
    )
    row = next(c for c in _cat(s, Category.TECHNICAL).checks if c.check_id == "tls-cert-expiring-soon")
    assert row.pass_ratio == 0.5  # High (4) against the family's Critical (8)


def test_severity_matters_a_critical_costs_more_than_a_low() -> None:
    crit = _cat(_page_mode([_f("noindex")]), Category.TECHNICAL).score
    low = _cat(_page_mode([_f("url-underscores")]), Category.ONPAGE).score
    assert crit is not None and low is not None and (100 - crit) > (100 - low)


def test_noindex_on_entry_caps_overall_at_40() -> None:
    s = compute_scores(
        [_f("noindex")],
        [],
        mode=AuditMode.SITE,
        pages=_pages(30),
        measured=M,
        gate_inputs=GateInputs(entry_url=URL, entry_status=200),
    )
    assert s.gate and s.gate.cap == 40 and s.overall is not None and s.overall <= 40


def test_entry_4xx_and_canonical_gates() -> None:
    s = compute_scores(
        [],
        [],
        mode=AuditMode.SITE,
        pages=_pages(30),
        measured=M,
        gate_inputs=GateInputs(entry_url=URL, entry_status=503),
    )
    assert s.gate and s.gate.cap == 40
    s = compute_scores(
        [_f("canonical-points-elsewhere")],
        [],
        mode=AuditMode.SITE,
        pages=_pages(30),
        measured=M,
        gate_inputs=GateInputs(entry_url=URL, entry_status=200),
    )
    assert s.gate and s.gate.cap == 70


def test_coverage_renormalises_and_below_60_hides_the_number() -> None:
    s = compute_scores([], [], mode=AuditMode.SITE, pages=_pages(30), measured=M)
    assert s.coverage_pct == round(100 * (100 - 10 - 12 - 8) / 100, 1)  # links, performance, schema unmeasured
    assert s.overall == 100.0 and not s.insufficient_data
    bad = {c: "no-data" for c in Category}
    bad[Category.TECHNICAL] = None
    s = compute_scores([], [], mode=AuditMode.SITE, pages=_pages(5), measured=bad)
    assert s.overall is None and s.insufficient_data and s.confidence == "low"


def test_categories_without_applicable_checks_are_not_measured() -> None:
    m = {c: None for c in Category}
    s = compute_scores([], [], mode=AuditMode.SITE, pages=_pages(3), measured=m)
    perf = _cat(s, Category.PERFORMANCE)
    assert (perf.score is None) == (not perf.measured)


def test_core_score_ignores_exploration_pages() -> None:
    pages = [
        PageRecord(id="c", url="https://a.test/c", status=200, sample_kind="core"),
        PageRecord(id="e", url="https://a.test/e", status=200, sample_kind="exploration"),
    ]
    s = compute_scores([_f("missing-title", "https://a.test/e")], [], mode=AuditMode.SITE, pages=pages, measured=M)
    assert s.core_overall == 100.0 and s.overall is not None and s.overall < 100.0


def test_determinism_and_order_independence() -> None:
    f = [_f("missing-title"), _f("noindex"), _f("title-too-short")]
    a = _page_mode(f)
    b = _page_mode(list(reversed(f)))
    assert a.model_dump() == b.model_dump()


def test_page_score_uses_the_same_rule_set() -> None:
    cats = [Category.ONPAGE, Category.CONTENT]
    assert page_score([], cats) == 100.0
    assert page_score([_f("missing-title")], cats) < 100.0


def test_confidence_levels() -> None:
    assert compute_scores([], [], mode=AuditMode.SITE, pages=_pages(10), measured=M).confidence != "high"


def test_unscored_rules_never_enter_the_score() -> None:
    import dataclasses

    from seoprobe.core.rules.scoring import applicable_rules

    title = r.get("missing-title")
    assert title in applicable_rules(Category.ONPAGE, AuditMode.SITE, set())
    off = dataclasses.replace(title, scored=False)
    real = r._view()[0]
    real["missing-title"] = off
    try:
        assert off not in applicable_rules(Category.ONPAGE, AuditMode.SITE, set())
    finally:
        real["missing-title"] = title
