"""perf_checks: thresholds, device handling, images, failed renders (pure)."""

from __future__ import annotations

from seoprobe.core.models import Category, Severity
from seoprobe.core.rules import catalog
from seoprobe.features.performance import ImageRecord, PagePerf
from seoprobe.features.performance import vitals as rules_perf
from seoprobe.features.performance.vitals import perf_checks

URL = "https://e.com/"
MIB = 1024 * 1024


def _p(**kw: object) -> PagePerf:
    base: dict[str, object] = {
        "url": URL,
        "device": "desktop",
        "lcp_ms": 1200,
        "cls": 0.01,
        "total_bytes": MIB,
        "total_requests": 40,
    }
    base.update(kw)
    return PagePerf(**base)  # type: ignore[arg-type]


def _ids(out) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.id for f in out.findings}


def test_clean_page_has_no_findings_and_inp_is_marked_field_only() -> None:
    out = perf_checks([_p()])
    assert out.findings == []
    assert {n.check_id for n in out.not_checked} == {"cwv-inp-poor", "cwv-inp-needs-improvement"}
    assert all(n.reason_code == "field-data-unavailable" and n.url is None for n in out.not_checked)


def test_lcp_thresholds_use_the_published_cwv_values() -> None:
    assert _ids(perf_checks([_p(lcp_ms=2500)])) == set()
    assert _ids(perf_checks([_p(lcp_ms=2501)])) == {"cwv-lcp-needs-improvement"}
    assert _ids(perf_checks([_p(lcp_ms=4000)])) == {"cwv-lcp-needs-improvement"}
    out = perf_checks([_p(lcp_ms=4001)])
    assert _ids(out) == {"cwv-lcp-poor"}
    f = out.findings[0]
    assert f.severity == Severity.HIGH and f.url == URL and "LCP 4001 ms (desktop, lab)" in (f.evidence or "")


def test_cls_thresholds() -> None:
    assert _ids(perf_checks([_p(cls=0.1)])) == set()
    assert _ids(perf_checks([_p(cls=0.11)])) == {"cwv-cls-needs-improvement"}
    assert _ids(perf_checks([_p(cls=0.26)])) == {"cwv-cls-poor"}


def test_missing_metrics_produce_nothing() -> None:
    assert perf_checks([_p(lcp_ms=None, cls=None)]).findings == []


def test_page_weight_and_request_boundaries() -> None:
    assert "page-weight-heavy" not in _ids(perf_checks([_p(total_bytes=3 * MIB)]))
    assert "page-weight-heavy" in _ids(perf_checks([_p(total_bytes=3 * MIB + 1)]))
    assert "too-many-requests" not in _ids(perf_checks([_p(total_requests=100)]))
    assert "too-many-requests" in _ids(perf_checks([_p(total_requests=101)]))


def test_uncompressed_cache_and_render_blocking() -> None:
    out = perf_checks([_p(uncompressed_text=["https://e.com/a.js", "https://e.com/b.css"])])
    f = next(x for x in out.findings if x.id == "uncompressed-text-resources")
    assert f.count == 2 and "/a.js" in (f.evidence or "")
    assert "static-assets-no-cache" not in _ids(
        perf_checks([_p(no_cache_static=["https://e.com/1", "https://e.com/2"])])
    )
    assert "static-assets-no-cache" in _ids(perf_checks([_p(no_cache_static=[f"https://e.com/{i}" for i in range(3)])]))
    assert "render-blocking-resources" not in _ids(perf_checks([_p(render_blocking=3)]))
    assert "render-blocking-resources" in _ids(perf_checks([_p(render_blocking=4)]))


def test_third_party_by_requests_or_byte_share() -> None:
    by_req = [{"host": "a.net", "requests": 25, "bytes": 1000}]
    assert "third-party-heavy" in _ids(perf_checks([_p(third_party=by_req)]))
    assert "third-party-heavy" not in _ids(
        perf_checks([_p(third_party=[{"host": "a.net", "requests": 24, "bytes": 1000}])])
    )
    by_bytes = [{"host": "a.net", "requests": 2, "bytes": int(0.41 * MIB)}]
    assert "third-party-heavy" in _ids(perf_checks([_p(third_party=by_bytes)]))
    assert "third-party-heavy" not in _ids(
        perf_checks([_p(third_party=[{"host": "a.net", "requests": 2, "bytes": int(0.39 * MIB)}])])
    )


def test_mobile_checks_only_come_from_the_mobile_run() -> None:
    mob = {
        "horizontal_overflow": True,
        "small_font_share": 0.3,
        "small_tap_targets": 3,
        "tap_target_examples": ["A x"],
        "viewport_meta": True,
    }
    assert _ids(perf_checks([_p(mobile=mob)])) == set()  # a desktop run never reports mobile findings
    out = perf_checks([_p(device="mobile", mobile=mob)])
    assert _ids(out) == {
        "mobile-horizontal-scroll",
        "mobile-font-too-small",
        "mobile-tap-targets-small",
    }
    quiet = {
        "horizontal_overflow": False,
        "small_font_share": 0.19,
        "small_tap_targets": 2,
        "viewport_meta": True,
    }
    assert _ids(perf_checks([_p(device="mobile", mobile=quiet)])) == set()


def test_page_level_rules_ignore_the_mobile_run() -> None:
    out = perf_checks([_p(device="mobile", total_bytes=9 * MIB, total_requests=500)])
    assert _ids(out) == set()


def _img(src: str, nbytes: int, *issues: str) -> ImageRecord:
    return ImageRecord(page_url=URL, src=src, bytes=nbytes, issues=list(issues))


def test_image_findings_are_one_per_issue_and_page_with_the_heaviest_first() -> None:
    imgs = [
        _img("https://e.com/a.png", 300_000, "image-too-large", "image-legacy-format"),
        _img("https://e.com/b.png", 900_000, "image-critically-large", "image-legacy-format"),
        _img("https://e.com/c.webp", 5000),
    ]
    out = perf_checks([_p(images=imgs)])
    assert _ids(out) == {"image-too-large", "image-critically-large", "image-legacy-format"}
    legacy = next(f for f in out.findings if f.id == "image-legacy-format")
    assert legacy.count == 2 and (legacy.evidence or "").startswith("/b.png (879 KB)")
    assert all(f.category == Category.IMAGES for f in out.findings)


def test_an_image_repeated_on_the_page_is_listed_and_counted_once() -> None:
    logo = _img("https://e.com/logo.png", 31_000, "image-oversized-for-display")
    out = perf_checks([_p(images=[logo, logo, _img("https://e.com/x.png", 40_000)])])
    f = next(f for f in out.findings if f.id == "image-oversized-for-display")
    assert f.count == 1 and (f.evidence or "").count("/logo.png") == 1


def test_two_devices_do_not_double_count_the_same_rule_on_a_page() -> None:
    imgs = [_img("https://e.com/a.png", 900_000, "image-critically-large")]
    out = perf_checks([_p(images=imgs), _p(device="mobile", images=imgs, lcp_ms=5000)])
    assert [f.id for f in out.findings].count("image-critically-large") == 1
    assert "cwv-lcp-poor" in _ids(out)  # a mobile-only verdict still shows up


def test_failed_render_marks_checks_not_checked_never_passed() -> None:
    d = PagePerf(url=URL, device="desktop", error="render-failed: timeout")
    m = PagePerf(url=URL, device="mobile", error="render-failed: timeout")
    out = perf_checks([d, m])
    assert out.findings == []
    by_dev = {(n.check_id, n.url) for n in out.not_checked}
    assert ("cwv-lcp-poor", URL) in by_dev and ("page-weight-heavy", URL) in by_dev
    assert ("mobile-horizontal-scroll", URL) in by_dev and ("image-too-large", URL) in by_dev
    assert all(n.reason_code == "render-failed" for n in out.not_checked)
    assert not any(n.check_id.startswith("cwv-inp") for n in out.not_checked)  # nothing was measured


def test_one_good_one_failed_page() -> None:
    out = perf_checks([_p(), PagePerf(url="https://e.com/b", error="target-rejected: x")])
    assert any(n.url == "https://e.com/b" for n in out.not_checked)
    assert any(n.check_id == "cwv-inp-poor" for n in out.not_checked)
    assert out.facts["lab_pages_measured"] == 1


def test_every_rule_is_registered_requires_lab_and_has_four_texts_in_two_languages() -> None:
    rules = catalog.rules()
    for rid in rules_perf.PERF_RULE_IDS:
        assert rules[rid].requires == ("lab",) and rules[rid].modes == (
            "page",
            "site",
        )
        for prefix in ("finding", "rec", "fail", "lead"):
            for lang in ("en", "tr"):
                assert catalog.message(f"{prefix}.{rid}", lang) != f"{prefix}.{rid}", (
                    rid,
                    prefix,
                    lang,
                )
    for rid in rules_perf.LAB_RULE_IDS:
        assert rid in rules


def test_severities_and_categories_match_the_design() -> None:
    r = catalog.rules()
    assert r["image-critically-large"].severity == Severity.HIGH and r["image-too-large"].severity == Severity.MEDIUM
    assert r["mobile-horizontal-scroll"].category == Category.TECHNICAL
    assert r["page-weight-heavy"].category == Category.PERFORMANCE and r["page-weight-heavy"].heuristic
    assert not r["uncompressed-text-resources"].heuristic
