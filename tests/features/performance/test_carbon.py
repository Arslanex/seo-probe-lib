"""carbon: Sustainable Web Design v4 arithmetic on fixed bytes, the three byte sources, the rule."""

from __future__ import annotations

import json

import pytest

from seoprobe.core.config import Config
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.features.performance import PagePerf, carbon
from seoprobe.features.performance.carbon import carbon_model, estimate, rating_for
from tests import fakes
from tests.features.performance.test_perf_operations import port, renderer  # noqa: F401 (fixtures)

URL = "https://e.com/"
MODEL = carbon_model()


def test_model_file_holds_version_sources_constants_and_bands() -> None:
    assert MODEL["model"] == "Sustainable Web Design Model" and MODEL["version"] == "4"
    assert MODEL["sources"] and MODEL["bytes_per_gb"] == 1_000_000_000
    e = MODEL["energy_kwh_per_gb"]
    assert sum(e["operational"].values()) + sum(e["embodied"].values()) == pytest.approx(0.300)
    assert [b["rating"] for b in MODEL["rating_bands_g_per_view"]] == ["A+", "A", "B", "C", "D", "E", "F"]
    assert MODEL["thresholds"]["flag_from_rating"] == "E"
    assert (
        MODEL["grid_intensity_g_per_kwh"]["global"] == 494 and "green_hosting" not in MODEL["grid_intensity_g_per_kwh"]
    )
    assert MODEL["green_hosting"]["verified_green_factor"] == 1.0
    assert MODEL["visits"] == {
        "first_visit_share": 1.0,
        "return_visit_share": 0.0,
        "data_reload_ratio": 0.0,
        "note": MODEL["visits"]["note"],
    }
    assert "https://sustainablewebdesign.org/estimating-digital-emissions/" in MODEL["sources"]
    assert "https://sustainablewebdesign.org/digital-carbon-ratings/" in MODEL["sources"]


def test_one_gigabyte_by_hand() -> None:
    # 0.300 kWh/GB x 494 g/kWh = 148.2 g
    est = estimate(1_000_000_000, model=MODEL)
    assert est["energy_kwh"] == pytest.approx(0.300)
    assert est["co2_grams"] == pytest.approx(148.2)
    assert est["segments_g"] == {
        "operational.data_centre": pytest.approx(27.17),
        "operational.network": pytest.approx(29.146),
        "operational.user_device": pytest.approx(39.52),
        "embodied.data_centre": pytest.approx(5.928),
        "embodied.network": pytest.approx(6.422),
        "embodied.user_device": pytest.approx(40.014),
    }
    assert est["rating"] == "F"


def test_green_hosting_scales_only_operational_data_centre_energy() -> None:
    # 148.2 - 0.055 x 494 = 121.03 g (verified green host: factor 1.0)
    est = estimate(1_000_000_000, model=MODEL, green_hosting_factor=1.0)
    assert est["co2_grams"] == pytest.approx(121.03)
    assert est["segments_g"]["operational.data_centre"] == 0.0
    assert est["segments_g"]["embodied.data_centre"] == pytest.approx(5.928)
    assert est["segments_g"]["operational.network"] == pytest.approx(29.146)
    # half green: 148.2 - 0.5 x 27.17 = 134.615 g
    assert estimate(1_000_000_000, model=MODEL, green_hosting_factor=0.5)["co2_grams"] == pytest.approx(134.615)


def test_visit_shares_and_reload_ratio() -> None:
    # 1 GB, 75 % first + 25 % return x 0.02 reload: 148.2 x (0.75 + 0.005) = 111.891 g
    est = estimate(1_000_000_000, model=MODEL, first_visit_share=0.75, return_visit_share=0.25, data_reload_ratio=0.02)
    assert est["co2_grams"] == pytest.approx(111.891)
    assert est["visits"] == {"first_visit_share": 0.75, "return_visit_share": 0.25, "data_reload_ratio": 0.02}
    assert estimate(1, model=MODEL)["visits"] == {
        "first_visit_share": 1.0,
        "return_visit_share": 0.0,
        "data_reload_ratio": 0.0,
    }


@pytest.mark.parametrize(
    "nbytes,grams,rating",
    [
        (0, 0.0, "A+"),
        (500_000, 0.0741, "A"),  # 0.0005 GB x 0.3 x 494
        (1_000_000, 0.1482, "C"),
        (2_000_000, 0.2964, "E"),
        (2_500_000, 0.3705, "F"),
    ],
)
def test_fixed_bytes_to_grams_and_rating(nbytes: int, grams: float, rating: str) -> None:
    est = estimate(nbytes, model=MODEL)
    assert est["co2_grams"] == pytest.approx(grams) and est["rating"] == rating


@pytest.mark.parametrize(
    "grams,rating",
    [(0.040, "A+"), (0.0401, "A"), (0.079, "A"), (0.145, "B"), (0.209, "C"), (0.278, "D"), (0.359, "E"), (0.36, "F")],
)
def test_rating_band_edges_are_inclusive(grams: float, rating: str) -> None:
    assert rating_for(grams, MODEL) == rating


async def test_a_measured_pageperf_is_used_as_is_and_flagged() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await carbon(PagePerf(url=URL, total_bytes=2_000_000), session=s)
        again = await carbon(PagePerf(url=URL, total_bytes=2_000_000), session=s)
    assert res.ok and res.status == "ok" and res.data is not None
    d = res.data
    assert (d.bytes, d.bytes_source, d.co2_grams, d.rating) == (2_000_000, "lab", 0.2964, "E")
    assert d.energy_kwh == pytest.approx(0.0006) and d.model_version == "4" and d.flag_from_rating == "E"
    assert d.constants["grid_intensity_g_per_kwh"]["global"] == 494 and "rating_bands_g_per_view" in d.constants
    [f] = res.findings
    assert (
        f.id == "page-carbon-high" and "0.296 g CO2e" in (f.evidence or "") and "flagged from E" in (f.evidence or "")
    )
    assert res.meta.requests == []  # nothing fetched
    assert again.data == res.data  # deterministic
    json.loads(res.to_json())


async def test_threshold_is_a_parameter_and_is_validated() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await carbon(PagePerf(url=URL, total_bytes=2_000_000), flag_from_rating="F", session=s)
        bad = await carbon(PagePerf(url=URL, total_bytes=1), flag_from_rating="Z", session=s)
    assert res.ok and res.findings == []
    assert not bad.ok and bad.error is not None and bad.error.code == "invalid-input"
    assert bad.error.details == {"reason": "unknown-rating", "limit": "A+, A, B, C, D, E, F"}


async def test_green_hosting_and_visits_through_the_operation() -> None:
    gb = PagePerf(url=URL, total_bytes=1_000_000_000)
    async with fakes.session(config=Config(render=False)) as s:
        green = await carbon(gb, green_hosting=True, session=s)
        half = await carbon(gb, green_hosting=True, green_hosting_factor=0.5, session=s)
        visits = await carbon(gb, first_visit_share=0.75, return_visit_share=0.25, data_reload_ratio=0.02, session=s)
    assert green.data is not None and green.data.green_hosting and green.data.green_hosting_factor == 1.0
    assert green.data.co2_grams == pytest.approx(121.03)
    assert half.data is not None and half.data.green_hosting_factor == 0.5
    assert half.data.co2_grams == pytest.approx(134.615)
    assert visits.data is not None and visits.data.co2_grams == pytest.approx(111.891)
    assert (visits.data.first_visit_share, visits.data.return_visit_share, visits.data.data_reload_ratio) == (
        0.75,
        0.25,
        0.02,
    )


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"green_hosting_factor": 1.5}, "green-hosting-factor-out-of-range"),
        ({"green_hosting_factor": -0.1}, "green-hosting-factor-out-of-range"),
        ({"first_visit_share": 1.5}, "first-visit-share-out-of-range"),
        ({"return_visit_share": -1.0}, "return-visit-share-out-of-range"),
        ({"data_reload_ratio": 2.0}, "data-reload-ratio-out-of-range"),
        ({"first_visit_share": 0.5}, "visit-shares-not-summing-to-one"),
        ({"first_visit_share": 0.6, "return_visit_share": 0.6}, "visit-shares-not-summing-to-one"),
    ],
)
async def test_shares_are_validated(kwargs: dict[str, float], field: str) -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await carbon(PagePerf(url=URL, total_bytes=1), session=s, **kwargs)  # type: ignore[arg-type]
    assert not res.ok and res.error is not None and res.error.code == "invalid-input"
    assert res.error.details["reason"] == field


async def test_without_a_browser_only_the_html_is_counted_and_the_result_is_partial() -> None:
    body = "<html><body>" + "x" * 5000 + "</body></html>"
    async with fakes.session({URL: fakes.html_page(body)}, config=Config(render=False)) as s:
        res = await carbon(URL, session=s)
    assert res.ok and res.status == "partial" and res.data is not None
    assert res.data.bytes == len(body) and res.data.bytes_source == "html"
    assert [(n.check_id, n.reason_code) for n in res.not_checked] == [
        ("page-carbon-high", "sub-resources-not-measured")
    ]
    assert res.findings == []


async def test_a_page_is_not_fetched_again_and_a_failed_pageperf_falls_back() -> None:
    page = Page(url=URL, final_url=URL, status=200, html="<p>hi</p>", html_bytes=1234)
    async with fakes.session(config=Config(render=False)) as s:
        res = await carbon(page, session=s)
        assert s.http.requested == []  # type: ignore[attr-defined]
    assert res.data is not None and res.data.bytes == 1234 and res.data.bytes_source == "html"
    async with fakes.session({URL: fakes.html_page("<p>x</p>")}, config=Config(render=False)) as s:
        res = await carbon(PagePerf(url=URL, error="render-failed: timeout"), session=s)
    assert res.data is not None and res.data.bytes_source == "html" and res.status == "partial"


async def test_unreachable_page_without_browser_is_an_error() -> None:
    async with fakes.session(errors={URL: "unreachable"}, config=Config(render=False)) as s:
        res = await carbon(URL, session=s)
    assert not res.ok


async def test_a_url_is_lab_measured_when_the_browser_is_available(renderer, port: int) -> None:  # noqa: F811
    url = f"http://app.localhost:{port}/"
    async with fakes.session(browser=renderer) as s:
        res = await carbon(url, session=s)
    assert res.ok and res.status == "ok" and res.data is not None
    assert res.data.bytes_source == "lab" and res.data.bytes > 0 and res.data.rating == "A+"


def test_the_new_rule_is_unscored_with_texts_in_both_languages() -> None:
    d = catalog.get("page-carbon-high")
    assert d.scored is False and d.category == "performance" and catalog.rule_feature(d.id) == "performance"
    for prefix in ("finding", "rec"):
        for lang in ("en", "tr"):
            assert catalog.t(lang, f"{prefix}.page-carbon-high") != f"{prefix}.page-carbon-high"
    assert catalog.t("tr", "finding.page-carbon-high") != catalog.t("en", "finding.page-carbon-high")
