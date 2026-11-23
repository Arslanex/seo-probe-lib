"""
The render operation: SPA gate, render failure, raw-vs-rendered gap (real Chromium for the
end-to-end cases, a stub renderer for the branches).
"""

from __future__ import annotations

import json

from seoprobe.core.net.browser import Renderer, RenderResult
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.features.render import RenderReport, render
from tests import fakes
from tests.features.render.conftest import Serve

WORDS = " ".join(f"rendered{i}" for i in range(80))
SHELL = "<html><head><title>t</title></head><body><div id='root'></div></body></html>"
JS_PAGE = (
    "<html><head><title>t</title></head><body><div id='root'></div><script>"
    "setTimeout(()=>{document.getElementById('root').innerHTML="
    f"'<h1>Rendered Heading</h1><p>{WORDS}</p>'}},50)</script></body></html>"
)
FULL = "<html><body><h1>Hello</h1><p>" + "Real paragraph content for visitors. " * 20 + "</p></body></html>"


class StubRenderer:
    launch_error: str | None = None

    def __init__(self, res: RenderResult) -> None:
        self.res = res
        self.calls: list[str] = []

    async def render(self, url: str) -> RenderResult:
        self.calls.append(url)
        return self.res


def _page(url: str, html: str | None, final_url: str | None = None) -> Page:
    return Page(url=url, final_url=final_url or url, status=200, content_type="text/html", html=html)


# --- moved from the core browser tests (they used renderer.render(url, raw_html=...))


async def test_js_content_exists_only_after_render(renderer: Renderer, serve: Serve) -> None:
    url = f"http://app.localhost:{await serve({'/js': JS_PAGE})}/js"
    async with fakes.session(browser=renderer) as s:
        res = await render(_page(url, SHELL), session=s)
    assert res.ok and res.data is not None
    d = res.data
    assert d.rendered is True and d.error is None
    assert "Rendered Heading" in (d.html or "") and "Rendered Heading" not in SHELL
    assert d.is_spa is True
    assert d.js_gap is not None and d.js_gap["significant"] and d.js_gap["gap"] is True
    assert d.js_gap["diff"]["h1_only_rendered"] == ["Rendered Heading"]
    assert d.reasons == d.js_gap["reasons"] and d.reasons
    assert res.not_checked == []


async def test_non_spa_page_is_not_rendered_unless_forced(renderer: Renderer, serve: Serve) -> None:
    """Without the SPA signal no render and no gap; ``force`` renders and computes the gap."""
    url = f"http://app.localhost:{await serve({'/js': JS_PAGE})}/js"
    async with fakes.session(browser=renderer) as s:
        res = await render(_page(url, FULL), session=s)
        forced = await render(_page(url, FULL), force=True, session=s)
    assert res.ok and res.data is not None
    assert res.data.is_spa is False and res.data.rendered is False
    assert res.data.js_gap is None and res.data.html is None
    assert forced.ok and forced.data is not None
    assert forced.data.is_spa is False and forced.data.rendered is True and forced.data.html
    assert forced.data.js_gap is not None


# --- branches


async def test_non_html_page_is_not_rendered() -> None:
    stub = StubRenderer(RenderResult(html="<p>x</p>"))
    async with fakes.session(browser=stub) as s:
        res = await render(_page("https://x.test/", None), session=s)
    assert res.ok and res.data == RenderReport(url="https://x.test/", is_spa=False, rendered=False)
    assert stub.calls == []


async def test_fetches_a_url_target_and_renders_the_final_url() -> None:
    stub = StubRenderer(RenderResult(html=JS_PAGE.replace("<div id='root'></div>", f"<h1>H</h1><p>{WORDS}</p>")))
    async with fakes.session({"https://x.test/": fakes.html_page(SHELL)}, browser=stub) as s:
        res = await render("https://x.test/", session=s)
    assert stub.calls == ["https://x.test/"]
    assert res.ok and res.data is not None and res.data.rendered and res.data.is_spa
    assert res.data.js_gap is not None and res.data.js_gap["gap"] is True


async def test_render_failure_is_not_checked_with_the_error_code() -> None:
    stub = StubRenderer(RenderResult(error="target-rejected: blocked address"))
    async with fakes.session(browser=stub) as s:
        res = await render(_page("https://x.test/a", SHELL, "https://x.test/b"), session=s)
    assert stub.calls == ["https://x.test/b"]
    assert res.status == "partial" and res.data is not None
    assert res.data.rendered is False and res.data.is_spa is True and res.data.error == "target-rejected"
    assert [(n.check_id, n.reason_code, n.url) for n in res.not_checked] == [
        ("js-dependent-content", "render-failed", "https://x.test/a")
    ]


async def test_empty_render_is_a_failure() -> None:
    stub = StubRenderer(RenderResult(html=""))
    async with fakes.session(browser=stub) as s:
        res = await render(_page("https://x.test/", SHELL), session=s)
    assert res.data is not None and res.data.rendered is False
    assert [n.reason_code for n in res.not_checked] == ["render-failed"]


async def test_result_is_json_serialisable() -> None:
    stub = StubRenderer(RenderResult(html=f"<html><body><h1>H</h1><p>{WORDS}</p></body></html>"))
    async with fakes.session(browser=stub) as s:
        res = await render(_page("https://x.test/", SHELL), session=s)
    out = json.loads(res.to_json())
    assert out["data"]["rendered"] is True and out["data"]["js_gap"]["gap"] is True


def test_referenced_rule_ids_exist() -> None:
    assert "js-dependent-content" in catalog.rules()
