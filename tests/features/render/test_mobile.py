"""mobile operation against local pages with real Chromium."""

from __future__ import annotations

import json

import pytest

from seoprobe.core.config import Config
from seoprobe.core.errors import ErrorCode
from seoprobe.core.net.browser import Renderer
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.features.render import MobileReport, mobile
from seoprobe.features.render.mobile import MOBILE_RULES, PARITY_RULE, defaults
from tests import fakes
from tests.features.render.conftest import Serve

VIEWPORT = "<meta name='viewport' content='width=device-width, initial-scale=1'>"
STYLE = (
    "<style>body{font-size:16px;margin:0} a,button{display:block;min-height:48px;min-width:48px;padding:12px}</style>"
)
TEXT = "Plenty of readable text for every visitor on every device. " * 10
PAGES = {
    "/good": f"<html><head>{VIEWPORT}{STYLE}</head><body><h1>Home</h1><p>{TEXT}</p>"
    "<a href='/a'>First link</a><a href='/b'>Second link</a><button>Buy</button></body></html>",
    "/bad": "<html><head><style>body{font-size:10px} .o{position:absolute;top:300px;width:60px;height:60px}</style>"
    "</head><body><p>"
    + "tiny text " * 40
    + "</p>"
    + "".join(f"<a href='/l{i}'>l{i}</a> " for i in range(8))
    + "<button class='o' style='left:10px'>A</button><button class='o' style='left:40px'>B</button>"
    "<div style='width:2400px;height:10px'></div></body></html>",
    "/parity": f"<html><head>{VIEWPORT}{STYLE}<style>@media (max-width:600px){{.desk{{display:none}}}}</style>"
    f"</head><body><p>Short intro.</p><div class='desk'><p>{TEXT}</p>"
    + "".join(f"<a href='/d{i}'>Desktop link {i}</a>" for i in range(10))
    + "</div><a href='/m'>Mobile link</a></body></html>",
}


@pytest.fixture
async def port(serve: Serve) -> int:
    return await serve(PAGES)  # type: ignore[arg-type]


def _t(port: int, path: str) -> Page:
    url = f"http://app.localhost:{port}{path}"
    return Page(url=url, final_url=url)


async def test_clean_mobile_page_has_no_findings(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await mobile(_t(port, "/good"), session=s)
    assert res.ok and res.data is not None, res.error
    d = res.data
    assert d.tap_targets == 3 and d.small_tap_targets == 0 and d.overlapping_tap_targets == 0
    assert d.text_nodes > 0 and d.small_text_share == 0.0
    assert not d.horizontal_overflow and d.viewport_width == 390
    assert d.viewport_meta_present and d.viewport_device_width
    assert d.mobile is not None and d.desktop is not None
    assert d.text_ratio is not None and d.text_ratio >= 0.95 and d.link_ratio == 1.0
    assert d.thresholds == {k: float(v) for k, v in defaults().items()}
    assert res.findings == [] and res.not_checked == []


async def test_bad_mobile_page_is_flagged_with_thresholds(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await mobile(_t(port, "/bad"), session=s)
    assert res.ok and res.data is not None
    d = res.data
    assert d.small_tap_targets >= 8 and d.small_tap_share >= 0.25 and d.small_tap_examples
    assert all(e.width < 48 or e.height < 48 for e in d.small_tap_examples)
    assert d.small_tap_examples[0].selector and d.small_tap_examples[0].text
    assert d.overlapping_tap_targets >= 1 and d.overlap_examples[0].first != d.overlap_examples[0].second
    assert d.small_text_share >= 0.4
    assert d.horizontal_overflow and d.scroll_width > d.viewport_width
    assert not d.viewport_meta_present and not d.viewport_device_width
    by_id = {f.id: f for f in res.findings}
    assert set(by_id) == set(MOBILE_RULES)
    assert "threshold 25%" in (by_id["mobile-small-tap-targets"].evidence or "")
    assert "48x48" in (by_id["mobile-small-tap-targets"].evidence or "")
    assert "threshold 40%" in (by_id["mobile-small-text"].evidence or "")
    assert by_id["mobile-viewport-not-device-width"].evidence == "no viewport meta tag"


async def test_content_hidden_on_mobile_breaks_parity(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await mobile(_t(port, "/parity"), session=s)
    assert res.data is not None
    d = res.data
    assert d.desktop is not None and d.mobile is not None
    assert d.desktop.internal_links == 11 and d.mobile.internal_links == 1
    assert d.link_ratio == round(1 / 11, 3) and d.text_ratio is not None and d.text_ratio < 0.8
    parity = [f for f in res.findings if f.id == PARITY_RULE]
    assert len(parity) == 1 and "threshold 80%" in (parity[0].evidence or "")
    assert "internal links 9% of desktop (1 vs 11)" in (parity[0].evidence or "")


async def test_threshold_parameters_override_the_defaults(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await mobile(_t(port, "/good"), min_tap_px=200, small_tap_share=0.5, session=s)
    assert res.data is not None and res.data.thresholds["min_tap_px"] == 200.0
    assert res.data.small_tap_targets == 3
    f = [f for f in res.findings if f.id == "mobile-small-tap-targets"]
    assert f and "200x200" in (f[0].evidence or "") and "threshold 50%" in (f[0].evidence or "")


async def test_result_is_json_serialisable(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await mobile(_t(port, "/bad"), session=s)
    out = json.loads(res.to_json())
    assert MobileReport.model_validate(out["data"]) == res.data
    assert {f["id"] for f in out["findings"]} == set(MOBILE_RULES)


async def test_same_input_same_output(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        a = await mobile(_t(port, "/bad"), session=s)
        b = await mobile(_t(port, "/bad"), session=s)
    assert a.data == b.data and [f.evidence for f in a.findings] == [f.evidence for f in b.findings]


async def test_production_policy_rejects_loopback(renderer_prod: Renderer) -> None:
    async with fakes.session(browser=renderer_prod) as s:
        res = await mobile(Page(url="http://127.0.0.1:9/", final_url="http://127.0.0.1:9/"), session=s)
    assert res.data is not None and res.data.mobile is None and not res.findings
    assert {n.check_id for n in res.not_checked} == {*MOBILE_RULES, PARITY_RULE}
    assert {n.reason_code for n in res.not_checked} == {"target-rejected"}


async def test_render_disabled_is_browser_unavailable() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await mobile(Page(url="https://x.test/", final_url="https://x.test/"), session=s)
    assert not res.ok and res.error is not None and res.error.code == ErrorCode.BROWSER_UNAVAILABLE


def test_rules_in_catalog_unscored_with_both_languages() -> None:
    for rid in (*MOBILE_RULES, PARITY_RULE):
        d = catalog.get(rid)
        assert catalog.rule_feature(rid) == "render" and d.scored is False
        for key in (d.message_key, d.recommendation_key):
            en, tr = catalog.message(key, "en"), catalog.message(key, "tr")
            assert en != key and tr != key and en != tr
