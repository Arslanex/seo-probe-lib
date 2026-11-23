from __future__ import annotations

from seoprobe.features.performance.vitals import parse_psi, rate


def test_rating_thresholds() -> None:
    assert rate("lcp", 2500) == "good" and rate("lcp", 3000) == "needs-improvement" and rate("lcp", 4001) == "poor"
    assert rate("inp", 200) == "good" and rate("inp", 501) == "poor"
    assert rate("cls", 0.1) == "good" and rate("cls", 0.3) == "poor"


def test_field_data_wins_over_lab() -> None:
    data = {
        "lighthouseResult": {"audits": {"largest-contentful-paint": {"numericValue": 5000}}},
        "loadingExperience": {
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2000},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 600},
            }
        },
    }
    out = parse_psi(data, "https://e.com/")
    ids = {f.id for f in out.findings}
    assert "cwv-inp-poor" in ids and "cwv-lcp-poor" not in ids and out.facts["cwv_lcp"]["source"] == "field"


def test_missing_field_inp_is_not_checked_not_passed() -> None:
    out = parse_psi({"lighthouseResult": {"audits": {"largest-contentful-paint": {"numericValue": 1000}}}}, "u")
    assert out.findings == [] and out.not_checked[0].reason_code == "field-data-unavailable"


def test_finding_evidence_names_the_unit_and_where_the_value_comes_from() -> None:
    lab = {"lighthouseResult": {"audits": {"largest-contentful-paint": {"numericValue": 2511.88}}}}
    f = parse_psi(lab, "https://e.com/").findings[0]
    assert f.evidence == "LCP 2512 ms (PageSpeed mobile lab)"
    field = {"loadingExperience": {"metrics": {"INTERACTION_TO_NEXT_PAINT": {"percentile": 600}}}}
    assert parse_psi(field, "https://e.com/").findings[0].evidence == "INP 600 ms (field)"
