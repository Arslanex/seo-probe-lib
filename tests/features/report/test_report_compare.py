"""compare_audits(): score and metric changes, new / resolved / persisting findings, comparability."""

from __future__ import annotations

from seoprobe.core.models import AuditMode, Finding
from seoprobe.core.rules import catalog
from seoprobe.features.audit import Audit
from seoprobe.features.performance import PagePerf
from seoprobe.features.report.compare import compare_audits as compare
from tests.features.report.sample import make_sample_record


def _rec(aid: str, **kw: object) -> Audit:
    rec = make_sample_record("gated", audit_id=aid)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def _finding(rule: str, url: str | None) -> Finding:
    return catalog.make_finding(rule, url=url)


def _rules() -> list[str]:
    rules = catalog.rules()
    return [r for r in rules if rules[r].scope == "page"][:4]


def test_findings_are_new_resolved_or_persisting_by_rule_and_url() -> None:
    a, b, c, d = _rules()
    old = _rec("old", findings=[_finding(a, "https://e.com/"), _finding(b, "https://e.com/")])
    new = _rec(
        "new",
        findings=[_finding(b, "https://e.com/"), _finding(c, "https://e.com/x"), _finding(d, None)],
    )
    cmp = compare(old, new)
    assert {(f.rule_id, f.url) for f in cmp.new_findings} == {(c, "https://e.com/x"), (d, None)}
    assert {(f.rule_id, f.url) for f in cmp.resolved_findings} == {(a, "https://e.com/")}
    assert cmp.persisting_count == 1
    assert (cmp.new_total, cmp.resolved_total) == (2, 1)


def test_same_rule_on_another_url_is_a_new_finding() -> None:
    (a, *_) = _rules()
    old = _rec("old", findings=[_finding(a, "https://e.com/1")])
    new = _rec("new", findings=[_finding(a, "https://e.com/2")])
    cmp = compare(old, new)
    assert cmp.new_total == 1 and cmp.resolved_total == 1 and cmp.persisting_count == 0


def test_scores_are_compared_with_deltas() -> None:
    old, new = _rec("old"), _rec("new")
    assert old.scores and new.scores
    old.scores.overall, new.scores.overall = 60.0, 72.5
    cmp = compare(old, new)
    assert cmp.comparable and cmp.warnings == []
    assert cmp.overall is not None and cmp.overall.delta == 12.5
    assert cmp.categories and all(c.category for c in cmp.categories)


def test_different_score_versions_give_a_warning_and_no_score_numbers() -> None:
    old, new = _rec("old"), _rec("new")
    assert old.scores
    old.scores.score_version = "mvp-0.1"
    cmp = compare(old, new)
    assert not cmp.comparable and cmp.warnings == ["score-version-differs"]
    assert cmp.overall is None and cmp.core_overall is None and cmp.categories == []
    assert cmp.new_findings is not None  # findings are still compared


def test_different_modes_are_not_comparable() -> None:
    old, new = _rec("old"), _rec("new")
    new.mode = AuditMode.PAGE if old.mode == AuditMode.SITE else AuditMode.SITE
    cmp = compare(old, new)
    assert "mode-differs" in cmp.warnings and cmp.overall is None


def _perf(**kw: int) -> PagePerf:
    return PagePerf(url="https://e.com/", **kw)  # type: ignore[arg-type]


def test_metric_deltas_know_which_direction_is_better() -> None:
    old = _rec("old", perf=[_perf(lcp_ms=3000, total_requests=40, total_bytes=900_000)])
    new = _rec("new", perf=[_perf(lcp_ms=2000, total_requests=45, total_bytes=900_000)])
    by = {m.key: m for m in compare(old, new).metrics}
    assert by["lcp_ms"].delta == -1000 and by["lcp_ms"].improved is True
    assert by["requests"].delta == 5 and by["requests"].improved is False
    assert by["total_bytes"].delta == 0 and by["total_bytes"].improved is None


def test_metric_missing_on_both_sides_is_left_out() -> None:
    old = _rec("old", perf=[], links=None, indexability=[])
    new = _rec("new", perf=[], links=None, indexability=[])
    assert compare(old, new).metrics == []


def test_comparing_an_audit_with_itself_changes_nothing() -> None:
    rec = _rec("same")
    cmp = compare(rec, rec)
    assert cmp.new_total == 0 and cmp.resolved_total == 0
    assert cmp.overall is not None and cmp.overall.delta == 0
