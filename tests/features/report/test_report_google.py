"""Search Console + CrUX field-data sections."""

from __future__ import annotations

import json

import pytest

from seoprobe.core.rules.catalog import t
from seoprobe.features.report import build
from seoprobe.features.report import models as s
from seoprobe.features.report.build import build_report
from seoprobe.features.report.quality import _UI, check_report, missing_keys
from seoprobe.features.report.sections import performance, search
from tests import fakes
from tests.features.report.sample import make_sample_record

GSC_KINDS = ["not-connected", "not-configured", "property-not-found", "no-permission", "error"]


def _page(lang: str = "en", **kw: object) -> s.AuditReport:
    return build_report(make_sample_record("gated", **kw), lang)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- builder


def test_gsc_ok_section_has_totals_tables_and_inspection_summary() -> None:
    g = _page(gsc="ok").gsc
    assert g.status == "measured" and g.gsc_status == "ok"
    assert g.property == "sc-domain:example.com" and g.permission == "siteOwner"
    assert g.totals["clicks"] == 1234
    assert g.top_queries[0].ctr_pct == 3.5 and g.top_pages
    assert (g.indexed_count, g.not_indexed_count, g.mismatch_count) == (1, 1, 1)
    assert [i.canonical_mismatch for i in g.inspections] == [False, True, False]
    assert g.inspections[2].error == "quota-exceeded" and g.inspections[2].error_text
    assert g.sitemaps[0].errors == 1
    assert "2-3" in search.gsc_section(make_sample_record("gated", gsc="ok"), "tr").delay_note


@pytest.mark.parametrize("kind", GSC_KINDS)
def test_gsc_unmeasured_states_carry_a_reason_and_no_rows(kind: str) -> None:
    g = _page(gsc=kind).gsc
    assert g.status == "not_measured"
    assert g.not_measured_reason == f"gsc-{kind}" and (g.not_measured_text or "").strip()
    assert not (g.top_queries or g.top_pages or g.inspections or g.sitemaps)
    assert g.can_connect is (kind == "not-connected")


def test_gsc_absent_blob_reads_as_not_connected() -> None:
    report = _page()  # old audits have no stored 'gsc'
    assert report.gsc.gsc_status == "not-connected" and report.gsc.can_connect


def test_field_ok_rates_p75_by_the_cwv_thresholds_and_shares_sum_sensibly() -> None:
    f = _page(field="ok").field
    assert f.status == "measured" and f.scope == "origin" and f.form_factor == "PHONE"
    by = {m.metric: m for m in f.metrics}
    assert [m.metric for m in f.metrics] == ["lcp", "inp", "cls", "ttfb"]
    assert (by["lcp"].rating, by["inp"].rating, by["cls"].rating) == (
        "needs-improvement",
        "good",
        "poor",
    )
    assert by["cls"].p75_text == "0.31" and by["lcp"].p75_text == "3100 ms"
    assert by["ttfb"].good_pct is None  # no distribution: shown as an em dash, never as 0 %
    lcp = by["lcp"]
    assert (lcp.good_pct or 0) + (lcp.needs_improvement_pct or 0) + (lcp.poor_pct or 0) == 100


def test_field_thresholds_match_the_psi_module() -> None:
    # the performance feature does not export its thresholds: the Core Web Vitals values (web.dev)
    for m, bounds in {"lcp": (2500, 4000), "inp": (200, 500), "cls": (0.1, 0.25)}.items():
        assert performance.FIELD_THRESHOLDS[m] == bounds


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("no-data", "field-no-data"), ("no-api-key", "field-no-api-key"), ("error", "field-error")],
)
def test_field_unmeasured_reasons(kind: str, reason: str) -> None:
    report = _page(field=kind)
    assert report.field.status == "not_measured" and report.field.not_measured_reason == reason
    assert not report.field.metrics


def test_field_not_run_reason() -> None:
    assert _page().field.not_measured_reason == "field-not-run"


def test_data_sources_list_search_console_and_crux() -> None:
    ok = {d.id: d.status for d in _page(gsc="ok", field="ok").data_sources}
    assert ok["gsc"] == "ok" and ok["crux"] == "ok"
    bad = {d.id: d.status for d in _page(gsc="error", field="error").data_sources}
    assert bad["gsc"] == "failed" and bad["crux"] == "failed"
    miss = {d.id: d.status for d in _page(gsc="not-connected", field="no-data").data_sources}
    assert miss["gsc"] == "missing" and miss["crux"] == "missing"


def test_scope_box_drops_the_indexing_item_only_when_search_console_is_ok() -> None:
    connected = _page(gsc="ok").scope_note
    off = _page(gsc="not-connected").scope_note
    assert connected is not None and off is not None
    assert not any("Indexing status" in i for i in connected.items)
    assert any("Indexing status" in i for i in off.items)
    # the sample audit also has ranking data when connected, so that item goes too
    assert not any("Rankings" in i for i in connected.items)
    assert any("Rankings" in i for i in off.items)
    for kept in ("Backlinks", "Keyword targeting", "Field speed", "competitors"):
        assert any(kept in i for i in connected.items)  # nothing measured these here
    assert "connect your Google account" in off.next_step
    assert "roadmap" not in off.next_step and "roadmap" not in connected.next_step


def test_visibility_search_console_item_follows_the_connection() -> None:
    def item(**kw: object) -> s.VisItem:
        r = build_report(make_sample_record("gated", **kw), "en")  # type: ignore[arg-type]
        return next(i for i in r.visibility.items if i.id == "search_console")

    assert item(gsc="ok").available and item(gsc="ok").value == "Connected"
    assert not item(gsc="not-connected").available


# --------------------------------------------------------------------------- quality gate


@pytest.mark.parametrize("gsc", [None, "ok", *GSC_KINDS])
@pytest.mark.parametrize("field", [None, "ok", "no-data", "no-api-key", "error"])
def test_quality_gate_passes_for_every_gsc_and_field_state(gsc: str | None, field: str | None) -> None:
    report = build_report(make_sample_record("gated", gsc=gsc, field=field), "en")
    q = check_report(report, lang="en")
    assert q.status != "FAIL", q.checks


def test_quality_gate_rejects_rows_in_an_unmeasured_gsc_section() -> None:
    report = build_report(make_sample_record("gated", gsc="not-connected"), "en")
    report.gsc.top_queries = build_report(make_sample_record("gated", gsc="ok"), "en").gsc.top_queries
    q = check_report(report, lang="en")
    assert any(c.id == "deep.gsc" and c.status == "FAIL" for c in q.checks)


def test_i18n_catalogs_have_identical_keys_and_new_keys_exist() -> None:
    assert missing_keys() == set()
    for key in ("sec.gsc", "sec.field", "gsc.note", "field.note"):  # home.* texts belonged to the app
        assert key in _UI.messages["tr"] and key in _UI.messages["en"]
    assert t("en", "gsc.note") != "gsc.note"


def test_long_gsc_lists_are_truncated_and_flagged() -> None:
    record = make_sample_record("gated", gsc="ok")
    assert record.gsc is not None
    record.gsc.top_queries = record.gsc.top_queries * 30
    report = build_report(record, "en")
    assert len(report.gsc.top_queries) == search.GSC_ROWS and report.gsc.truncated


# --------------------------------------------------------------------------- operation


async def test_report_json_includes_the_new_sections() -> None:
    async with fakes.session() as session:
        res = await build(make_sample_record("gated", gsc="ok", field="ok"), lang="en", session=session)
    body = json.loads(res.to_json())["data"]
    assert body["gsc"]["status"] == "measured" and body["field"]["status"] == "measured"
    assert {d["id"] for d in body["data_sources"]} >= {"gsc", "crux"}
