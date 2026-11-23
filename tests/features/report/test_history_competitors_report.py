"""History, comparison and the competitors section in the report."""

from __future__ import annotations

from datetime import timedelta

from seoprobe.core.models import AuditMode, NotChecked
from seoprobe.features.audit import Audit, AuditStatus
from seoprobe.features.offsite import CompetitorComparison, CompetitorRow
from seoprobe.features.report import AuditSummary, build, compare
from seoprobe.features.report.build import build_report
from seoprobe.features.report.compare import HistoryInput, history_section, pick_previous
from seoprobe.features.report.quality import check_report, missing_keys
from tests import fakes
from tests.features.report.sample import make_sample_record

XSS = '<script>alert("c1")</script>'


def _rec(aid: str, minutes: int = 0, **kw: object) -> Audit:
    rec = make_sample_record("gated", audit_id=aid)
    rec.created_at = rec.created_at + timedelta(minutes=minutes)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def _summary(rec: Audit) -> AuditSummary:
    return AuditSummary(
        id=rec.id,
        target_url=rec.target_url,
        mode=rec.mode,
        status=rec.status,
        overall=rec.scores.overall if rec.scores else None,
        coverage_pct=rec.scores.coverage_pct if rec.scores else None,
        created_at=rec.created_at,
    )


def test_catalog_parity() -> None:
    assert missing_keys() == set()


# --------------------------------------------------------------------------- history


def test_history_without_input_or_with_a_single_audit_says_why() -> None:
    rec = _rec("a")
    assert history_section(rec, "en", None).not_measured_reason == "history-unavailable"
    only = HistoryInput(entries=[_summary(rec)])
    sec = history_section(rec, "en", only)
    assert sec.status == "not_measured" and sec.not_measured_reason == "first-audit"
    assert sec.rows == [] and (sec.not_measured_text or "").strip()


def test_pick_previous_takes_the_closest_older_finished_scored_audit_of_the_same_mode() -> None:
    cur, failed, other_mode, good, older = (
        _rec("cur", 50),
        _rec("failed", 40, status=AuditStatus.FAILED),
        _rec("other", 30, mode=AuditMode.PAGE),
        _rec("good", 20),
        _rec("older", 10),
    )
    cur.mode = good.mode = older.mode = failed.mode = AuditMode.SITE
    entries = [_summary(r) for r in (cur, failed, other_mode, good, older)]
    assert pick_previous("cur", "site", entries) == "good"
    assert pick_previous("older", "site", entries) is None
    assert pick_previous("unknown", "site", entries) is None


def test_history_section_has_rows_and_a_comparison() -> None:
    old, new = _rec("old", 0), _rec("new", 10)
    old.scores.overall = 50.0  # type: ignore[union-attr]
    new.scores.overall = 62.0  # type: ignore[union-attr]
    hist = HistoryInput(entries=[_summary(new), _summary(old)], previous=old)
    sec = history_section(new, "en", hist)
    assert sec.status == "measured" and sec.previous_id == "old"
    assert [r.is_current for r in sec.rows] == [True, False]
    overall = sec.scores[0]
    assert (overall.old, overall.new, overall.delta, overall.direction) == (
        "50",
        "62",
        "+12",
        "better",
    )
    assert sec.counts_toward_overall is False


def test_history_with_different_score_versions_shows_a_warning_and_no_numbers() -> None:
    old, new = _rec("old", 0), _rec("new", 10)
    old.scores.score_version = "mvp-0.1"  # type: ignore[union-attr]
    sec = history_section(new, "tr", HistoryInput([_summary(new), _summary(old)], old))
    assert sec.warning_text and sec.scores == []


def test_history_rows_are_capped_and_the_current_audit_stays_in() -> None:
    recs = [_rec(f"r{i}", i) for i in range(15)]
    cur = recs[0]  # the oldest: it falls outside the newest 10 but must be listed
    entries = [_summary(r) for r in sorted(recs, key=lambda r: r.created_at, reverse=True)]
    sec = history_section(cur, "en", HistoryInput(entries=entries))
    assert len(sec.rows) == 10 and sec.rows_total == 15 and sec.truncated
    assert any(r.is_current for r in sec.rows)


def test_history_in_the_report_passes_the_quality_gate() -> None:
    old, new = _rec("old", 0), _rec("new", 10)
    hist = HistoryInput(entries=[_summary(new), _summary(old)], previous=old)
    for lang in ("tr", "en"):
        report = build_report(new, lang, history=hist)
        quality = check_report(report, lang=lang)
        assert not [c for c in quality.checks if c.status == "FAIL"], quality
        assert report.history.status == "measured" and report.history.rows
        assert report.history.previous_id == "old"


def test_history_findings_keep_page_text_and_pass_the_gate() -> None:
    old, new = _rec("old", 0), _rec("new", 10)
    assert new.findings
    new.findings[0].url = f"https://example.com/{XSS}"
    old.findings = []
    hist = HistoryInput(entries=[_summary(new), _summary(old)], previous=old)
    report = build_report(new, "en", history=hist)
    assert any(XSS in (f.url or "") for f in report.history.new_findings)
    assert not [c for c in check_report(report, lang="en").checks if c.status == "FAIL"]


# --------------------------------------------------------------------------- competitors


def _comp_record(**row: object) -> Audit:
    base: dict[str, object] = dict(url="https://rival.example/", host="rival.example", status=200, score=70.0)
    data = CompetitorComparison(
        you=CompetitorRow(url="https://example.com/", host="example.com", is_you=True, status=200),
        rows=[CompetitorRow(**{**base, **row})],  # type: ignore[arg-type]
    )
    return _rec("c", extras={"competitors": data.model_dump()})


def test_competitors_not_measured_reasons() -> None:
    rec = _rec("c")
    sec = build_report(rec, "en").competitors
    assert sec.status == "not_measured" and sec.not_measured_reason == "suite-no-data"
    rec.not_checked.append(NotChecked(check_id="competitors", reason_code="no-competitors"))
    sec = build_report(rec, "tr").competitors
    assert sec.not_measured_reason == "no-competitors" and "3" in (sec.not_measured_text or "")


def test_competitors_table_puts_you_first_and_never_changes_the_score() -> None:
    plain = _rec("p")
    rec = _comp_record(tech=["WordPress"], schema_types=["Organization"], title_len=30, h1_count=1)
    a, b = build_report(plain, "en"), build_report(rec, "en")
    assert a.scores.overall.value == b.scores.overall.value
    sec = b.competitors
    assert sec.status == "measured"
    assert [c.is_you for c in sec.columns] == [True, False]
    assert all(len(c.values) == len(sec.metrics) for c in sec.columns)
    assert sec.columns[1].host == "rival.example" and "WordPress" in sec.columns[1].values
    assert not [c for c in check_report(b, lang="en").checks if c.status == "FAIL"]


def test_competitor_text_from_the_page_passes_the_gate() -> None:
    rec = _comp_record(host=XSS, tech=[XSS], schema_types=[XSS], url=f"https://x.example/{XSS}")
    report = build_report(rec, "en")
    assert report.competitors.columns[1].host == XSS
    assert not [c for c in check_report(report, lang="en").checks if c.status == "FAIL"]


def test_competitor_error_row_explains_itself() -> None:
    rec = _comp_record(error="target-rejected", status=None, score=None)
    col = build_report(rec, "en").competitors.columns[1]
    assert col.note and "safety" in col.note


# --------------------------------------------------------------------------- operation


async def test_compare_operation() -> None:
    old = _rec("aud_old", 0, target_url="https://example.com/")
    new = _rec("aud_new", 10, target_url="https://www.example.com/")
    async with fakes.session() as session:
        res = await compare(old, new, session=session)
    b = res.to_dict()["data"]
    assert res.ok and b["old_id"] == "aud_old" and b["new_id"] == "aud_new" and b["comparable"] is True
    assert b["overall"]["delta"] == 0


async def test_build_with_the_previous_audit_fills_the_history_section() -> None:
    old, new = _rec("old", 0), _rec("new", 10)
    old.scores.overall = 50.0  # type: ignore[union-attr]
    async with fakes.session() as session:
        res = await build(new, lang="en", previous=old, session=session)
        alone = await build(new, lang="en", session=session)
        itself = await build(new, lang="en", previous=new, session=session)
    hist = res.unwrap().history
    assert hist.status == "measured" and hist.previous_id == "old"
    assert [r.id for r in hist.rows] == ["new", "old"] and [r.is_current for r in hist.rows] == [True, False]
    assert hist.scores[0].delta == "−10" and hist.scores[0].direction == "worse"
    assert hist == history_section(new, "en", HistoryInput([_summary(new), _summary(old)], old))
    assert alone.unwrap().history.not_measured_reason == "history-unavailable"
    assert itself.unwrap().history.not_measured_reason == "first-audit"
    assert res.unwrap().quality.status != "FAIL"
