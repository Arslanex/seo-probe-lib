"""accessibility operation (axe-core) against local pages with real Chromium."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

import pytest

from seoprobe.core.config import Config
from seoprobe.core.errors import ErrorCode
from seoprobe.core.net.browser import Renderer
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.features.render import AccessibilityReport, accessibility
from seoprobe.features.render.accessibility import A11Y_RULES, axe_version, wcag_criteria
from tests import fakes
from tests.features.render.conftest import Serve

AXE_VERSION = "4.10.2"
AXE_SHA256 = "b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3"

GOOD = (
    "<!doctype html><html lang='en'><head><title>Fine page</title></head><body><main><h1>Hello</h1>"
    "<p>Readable text.</p><img src='data:,' alt='A logo'><a href='/x'>Read more</a></main></body></html>"
)
BAD = (
    "<!doctype html><html><head><title>Bad page</title></head><body><h1>Hi</h1>"
    "<img src='data:,'><button></button>"
    "<p style='color:#bbb;background:#fff'>Low contrast text</p></body></html>"
)
CONTRAST_ONLY = (
    "<!doctype html><html lang='en'><head><title>Contrast</title></head><body><main><h1>T</h1>"
    "<p style='color:#bbb;background:#fff'>Low contrast text</p></main></body></html>"
)
PAGES = {
    "/good": GOOD,
    "/bad": BAD,
    "/contrast": CONTRAST_ONLY,
    "/csp": (BAD, {"Content-Security-Policy": "default-src 'none'; script-src 'none'"}),
}


@pytest.fixture
async def port(serve: Serve) -> int:
    return await serve(PAGES)  # type: ignore[arg-type]


def _t(port: int, path: str) -> Page:
    url = f"http://app.localhost:{port}{path}"
    return Page(url=url, final_url=url)


async def test_clean_page_has_no_violations(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await accessibility(_t(port, "/good"), session=s)
    assert res.ok and res.data is not None, res.error
    d = res.data
    assert d.axe_version == AXE_VERSION and d.standard == "wcag21aa"
    assert d.tags == ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
    assert d.violations == [] and d.passes > 0 and d.error is None
    assert res.findings == [] and res.not_checked == []


async def test_violations_are_grouped_by_impact(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await accessibility(_t(port, "/bad"), session=s)
    assert res.data is not None
    d = res.data
    ids = {v.id: v for v in d.violations}
    assert {"image-alt", "button-name", "html-has-lang", "color-contrast"} <= set(ids)
    img = ids["image-alt"]
    assert img.impact == "critical" and "1.1.1" in img.wcag and "wcag111" in img.tags
    assert img.help_url.startswith("https://dequeuniversity.com/rules/axe/4.10/image-alt")
    assert img.nodes_count == 1 and img.nodes[0].selector == "img" and img.nodes[0].html.startswith("<img")
    assert ids["color-contrast"].impact == "serious" and "1.4.3" in ids["color-contrast"].wcag
    assert d.violations_by_impact["critical"] >= 2 and d.violations_by_impact["serious"] >= 2
    rank = ["critical", "serious", "moderate", "minor"]
    assert [rank.index(v.impact or "minor") for v in d.violations] == sorted(
        rank.index(v.impact or "minor") for v in d.violations
    )
    by_id = {f.id: f for f in res.findings}
    assert {"a11y-critical", "a11y-serious"} <= set(by_id)
    ev = by_id["a11y-critical"].evidence or ""
    assert "image-alt: Images must have alternative text (1 nodes)" in ev
    assert by_id["a11y-critical"].count == sum(v.nodes_count for v in d.violations if v.impact == "critical")


async def test_standard_selects_the_axe_tags(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        level_a = await accessibility(_t(port, "/contrast"), standard="wcag2a", session=s)
        level_aa = await accessibility(_t(port, "/contrast"), standard="wcag22aa", session=s)
    assert level_a.data is not None and level_aa.data is not None
    assert level_a.data.tags == ["wcag2a"] and level_a.data.violations == []
    assert "wcag22aa" in level_aa.data.tags
    assert [v.id for v in level_aa.data.violations] == ["color-contrast"]


async def test_csp_does_not_block_axe(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await accessibility(_t(port, "/csp"), session=s)
    assert res.data is not None and res.data.error is None
    assert "image-alt" in {v.id for v in res.data.violations}


async def test_incomplete_results_are_listed_separately(renderer: Renderer, serve: Serve) -> None:
    # text over a background image: axe cannot decide the contrast -> needs review
    html = (
        "<!doctype html><html lang='en'><head><title>t</title></head><body><main><h1>T</h1>"
        "<p style=\"background-image:url('data:image/gif;base64,R0lGODlhAQABAAAAACw=');color:#777\">"
        "Text over an image</p></main></body></html>"
    )
    port = await serve({"/i": html})
    async with fakes.session(browser=renderer) as s:
        res = await accessibility(_t(port, "/i"), session=s)
    assert res.data is not None
    assert "color-contrast" in {v.id for v in res.data.incomplete}
    assert "color-contrast" not in {v.id for v in res.data.violations}
    assert not res.findings


async def test_result_is_json_serialisable_and_deterministic(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        a = await accessibility(_t(port, "/bad"), session=s)
        b = await accessibility(_t(port, "/bad"), session=s)
    out = json.loads(a.to_json())
    assert AccessibilityReport.model_validate(out["data"]) == a.data
    assert a.data == b.data and [f.evidence for f in a.findings] == [f.evidence for f in b.findings]


async def test_production_policy_rejects_loopback(renderer_prod: Renderer) -> None:
    async with fakes.session(browser=renderer_prod) as s:
        res = await accessibility(Page(url="http://127.0.0.1:9/", final_url="http://127.0.0.1:9/"), session=s)
    assert res.data is not None and res.data.error == "target-rejected" and not res.findings
    assert {n.check_id for n in res.not_checked} == set(A11Y_RULES)


async def test_render_disabled_is_browser_unavailable() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await accessibility(Page(url="https://x.test/", final_url="https://x.test/"), session=s)
    assert not res.ok and res.error is not None and res.error.code == ErrorCode.BROWSER_UNAVAILABLE


async def test_unknown_standard_is_rejected() -> None:
    with pytest.raises(ValueError):
        await accessibility(Page(url="https://x.test/", final_url="https://x.test/"), standard="wcag3")  # type: ignore[arg-type]


def test_wcag_criteria() -> None:
    assert wcag_criteria(["cat.text", "wcag2a", "wcag111", "wcag1410", "wcag21aa", "EN-301-549"]) == [
        "1.1.1",
        "1.4.10",
    ]


def test_vendored_axe_is_pinned_with_its_license() -> None:
    axe = files("seoprobe.features.render").joinpath("axe")
    assert hashlib.sha256(axe.joinpath("axe.min.js").read_bytes()).hexdigest() == AXE_SHA256
    assert axe_version() == AXE_VERSION
    assert "Mozilla Public License, version 2.0" in axe.joinpath("LICENSE").read_text(encoding="utf-8")


def test_rules_in_catalog_unscored_with_both_languages() -> None:
    assert A11Y_RULES == ("a11y-critical", "a11y-serious", "a11y-moderate", "a11y-minor")
    for rid in A11Y_RULES:
        d = catalog.get(rid)
        assert catalog.rule_feature(rid) == "render" and d.scored is False
        for key in (d.message_key, d.recommendation_key):
            en, tr = catalog.message(key, "en"), catalog.message(key, "tr")
            assert en != key and tr != key and en != tr
