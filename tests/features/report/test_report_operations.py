"""The report operations: JSON-serialisable results, filters, the plan, and rule ids in the catalog."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from seoprobe.core.models import Category, Layer
from seoprobe.core.rules import catalog
from seoprobe.features.report import build, compare, findings, plan
from seoprobe.features.report.build import build_report
from seoprobe.features.report.models import Quality, QualityCheck
from tests import fakes
from tests.features.report.sample import make_sample_record


async def test_build_returns_a_json_serialisable_result_with_the_quality_gate() -> None:
    async with fakes.session() as session:
        res = await build(make_sample_record("gated", gsc="ok", field="ok"), session=session)
    assert res.ok and res.status == "ok" and res.meta.requests == []
    body = json.loads(res.to_json())
    assert body["operation"] == "report.build" and body["data"]["quality"]["status"] in ("PASS", "WARN")
    assert body["data"]["audit"]["locale"] == "en"  # the session language


async def test_build_uses_the_given_language() -> None:
    async with fakes.session() as session:
        res = await build(make_sample_record("gated"), lang="tr", session=session)
    assert res.unwrap().audit.locale == "tr"


async def test_build_never_fails_on_a_failing_quality_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(report: object, **_: object) -> Quality:
        return Quality(status="FAIL", checks=[QualityCheck(id="schema", status="FAIL")])

    build_module = importlib.import_module("seoprobe.features.report.build")  # the package exports the op as `build`
    monkeypatch.setattr(build_module, "check_report", failing)
    async with fakes.session() as session:
        res = await build(make_sample_record("gated"), session=session)
    assert res.ok and res.unwrap().quality.status == "FAIL"


async def test_plan_is_the_report_action_plan() -> None:
    report = build_report(make_sample_record("gated"), "en")
    async with fakes.session() as session:
        res = await plan(report, session=session)
    assert res.ok and res.unwrap() == report.action_plan
    json.loads(res.to_json())


async def test_compare_result_is_json_serialisable() -> None:
    async with fakes.session() as session:
        res = await compare(make_sample_record("gated"), make_sample_record("clean"), session=session)
    data = json.loads(res.to_json())["data"]
    assert data["resolved_total"] > 0 and data["new_total"] == 0


async def test_findings_filters_and_priority_sort() -> None:
    report = build_report(make_sample_record("gated"), "en")
    async with fakes.session() as session:
        every = (await findings(report, session=session)).unwrap().items
        assert [f.id for f in every] == [f.id for f in report.findings]
        onpage = (await findings(report, category=Category.ONPAGE, layer=Layer.A, session=session)).unwrap()
        site = (await findings(report, scope="site", session=session)).unwrap()
        one = (await findings(report, check_id="noindex", session=session)).unwrap()
        res = await findings(report, sort="priority", session=session)
    assert onpage.items and all(f.category == Category.ONPAGE and f.layer == Layer.A for f in onpage.items)
    assert [f.check_id for f in site.items] == ["hsts-missing"]
    assert [f.check_id for f in one.items] == ["noindex"]
    prio = {r.finding_ids[0]: r.priority_score for r in report.recommendations}
    scores = [prio[f.id] for f in res.unwrap().items]
    assert scores == sorted(scores, reverse=True)
    json.loads(res.to_json())


async def test_findings_rejects_an_unknown_sort() -> None:
    report = build_report(make_sample_record("gated"), "en")
    with pytest.raises(ValueError):
        await findings(report, sort="random")  # type: ignore[arg-type]


def _source() -> str:
    """Every module of the report package, the sections subpackage included."""
    root = Path(str(importlib.import_module("seoprobe.features.report").__file__)).parent
    return "".join(f.read_text(encoding="utf-8") for f in sorted(root.rglob("*.py")))


def test_every_rule_id_the_code_names_is_in_the_catalog() -> None:
    src = _source()
    named = set(re.findall(r'f\.id == "([a-z0-9-]+)"', src))
    assert named == {"robots-disallows-entry"}
    assert named <= set(catalog.rules())


def test_every_literal_text_key_the_code_uses_has_both_languages() -> None:
    src = _source()
    keys = set(re.findall(r't\(lang, "([a-z0-9_.-]+)"', src))
    assert len(keys) > 100
    missing = sorted(k for k in keys if catalog.t("en", k) == k or catalog.t("tr", k) == k)
    assert missing == []
