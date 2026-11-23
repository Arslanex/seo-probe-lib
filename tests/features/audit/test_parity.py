"""
Behavioural parity with the source project: the snapshots in ``tests/fixtures/parity`` are the
source pipeline's output on the demo site (see the README there). The same scenarios must give the
same findings, not-checked entries, scores, pages, coverage, analysis data and report.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from seoprobe.features.audit import Audit, audit_page, audit_site
from seoprobe.features.report.build import build_report
from tests.features.audit.support import BASE, demo_session, no_transport

SEED = 12345
SCENARIOS = [
    ("page_hakkimizda", f"{BASE}/hakkimizda", "page", {}),
    ("page_home", f"{BASE}/", "page", {"keywords": ["demo hizmet"]}),
    ("site_home", f"{BASE}/", "site", {"keywords": ["demo hizmet"]}),
]
PAGE_KEYS = (
    "url",
    "final_url",
    "status",
    "sample_kind",
    "template_key",
    "title",
    "word_count",
    "page_score",
    "rendered",
    "is_spa",
    "error",
)
FINDING_KEYS = ("id", "url", "severity", "category", "scope", "evidence", "count", "confidence")
CATEGORY_KEYS = ("category", "score", "measured", "reason", "checks_total", "checks_passed", "checks_failed")
SCORE_KEYS = (
    "overall",
    "raw_overall",
    "coverage_pct",
    "insufficient_data",
    "gate",
    "confidence",
    "core_overall",
    "p25",
)

# Report parts the report feature removed or rewrote on purpose (LLM, app links, library wording).
REMOVED_REPORT_KEYS = ("advisory", "synthesis")
REWRITTEN_KEYS = (
    "reason.no-api-key",
    "reason.field-no-api-key",
    "reason.gsc-not-configured",
    "reason.no-competitors",
    "reason.no-valid-competitors",
    "reason.rankings-no-keywords",
    "read.kw.none",
    "rank.kw.none",
    "rank.keywords.missing",
    "reason.gsc-property-not-found",
    "reason.rankings-not-run",
    "plan.monitor1",
    "field.note",
    "scope.i5",
    "scope.next",
    "score.insufficient.text",
    "summary.none.note",
    "meth.gate",
    "finding.above-fold-no-h1",  # names the device in the evidence instead of "desktop or mobile"
)
# Intended difference: the source registered this pipeline rule without texts, so its report
# showed the bare key; the library catalog gives it real texts (message and recommendation).
INTENDED_NEW_RULE_TEXTS = ("finding.schema-none-found",)
PROBE = re.compile(r"/[0-9a-f]{24}-(?:seoa|seoprobe)-probe")


def snapshot(audit: Audit) -> dict[str, Any]:
    """The same projection as the source's make_parity.py."""
    d = audit.model_dump(mode="json")
    sc = d["scores"] or {}
    return {
        "status": d["status"],
        "mode": d["mode"],
        "target_url": d["target_url"],
        "final_origin": d["final_origin"],
        "business_type": d["business_type"],
        "lang": d["lang"],
        "error": d["error"],
        "coverage": d["coverage"],
        "findings": sorted(
            ({k: f.get(k) for k in FINDING_KEYS} for f in d["findings"]),
            key=lambda f: (f["id"], f["url"] or "", f["evidence"] or "", f["count"] or 0),
        ),
        "not_checked": sorted(d["not_checked"], key=lambda n: (n["check_id"], n["reason_code"], n["url"] or "")),
        "pages": sorted(({k: p.get(k) for k in PAGE_KEYS} for p in d["pages"]), key=lambda p: p["url"]),
        "scores": {
            **{k: sc.get(k) for k in SCORE_KEYS},
            "categories": [{k: c.get(k) for k in CATEGORY_KEYS} for c in sc.get("categories", [])],
        },
        # skipped_time_budget is a library addition (the source has no link-check time budget)
        "links": {k: v for k, v in d["links"].items() if k != "skipped_time_budget"},
        "indexability": sorted(d["indexability"], key=lambda r: r["url"]),
        "extras": d["extras"],
    }


def _norm(value: Any) -> Any:
    """Random or time-based values: the soft-404 probe token."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return json.loads(PROBE.sub("/<probe>", text))


def _norm_report(report: dict[str, Any]) -> dict[str, Any]:
    r = dict(report)
    for k in REMOVED_REPORT_KEYS:
        r.pop(k, None)
    audit = dict(r["audit"])
    for k in ("id", "started_at", "finished_at", "parent_audit_id"):
        audit.pop(k, None)
    r["audit"] = audit
    # rule_version is this library's label for its rule set (0.1.0), not behaviour: the source says 1.0
    r["versions"] = {k: v for k, v in r["versions"].items() if k not in ("rubric_version", "rule_version")}
    r["findings"] = [{k: v for k, v in f.items() if k != "rule_version"} for f in r["findings"]]
    r["usage"] = {
        k: v for k, v in r["usage"].items() if k not in ("wall_ms", "llm_cost_usd", "input_tokens", "output_tokens")
    }
    r["data_sources"] = [x for x in r["data_sources"] if x["id"] != "llm"]
    r["limitations"] = [x for x in r["limitations"] if x["code"] != "ai-unavailable"]
    r["recommendations"] = [{k: v for k, v in x.items() if k != "context"} for x in r["recommendations"]]
    r["sitemap_gen"] = {k: v for k, v in r["sitemap_gen"].items() if k != "download_href"}
    r["history"] = None  # read from the source's database; here only from ``previous=`` (not_measured)
    return _norm(r)


def _diff(got: Any, want: Any, path: str = "") -> list[str]:
    if isinstance(got, dict) and isinstance(want, dict):
        out: list[str] = []
        for k in sorted(set(got) | set(want)):
            if k not in got:
                out.append(f"{path}.{k}: missing (want {str(want[k])[:200]})")
            elif k not in want:
                out.append(f"{path}.{k}: unexpected {str(got[k])[:200]}")
            else:
                out += _diff(got[k], want[k], f"{path}.{k}")
        return out
    if isinstance(got, list) and isinstance(want, list) and len(got) == len(want):
        return [d for i, (g, w) in enumerate(zip(got, want, strict=True)) for d in _diff(g, w, f"{path}[{i}]")]
    return [] if got == want else [f"{path}: got {str(got)[:300]} want {str(want)[:300]}"]


def _rewritten_texts() -> set[str]:
    """
    Every text of a rewritten key, old (source) and new (library) wording alike: a report field
    that differs only by such a text is an intended difference.
    """
    from seoprobe.core.rules import catalog

    return {catalog.t("en", k) for k in REWRITTEN_KEYS}


def _intended(line: str, rewritten: set[str]) -> bool:
    if any(line.endswith(f" want {key}") for key in INTENDED_NEW_RULE_TEXTS):
        return True
    return any(t and t[:60] in line for t in rewritten)


async def _run(name: str, url: str, mode: str, kw: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Audit:
    no_transport(monkeypatch)
    async with demo_session() as s:  # render and visibility off, no API keys
        op = audit_site if mode == "site" else audit_page
        res = await op(url, seed=SEED, session=s, **kw)
    assert res.ok, res.error
    assert res.data is not None
    return res.data


@pytest.mark.parametrize(("name", "url", "mode", "kw"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_audit_matches_the_source_snapshot(
    name: str, url: str, mode: str, kw: dict[str, Any], fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want = json.loads((fixtures / "parity" / f"{name}.json").read_text(encoding="utf-8"))
    audit = await _run(name, url, mode, kw, monkeypatch)
    got = _norm(snapshot(audit))
    expected = _norm({k: v for k, v in want.items() if k != "report_en"})
    assert _diff(got, expected) == []


@pytest.mark.parametrize(("name", "url", "mode", "kw"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_report_matches_the_source_snapshot(
    name: str, url: str, mode: str, kw: dict[str, Any], fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want = json.loads((fixtures / "parity" / f"{name}.json").read_text(encoding="utf-8"))["report_en"]
    audit = await _run(name, url, mode, kw, monkeypatch)
    report = build_report(audit, "en").model_dump(mode="json")
    assert (report["history"]["status"], report["history"]["not_measured_reason"]) == (
        "not_measured",
        "history-unavailable",
    )
    got = _norm_report(report)
    rewritten = _rewritten_texts()
    diffs = [d for d in _diff(got, _norm_report(want)) if not _intended(d, rewritten)]
    assert diffs == []
