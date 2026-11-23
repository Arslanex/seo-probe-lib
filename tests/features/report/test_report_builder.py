from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from seoprobe.core.models import Category, Finding, Layer, Severity
from seoprobe.core.rules.catalog import reason_text, t
from seoprobe.features.report import models as s
from seoprobe.features.report.build import build_report
from seoprobe.features.report.plan import (
    assign_phases,
    build_action_plan,
    find_cycles,
    priority_score,
)
from seoprobe.features.report.quality import _UI, missing_keys
from tests.features.report.sample import make_sample_record

VARIANTS = ["gated", "clean", "insufficient", "partial", "failed", "page", "canceled"]


def test_gate_banner_data_is_consistent() -> None:
    r = build_report(make_sample_record("gated"), "tr")
    ov = r.scores.overall
    assert (ov.value, ov.raw_value, ov.gate and ov.gate.cap) == (40.0, 71.0, 40)
    assert ov.value <= ov.gate.cap  # type: ignore[union-attr]
    gate_finding = r.finding(ov.gate.finding_id)  # type: ignore[union-attr, arg-type]
    assert gate_finding is not None and gate_finding.check_id == "noindex" and gate_finding.is_gate
    assert ov.band == "poor"


def test_no_gate_variant_has_no_gate_and_good_band() -> None:
    r = build_report(make_sample_record("clean"), "en")
    assert r.scores.overall.gate is None and r.scores.overall.band == "good"
    assert r.summary.zero_findings and r.findings == []


def test_insufficient_data_never_shows_a_number_or_raw_value() -> None:
    r = build_report(make_sample_record("insufficient"), "tr")
    ov = r.scores.overall
    assert ov.value is None and ov.raw_value is None and ov.display == "insufficient_data"
    assert ov.coverage_pct == 40.0


def test_not_measured_category_has_no_score_and_weights_redistribute() -> None:
    r = build_report(make_sample_record("partial"), "tr")
    perf = next(c for c in r.scores.categories if c.id == Category.PERFORMANCE)
    assert perf.status == "not_measured" and perf.score is None and perf.weight_effective is None
    assert perf.not_measured_reason == "psi-unavailable" and "PageSpeed" in (perf.not_measured_text or "")
    measured = [c for c in r.scores.categories if c.status != "not_measured"]
    assert sum(c.weight_effective or 0 for c in measured) == pytest.approx(100, abs=0.6)


def test_page_mode_is_a_page_card_without_perf_and_ai() -> None:
    r = build_report(make_sample_record("page"), "tr")
    assert r.scores.kind == "page_card"
    assert {c.id for c in r.scores.categories} == {
        Category.TECHNICAL,
        Category.ONPAGE,
        Category.CONTENT,
        Category.LINKS,
        Category.SCHEMA,
        Category.IMAGES,
    }


def test_failed_audit_has_error_text_and_no_score() -> None:
    r = build_report(make_sample_record("failed"), "tr")
    assert r.audit.error == "unreachable" and "ulaşılamadı" in (r.audit.error_text or "")
    assert r.scores.overall.value is None and r.findings == []
    r_en = build_report(make_sample_record("failed"), "en")
    assert "could not be reached" in (r_en.audit.error_text or "")
    assert "internal-host" not in json.dumps(r.model_dump(mode="json"))  # no internal detail leaks


def test_partial_audit_lists_limitations() -> None:
    r = build_report(make_sample_record("partial"), "en")
    codes = {x.code for x in r.limitations}
    assert {
        "partial-sample",
        "render-failed",
        "psi-unavailable",
        "language-unsupported",
        "tier-capped",
    } <= codes


def test_same_check_on_many_pages_is_one_grouped_finding() -> None:
    r = build_report(make_sample_record("gated"), "en")
    f = next(x for x in r.findings if x.check_id == "title-too-long")
    assert len(f.urls) == 2 and f.affected.count == 2
    assert f.affected.share_of_sampled_pages == pytest.approx(0.2)
    assert len([x for x in r.findings if x.check_id == "title-too-long"]) == 1


def test_messages_are_resolved_in_the_report_language() -> None:
    tr = build_report(make_sample_record("gated"), "tr")
    en = build_report(make_sample_record("gated"), "en")
    a = next(x for x in tr.findings if x.check_id == "multiple-h1")
    b = next(x for x in en.findings if x.check_id == "multiple-h1")
    assert a.message != b.message and "H1" in a.message and "H1" in b.message
    assert tr.audit.locale == "tr" and en.audit.locale == "en"


def test_unknown_language_falls_back_to_default() -> None:
    # the library's default language is English (the source defaulted to Turkish)
    assert build_report(make_sample_record("gated"), "de").audit.locale == "en"


def test_recommendations_carry_registry_defaults() -> None:
    r = build_report(make_sample_record("gated"), "en")
    rec = next(x for x in r.recommendations if x.check_ids == ["noindex"])
    assert rec.effort.level == "S" and rec.impact.level == "high" and rec.priority_score == 3.0
    assert (rec.effort.hours_min, rec.effort.hours_max) == (0.25, 1.0)


def test_build_is_pure_and_deterministic() -> None:
    rec = make_sample_record("gated")
    a = build_report(rec, "tr").model_dump(mode="json")
    b = build_report(rec, "tr").model_dump(mode="json")
    assert a == b


def test_finding_order_in_the_record_does_not_change_the_report() -> None:
    rec = make_sample_record("gated")
    shuffled = rec.model_copy(update={"findings": list(reversed(rec.findings))})
    a = build_report(rec, "en")
    b = build_report(shuffled, "en")
    assert [f.check_id for f in a.findings] == [f.check_id for f in b.findings]
    assert [[i.recommendation_id for i in p.items] for p in a.action_plan.phases] == [
        [i.recommendation_id for i in p.items] for p in b.action_plan.phases
    ]


def test_evidence_is_clean_and_bounded() -> None:
    rec = make_sample_record("gated")
    rec.findings[0] = rec.findings[0].model_copy(update={"evidence": "a‮b\x00c" + "x" * 300})
    r = build_report(rec, "en")
    for f in r.findings:
        if f.evidence:
            assert len(f.evidence.value) <= 200 and "‮" not in f.evidence.value and "\x00" not in f.evidence.value


def test_schema_rejects_inconsistent_scores() -> None:
    with pytest.raises(ValidationError):
        s.OverallScore(value=80, display="insufficient_data", coverage_pct=40, confidence="low")
    with pytest.raises(ValidationError):
        s.OverallScore(
            value=80,
            display="number",
            coverage_pct=90,
            confidence="low",
            gate=s.Gate(id="x", cap=40),
        )
    with pytest.raises(ValidationError):
        s.CategoryReport(id=Category.SCHEMA, label="x", weight_default=10, status="not_measured", score=50)


def test_layer_c_cannot_produce_findings_at_all() -> None:
    """Layer C (the removed LLM layer) never yields findings: the schema rejects it."""
    base = {
        "id": "f",
        "check_id": "x",
        "rule_version": "1",
        "category": "content",
        "layer": "C",
        "severity": "High",
        "scope": "page",
        "affected": {"count": 1},
        "message_key": "k",
        "message": "m",
    }
    with pytest.raises(ValidationError):
        s.FindingReport.model_validate(base)


@pytest.mark.parametrize("variant", VARIANTS)
def test_report_round_trips_through_the_schema(variant: str) -> None:
    r = build_report(make_sample_record(variant), "tr")  # type: ignore[arg-type]
    again = s.AuditReport.model_validate_json(r.model_dump_json())
    assert again == r


# ------------------------------------------------------------------ action plan


def _rec(
    rid: str,
    sev: Severity,
    effort: str = "M",
    impact: str = "medium",
    deps: list[str] | None = None,
    check: str | None = None,
) -> s.Recommendation:
    return s.Recommendation(
        id=rid,
        finding_ids=[f"f_{rid}"],
        check_ids=[check or rid],
        title=rid,
        body=rid,
        severity=sev,
        effort=s.Effort(level=effort, hours_min=1, hours_max=2),  # type: ignore[arg-type]
        impact=s.Impact(level=impact),  # type: ignore[arg-type]
        depends_on=deps or [],
        priority_score=priority_score(impact, "high", effort),
    )


def test_phase_rules() -> None:
    recs = [
        _rec("crit", Severity.CRITICAL, "L", "low"),
        _rec("high", Severity.HIGH, "L", "low"),
        _rec("med", Severity.MEDIUM, "L", "low"),
        _rec("low", Severity.LOW, "L", "low"),
        _rec("info", Severity.INFO, "L", "low"),
        _rec("quick_high", Severity.HIGH, "S", "high"),
        _rec("quick_med", Severity.MEDIUM, "S", "medium"),
        _rec("quick_low", Severity.LOW, "S", "high"),  # Low is never bumped
    ]
    ph = assign_phases(recs, gate_rec_ids={"med"})
    assert ph == {
        "crit": 1,
        "high": 2,
        "med": 1,
        "low": 4,
        "info": 4,
        "quick_high": 1,
        "quick_med": 2,
        "quick_low": 4,
    }


def test_dependants_never_move_earlier_than_their_dependency() -> None:
    recs = [
        _rec("base", Severity.LOW, "L", "low"),
        _rec("urgent", Severity.CRITICAL, "S", "high", deps=["base"]),
    ]
    ph = assign_phases(recs, set())
    assert ph["base"] == 4 and ph["urgent"] == 4  # urgent waits for base, never the reverse


def test_cycle_detection() -> None:
    a, b = _rec("a", Severity.LOW, deps=["b"]), _rec("b", Severity.LOW, deps=["a"])
    assert find_cycles([a, b]) == ["a", "b"] and find_cycles([_rec("x", Severity.LOW)]) == []
    assign_phases([a, b], set())  # must terminate


def test_plan_has_four_phases_and_is_deterministic() -> None:
    recs = [_rec(f"r{i}", Severity.MEDIUM, "S", "medium") for i in range(6)]
    p1, _ = build_action_plan(recs, set(), "en")
    p2, _ = build_action_plan(list(reversed(recs)), set(), "en")
    assert [p.number for p in p1.phases] == [1, 2, 3, 4]
    assert p1 == p2  # order within a phase is a total order (priority, severity, check id)
    assert p1.phases[3].notes  # phase 4 carries monitoring notes


# ------------------------------------------------------------------ i18n


def test_ui_catalogs_have_identical_keys() -> None:
    assert missing_keys() == set()
    assert len(_UI.messages["tr"]) > 150


def test_t_never_raises_and_formats() -> None:
    assert t("tr", "no.such.key") == "no.such.key"
    assert "40" in t("en", "gate.text", reason="r", raw=71, cap=40)
    assert "{reason}" in t("en", "gate.text", reason="r")  # missing params degrade, not crash


@pytest.mark.parametrize(
    "code",
    [
        "target-rejected",
        "unreachable",
        "tls-handshake-failed",
        "status-error",
        "robots-unreachable",
        "robots-disallows-entry",
        "sitemap-missing",
        "sitemap-invalid",
        "headers-unavailable",
        "render-failed",
        "psi-unavailable",
        "language-unsupported",
        "words-not-measurable",
        "partial-sample",
        "rule-error",
    ],
)
def test_every_reason_code_has_a_sentence_in_both_languages(code: str) -> None:
    assert reason_text("tr", code) != code and reason_text("en", code) != code


def test_fine_grained_finding_model_accepts_domain_finding() -> None:
    f = Finding(id="x", category=Category.ONPAGE, severity=Severity.LOW, message_key="k", message="m")
    assert f.layer == Layer.A
