"""Score v2 presentation and the deep-analysis sections."""

from __future__ import annotations

import json

import pytest

from seoprobe.core.models import Category
from seoprobe.core.rules import catalog
from seoprobe.features.crawl import LinkRecord
from seoprobe.features.report import models as s
from seoprobe.features.report.build import build_report
from seoprobe.features.report.quality import check_report, missing_keys
from seoprobe.features.report.sections import crawl, performance
from seoprobe.features.report.sections.crawl import _link_state
from tests.features.report.sample import XSS_EVIDENCE, make_sample_record

SECTIONS = ("links", "performance", "images", "indexability", "visibility")


def _report(variant: str, lang: str, **kw: object) -> s.AuditReport:
    return build_report(make_sample_record(variant, **kw), lang)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- scope box


def test_scope_note_names_what_is_not_measured() -> None:
    box = _report("gated", "en").scope_note
    assert box is not None
    text = " ".join([box.title, box.intro, *box.items, box.next_step, box.model_note])
    for needle in (
        "indexed",
        "Rankings",
        "Backlinks",
        "Keyword",
        "Field speed",
        "competitors",
        "Search Console",
        "calibrated",
    ):
        assert needle in text, needle


def test_scope_note_is_part_of_the_json_report_in_both_languages() -> None:
    for lang in ("tr", "en"):
        r = build_report(make_sample_record("gated"), lang)
        assert r.scope_note and len(r.scope_note.items) == 6 and r.scope_note.next_step


# --------------------------------------------------------------------------- category checks table


def test_every_measured_category_lists_the_checks_behind_its_score() -> None:
    r = build_report(make_sample_record("gated"), "en")
    for c in r.scores.categories:
        if c.status == "measured":
            assert c.checks and c.checks_total == len(c.checks) == c.checks_passed + c.checks_failed
        else:
            assert not c.checks and c.checks_total == 0
    links = next(c for c in r.scores.categories if c.id == Category.LINKS)
    assert links.status == "measured" and links.label == "Links"


def test_the_check_message_is_readable_not_only_an_id() -> None:
    r = build_report(make_sample_record("gated"), "tr")
    tech = next(c for c in r.scores.categories if c.id == Category.TECHNICAL)
    hsts = next(k for k in tech.checks if k.check_id == "hsts-missing")
    assert hsts.message != "hsts-missing" and hsts.status == "fail"


# --------------------------------------------------------------------------- sections


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_all_five_deep_sections_carry_real_data(lang: str) -> None:
    report = _report("gated", lang)
    for sid in SECTIONS:
        assert getattr(report, sid).status == "measured", f"{sid} unexpectedly not measured"
    assert check_report(report, lang=lang).status in ("PASS", "WARN")


def test_links_section_shows_broken_redirecting_domains_pages_and_orphans() -> None:
    report = _report("gated", "en")
    L = report.links
    assert L.status == "measured" and [r.state for r in L.broken] == ["broken", "broken"]
    assert L.redirecting[0].final_url and L.orphans and L.external_domains[0].domain == "twitter.com"
    assert L.broken[1].error == "timeout" and L.broken[1].error_text == "timed out"


def test_performance_section_is_labelled_lab_and_rates_with_text() -> None:
    report = _report("gated", "en")
    P = report.performance
    desktop = next(r for r in P.rows if r.device == "desktop")
    mobile = next(r for r in P.rows if r.device == "mobile" and not r.error)
    assert desktop.ratings["lcp_ms"] == "needs-improvement" and desktop.ratings["cls"] == "good"
    assert mobile.ratings["lcp_ms"] == "poor" and mobile.ratings["cls"] == "poor"
    assert "dom_content_loaded_ms" not in desktop.ratings  # no published threshold: no rating
    assert P.lab_note
    assert P.by_type and P.third_party and P.uncompressed and P.no_cache
    assert mobile.mobile and mobile.mobile["horizontal_overflow"] is True
    err = next(r for r in P.rows if r.error)
    assert err.error == "render-failed" and err.error_text


def test_images_section_totals_thresholds_and_issue_texts() -> None:
    report = _report("gated", "en")
    im = report.images
    assert im.rows_total == 3 and im.rows[0].kb and im.rows[0].kb > (im.rows[1].kb or 0)  # heaviest first
    assert im.total_kb == pytest.approx(1117.2, abs=0.2)
    assert {f.format for f in im.by_format} == {"png", "jpeg", "webp"}
    assert "50 KB" in im.thresholds_note and "AVIF" in im.thresholds_note
    assert im.rows[0].issue_texts and im.rows[0].category == "hero"
    assert "hero.png" in im.rows[0].src


def test_indexability_section_says_blocker_not_indexed_status() -> None:
    report = _report("gated", "en")
    X = report.indexability
    assert X.blocked_count == 3 and X.indexable_count == 0
    assert X.rows[0].reason_texts and "noindex tag" in X.rows[0].reason_texts
    assert "does not show whether Google has actually indexed" in X.note
    clean = _report("clean", "en")
    assert clean.indexability.indexable_count >= 1


def test_visibility_section_is_informational_and_translates_notes() -> None:
    report = _report("gated", "tr")
    V = report.visibility
    assert V.counts_toward_overall is False and V.status == "measured"
    assert any("6 aydan yeni" in n for n in V.notes)
    by_id = {i.id: i for i in V.items}
    assert by_id["search_console"].value == "Bağlı değil" and not by_id["search_console"].available
    assert by_id["rdap"].value and "104" in by_id["rdap"].value
    assert "kanıtı değildir" in V.disclaimer
    assert "scores" not in json.dumps(V.model_dump(mode="json")).lower()


def test_visibility_notes_never_print_the_not_connected_code_when_search_console_is_connected() -> None:
    rec = make_sample_record("gated", gsc="ok")
    assert rec.visibility is not None
    rec.visibility.notes.append("gsc-not-connected")
    section = crawl.visibility_section(rec, "en")
    assert not any("gsc-not-connected" in n for n in section.notes)
    assert {i.id: i for i in section.items}["search_console"].available


def test_visibility_errors_are_shown_politely_and_unknown_notes_do_not_leak_codes_as_prose() -> None:
    report = _report("partial", "en")
    by_id = {i.id: i for i in report.visibility.items}
    assert by_id["rdap"].error_text == "RDAP data is not offered for this domain extension."
    assert by_id["wayback"].error_text == "The source did not answer in time."
    assert by_id["common_crawl"].error_text == "The source could not be reached."
    assert all(not by_id[k].available for k in ("rdap", "wayback", "common_crawl"))
    assert report.visibility.notes == ["Note code: some-unknown-note"]


def test_a_partial_wayback_answer_still_shows_its_dates() -> None:
    rec = make_sample_record("gated")
    assert rec.visibility is not None
    rec.visibility.wayback = {
        "first_capture": "1997-05-01",
        "last_capture": "2026-11-23",
        "captures": None,
        "urls_archived_sample": None,
        "error": "sample-timeout",  # the slow domain-wide URL sample did not answer
    }
    item = {i.id: i for i in crawl.visibility_section(rec, "en").items}["wayback"]
    assert item.available and "1997" in (item.value or "") and item.detail is None


def test_no_deep_analysis_shows_not_measured_with_reasons_and_never_an_empty_table() -> None:
    report = _report("no-deep-analysis", "en")
    for name in SECTIONS:
        sec = getattr(report, name)
        assert sec.status == "not_measured" and sec.not_measured_reason and sec.not_measured_text
    assert report.links.not_measured_reason == "analysis-not-run"
    assert report.performance.not_measured_reason == "lab-unavailable"
    assert report.visibility.not_measured_reason == "visibility-unavailable"
    assert not report.links.links and not report.performance.rows and not report.images.rows
    assert check_report(report, lang="en").status in ("PASS", "WARN")
    perf = next(c for c in report.scores.categories if c.id == Category.PERFORMANCE)
    assert perf.status == "not_measured"


def test_failed_audit_has_no_deep_sections_and_still_passes_the_gate() -> None:
    report = _report("failed", "en")
    assert report.links.status == "not_measured"
    assert check_report(report, lang="en").status in ("PASS", "WARN")


# --------------------------------------------------------------------------- caps, XSS, quality gate


def test_big_lists_are_capped_and_flagged_as_truncated() -> None:
    rec = make_sample_record("gated")
    assert rec.links is not None
    rec.links.links = rec.links.links * 80  # 480 links
    rec.links.pages = rec.links.pages * 80  # 320 page rows
    report = build_report(rec, "en")
    assert len(report.links.links) == crawl.LINK_ROWS and report.links.truncated
    assert len(report.links.pages) == crawl.PAGE_ROWS and report.links.pages_total == 320


def test_page_derived_markup_is_kept_as_text_and_passes_the_gate() -> None:
    report = _report("gated", "en", xss=True)
    assert any(XSS_EVIDENCE == r.anchor for r in report.links.links)  # data, never interpreted
    q = check_report(report, lang="en")
    assert q.status != "FAIL", q.checks


def test_quality_gate_fails_when_a_not_measured_section_carries_rows() -> None:
    report = _report("gated", "en")
    bad = report.model_copy(deep=True)
    bad.images.status = "not_measured"
    q = check_report(bad, lang="en")
    assert q.status == "FAIL" and any(c.id == "deep.images" for c in q.checks)


def test_catalogs_stay_in_sync_and_new_keys_exist_in_both_languages() -> None:
    assert missing_keys() == set()
    for key in (
        "cat.links",
        "stage.analyze",
        "scope.title",
        "sec.links",
        "sec.perf",
        "sec.images",
        "sec.index",
        "sec.vis",
        "perf.lab",
        "idx.note",
    ):
        assert catalog.t("tr", key) != key and catalog.t("en", key) != key
    assert catalog.t("tr", "cat.links") == "Bağlantılar" and catalog.t("tr", "stage.analyze") == "Derin analiz"
    assert catalog.t("en", "stage.analyze") == "Deep analysis"
    for code in ("analysis-not-run", "lab-unavailable", "visibility-unavailable"):
        assert catalog.reason_text("tr", code) != code and catalog.reason_text("en", code) != code


def test_rating_thresholds_follow_core_web_vitals() -> None:
    assert performance.rate("lcp_ms", 2500) == "good" and performance.rate("lcp_ms", 4001) == "poor"
    assert performance.rate("cls", 0.1) == "good" and performance.rate("cls", 0.26) == "poor"
    assert performance.rate("ttfb_ms", 900) == "needs-improvement" and performance.rate("lcp_ms", None) is None


def test_bot_blocking_sites_are_not_reported_as_broken_links() -> None:
    def lk(**kw: object) -> LinkRecord:
        return LinkRecord(source_url="https://a/", target_url="https://b/", kind="external", checked=True, **kw)  # type: ignore[arg-type]

    assert _link_state(lk(target_status=999)) == "blocked"  # LinkedIn
    assert _link_state(lk(target_status=403)) == "blocked"
    assert _link_state(lk(target_status=404)) == "broken"
    assert _link_state(lk(error="timeout")) == "broken"
    assert _link_state(lk(error="circuit-open")) == "unchecked"
    assert _link_state(lk(error="target-rejected")) == "unchecked"
    assert _link_state(lk(target_status=200, redirect_hops=2)) == "redirect"
