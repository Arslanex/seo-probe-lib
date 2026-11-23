"""
Screenshots operation against local pages. The URL policy is relaxed ONLY for ``*.localhost``
(test seam, see conftest); every other target goes through the real policy.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from seoprobe.core.errors import ErrorCode
from seoprobe.core.net.browser import Renderer
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.features.render.screenshots import MAX_BYTES, screenshots
from tests import fakes
from tests.features.render.conftest import Serve

PAGES = {
    "/good": "<html><head><meta name='viewport' content='width=device-width'></head><body>"
    "<h1>Welcome home</h1><p>Some intro text for visitors.</p>"
    "<a class='btn' href='/x'>Buy now</a></body></html>",
    "/deep-h1": "<html><head><meta name='viewport' content='width=device-width'></head><body>"
    "<div style='height:2000px'>spacer</div><h1>Far below</h1></body></html>",
    "/banner": "<html><head><meta name='viewport' content='width=device-width'></head><body>"
    "<h1>Page</h1><div id='cookie' style='position:fixed;left:0;right:0;bottom:0;top:30%;"
    "background:#222;color:#fff'>We use cookies <button>OK</button></div></body></html>",
    "/overflow": "<html><head><meta name='viewport' content='width=device-width'></head><body>"
    "<h1>Wide</h1><div style='width:2400px;height:10px;background:red'></div></body></html>",
}


@pytest.fixture
async def port(serve: Serve) -> int:
    return await serve(PAGES)


def _target(url: str) -> Page:
    return Page(url=url, final_url=url)


async def test_two_devices_saved_and_clean_page_has_no_findings(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await screenshots(_target(f"http://app.localhost:{port}/good"), session=s)
    d = res.data
    assert d is not None
    saved = {f.name: base64.b64decode(f.data_base64 or "") for f in d.files}
    assert set(saved) == {"desktop.jpg", "mobile.jpg"}
    assert all(b[:3] == b"\xff\xd8\xff" and len(b) <= MAX_BYTES for b in saved.values())
    assert all(f.bytes == len(saved[f.name]) for f in d.files)
    assert {(f.device, f.width, f.height) for f in d.files} == {
        ("desktop", 1366, 768),
        ("mobile", 780, 1688),
    }
    for dev in ("desktop", "mobile"):
        fold = d.above_fold[dev]
        assert fold.h1_visible and fold.cta_visible and fold.text_chars > 20
        assert fold.overlay_share == 0 and not fold.horizontal_overflow
    assert res.findings == [] and res.not_checked == []
    assert res.ok and json.loads(res.to_json())["data"]["files"][0]["data_base64"]


async def test_h1_below_the_fold_and_overlay_are_flagged(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await screenshots(_target(f"http://app.localhost:{port}/deep-h1"), session=s)
        assert [f.id for f in res.findings] == ["above-fold-no-h1"]
        res2 = await screenshots(_target(f"http://app.localhost:{port}/banner"), session=s)
    ids = [f.id for f in res2.findings]
    assert ids == ["intrusive-interstitial"]
    assert res2.data is not None and res2.data.above_fold["mobile"].overlay_share > 0.3


async def test_horizontal_overflow_reported_on_mobile(renderer: Renderer, port: int) -> None:
    async with fakes.session(browser=renderer) as s:
        res = await screenshots(_target(f"http://app.localhost:{port}/overflow"), session=s)
    assert res.data is not None and res.data.above_fold["mobile"].horizontal_overflow


class _BrokenBrowser:
    launch_error = "Error: executable doesn't exist"


async def test_no_renderer_is_not_checked() -> None:
    """
    The source returned a not_checked "render-unavailable"; here ``ctx.browser()`` fails the
    operation with browser-unavailable (no data, no findings).
    """
    async with fakes.session(browser=_BrokenBrowser()) as s:
        res = await screenshots(_target("https://x.test/"), session=s)
    assert res.data is None and not res.findings
    assert res.error is not None and res.error.code == ErrorCode.BROWSER_UNAVAILABLE


async def test_production_policy_rejects_loopback(renderer_prod: Renderer) -> None:
    async with fakes.session(browser=renderer_prod) as s:
        res = await screenshots(_target("http://127.0.0.1:9/"), session=s)
    assert res.data is not None and res.data.files == []
    assert res.not_checked[0].reason_code == "target-rejected"


def test_rules_registered_with_both_languages() -> None:
    src = (Path(__file__).parents[3] / "src/seoprobe/features/render/screenshots.py").read_text()
    ids = set(re.findall(r'make_finding\(\s*"([a-z0-9-]+)"', src))
    assert ids == {"above-fold-no-h1", "intrusive-interstitial"}
    for rid in ids:
        d = catalog.get(rid)
        assert d.category.value == "technical" and d.heuristic
        assert catalog.rule_feature(rid) == "render"
        for key in (d.message_key, d.recommendation_key, d.failure_check_key, d.leading_indicator_key):
            assert catalog.message(key, "en") and catalog.message(key, "tr")
            assert catalog.message(key, "en") != catalog.message(key, "tr")
