"""The JSON report's quality gate and the golden report (the JSON parts of the source's HTML-report tests)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seoprobe.core.models import Severity
from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import Catalog
from seoprobe.features.report import findings
from seoprobe.features.report.build import build_report
from seoprobe.features.report.quality import check_report
from tests import fakes
from tests.features.report.sample import make_sample_record

VARIANTS = ["gated", "clean", "insufficient", "partial", "failed", "page", "canceled"]


@pytest.mark.parametrize("lang", ["tr", "en"])
@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_passes_the_quality_gate(variant: str, lang: str) -> None:
    report = build_report(make_sample_record(variant), lang)  # type: ignore[arg-type]
    q = check_report(report, lang=lang)
    assert q.status in ("PASS", "WARN"), q.checks
    assert all(c.status != "FAIL" for c in q.checks)


def test_page_derived_markup_does_not_fail_the_gate() -> None:
    report = build_report(make_sample_record("gated", xss=True), "en")
    assert check_report(report, lang="en").status != "FAIL"


def test_quality_contract_failures_are_detected() -> None:
    report = build_report(make_sample_record("gated"), "en")
    report.scores.overall.value = 99  # above the gate cap; bypass validators
    report.recommendations[0].depends_on = ["rec_9999"]
    ids = {c.id for c in check_report(report).checks}
    assert {"contract.gate", "refs.resolve"} <= ids


def test_quality_detects_a_dependency_cycle() -> None:
    report = build_report(make_sample_record("gated"), "en")
    a, b = report.recommendations[0], report.recommendations[1]
    a.depends_on, b.depends_on = [b.id], [a.id]
    assert "refs.cycle" in {c.id for c in check_report(report).checks}


def test_missing_translation_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = make_sample_record("gated")
    f = rec.findings[1].model_copy(
        update={
            "id": "x-no-translation",
            "message_key": "finding.x-no-translation",
            "message": "English only",
        }
    )
    rec.findings.append(f)
    extra = Catalog("tests", feature="test")
    extra._rules = {}
    extra._messages = {"en": {"finding.x-no-translation": "English only"}, "tr": {}}
    monkeypatch.setattr(catalog, "_installed", [*catalog.installed(), extra])
    monkeypatch.setattr(catalog, "_merged", None)
    report = build_report(rec, "tr")
    q = check_report(report, lang="tr")
    assert q.status == "WARN" and "text.translation" in {c.id for c in q.checks}
    assert next(x for x in report.findings if x.check_id == "x-no-translation").message == "English only"


async def test_findings_filter_keeps_only_the_asked_severity() -> None:
    report = build_report(make_sample_record("gated"), "en")
    async with fakes.session() as session:
        res = await findings(report, severity=Severity.HIGH, session=session)
    items = res.unwrap().items
    assert items and all(f.severity == Severity.HIGH for f in items)


def test_golden_report_json_is_stable(fixtures: Path) -> None:
    """Golden file for the sample audit (real catalog texts): the source golden minus the LLM keys."""
    report = build_report(make_sample_record("gated"), "en")
    got = json.loads(report.model_dump_json())
    want = json.loads((fixtures / "report" / "gated.en.json").read_text(encoding="utf-8"))
    assert json.loads(json.dumps(got, sort_keys=True)) == want


def test_p25_is_hidden_below_five_pages_and_few_pages_are_explained() -> None:
    rec = make_sample_record("gated")
    rec.scores.p25 = 55.0  # type: ignore[union-attr]
    rec.coverage = {
        "pages_planned": 1,
        "pages_fetched": 1,
        "sitemap_urls": 1,
        "skipped": {"out_of_scope": 2},
    }
    rec.pages = rec.pages[:1]
    report = build_report(rec, "en")
    assert report.scores.p25 is None
    assert any(
        lim.code == "few-pages" and "1 page" in lim.message and "2 link" in lim.message for lim in report.limitations
    )
