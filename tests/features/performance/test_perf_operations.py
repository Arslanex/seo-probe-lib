"""
The four operations end to end: lab (real Chromium on a local server), PageSpeed and CrUX
(fake API session), web_vitals merging, the Google access check and the key-in-header rule.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import web

import seoprobe.features
from seoprobe.core.config import Config
from seoprobe.core.models import NotChecked
from seoprobe.core.net import api
from seoprobe.core.net.browser import Renderer
from seoprobe.core.net.safety import Target, check_url
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog
from seoprobe.core.session import Session
from seoprobe.features.performance import field_data, lab_metrics, pagespeed, web_vitals
from seoprobe.features.performance import vitals as rules_perf
from seoprobe.features.performance.google import _test_code, check_google_key
from seoprobe.features.performance.vitals import merge_field
from tests import fakes

KEY = "AIzaSy-TEST-SECRET-0123456789"
PSI_DATA = {
    "lighthouseResult": {
        "audits": {
            "largest-contentful-paint": {"numericValue": 5200},
            "cumulative-layout-shift": {"numericValue": 0.02},
        }
    }
}
CRUX_DATA = {
    "record": {
        "metrics": {
            "largest_contentful_paint": {"histogram": [{"density": 0.5}], "percentiles": {"p75": 3100}},
            "interaction_to_next_paint": {"histogram": [{"density": 0.9}], "percentiles": {"p75": 650}},
            "cumulative_layout_shift": {"histogram": [{"density": 0.9}], "percentiles": {"p75": "0.01"}},
        },
        "collectionPeriod": {
            "firstDate": {"year": 2026, "month": 10, "day": 1},
            "lastDate": {"year": 2026, "month": 10, "day": 28},
        },
    }
}
PAGE = "<html><head><title>t</title></head><body><h1>Hello</h1><p>Some text for the page.</p></body></html>"


# ----------------------------------------------------------------------------- fakes


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw


class _Resp:
    def __init__(self, status: int, data: Any) -> None:
        raw = json.dumps(data).encode() if data is not None else b""
        self.status, self.headers, self.content_length = status, {}, len(raw)
        self.content = _Content(raw)
        self._data = data

    async def json(self, content_type: Any = None) -> Any:
        return self._data

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class FakeApi:
    """aiohttp-shaped session: ``get`` answers PageSpeed, ``request`` answers CrUX."""

    def __init__(self, psi: tuple[int, Any] = (200, PSI_DATA), crux: list[tuple[int, Any]] | None = None) -> None:
        self.psi = psi
        self.crux = list(crux if crux is not None else [(200, CRUX_DATA)])
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kw: Any) -> _Resp:
        self.calls.append({"method": "GET", "url": url, **kw})
        return _Resp(*self.psi)

    def request(self, method: str, url: str, **kw: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kw})
        return _Resp(*self.crux.pop(0))


class ApiHttp(fakes.FakeHttp):
    def __init__(self, fake_api: FakeApi, config: Config) -> None:
        super().__init__(config=config)
        self.fake_api = fake_api

    def api_session(self) -> Any:
        return self.fake_api


def api_session(
    fake_api: FakeApi, *, key: str | None = KEY, render: bool = False, browser: Renderer | None = None
) -> Session:
    cfg = Config(google_api_key=key, render=render)
    return Session(cfg, http=ApiHttp(fake_api, cfg), browser=browser)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_retry_delay", lambda attempt, retry_after: 0.0)


def _relaxed(url: str) -> Target:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.endswith(".localhost"):
        return Target(url, p.scheme, host, p.port or 80, False)
    return check_url(url)


async def _resolve(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def port() -> AsyncIterator[int]:
    app = web.Application()

    async def page(r: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    app.router.add_get("/", page)
    app.router.add_get("/b", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    await runner.cleanup()


@pytest.fixture
async def renderer() -> AsyncIterator[Renderer]:
    r = Renderer(url_check=_relaxed, resolve=_resolve)
    await r.start()
    if r._browser is None:
        pytest.skip(f"chromium unavailable: {r.launch_error}")
    yield r
    await r.close()


class _BrokenRenderer(Renderer):
    """A browser that cannot start (launch_error set), as on a machine without Chromium."""

    async def start(self) -> None:
        self._launch_error = "Error: chromium is not installed"


# ----------------------------------------------------------------------------- lab_metrics


async def test_lab_metrics_measures_every_target_kind(renderer: Renderer, port: int) -> None:
    url = f"http://app.localhost:{port}/"
    page = Page(url=f"http://app.localhost:{port}/old", final_url=f"http://app.localhost:{port}/b", status=200)
    async with fakes.session(browser=renderer) as s:
        res = await lab_metrics([url, page, (url, "mobile")], session=s)
    assert res.ok and res.data is not None
    got = [(p.url, p.device, p.error) for p in res.data.items]
    assert got == [(url, "desktop", None), (page.final_url, "desktop", None), (url, "mobile", None)]
    assert res.data.items[2].mobile is not None
    assert {n.check_id for n in res.not_checked} == {"cwv-inp-poor", "cwv-inp-needs-improvement"}
    assert res.status == "partial"  # INP is field-only
    json.loads(res.to_json())


async def test_lab_metrics_keeps_measuring_when_the_browser_cannot_start() -> None:
    url = "http://app.localhost:1/"
    async with fakes.session(browser=_BrokenRenderer(url_check=_relaxed, resolve=_resolve)) as s:
        res = await lab_metrics([url, (url, "mobile")], session=s)
    assert res.ok and res.data is not None
    assert all((p.error or "").startswith("render-failed") for p in res.data.items)
    assert res.findings == [] and {n.reason_code for n in res.not_checked} == {"render-failed"}
    assert ("mobile-horizontal-scroll", url) in {(n.check_id, n.url) for n in res.not_checked}


async def test_lab_metrics_needs_rendering_enabled() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await lab_metrics(["https://e.com/"], session=s)
    assert not res.ok and res.error is not None and "config:render" in res.error.details["missing"]


# ----------------------------------------------------------------------------- pagespeed / field_data


async def test_pagespeed_reports_facts_and_findings() -> None:
    fake = FakeApi()
    async with api_session(fake) as s:
        res = await pagespeed("https://e.com/", session=s)
    assert res.ok and res.data is not None and res.data.measured
    assert res.data.facts["cwv_lcp"] == {"value": 5200.0, "source": "lab", "rating": "poor"}
    assert [f.id for f in res.findings] == ["cwv-lcp-poor"]
    assert [n.check_id for n in res.not_checked] == ["cwv-inp-poor"]
    assert fake.calls[0]["params"] == {"url": "https://e.com/", "strategy": "mobile", "category": "performance"}
    assert fake.calls[0]["headers"] == {"X-Goog-Api-Key": KEY}
    json.loads(res.to_json())


async def test_pagespeed_unusable_is_not_measured_not_an_error() -> None:
    async with api_session(FakeApi(psi=(429, {}))) as s:
        res = await pagespeed("https://e.com/", session=s)
    assert res.ok and res.status == "partial" and res.data is not None and not res.data.measured
    assert res.findings == [] and res.data.facts == {}


async def test_google_operations_need_the_key() -> None:
    async with api_session(FakeApi(), key=None) as s:
        for res in (await pagespeed("https://e.com/", session=s), await field_data("https://e.com/", session=s)):
            assert not res.ok and res.error is not None and res.error.code == "missing-credential"


async def test_field_data_findings_and_origin() -> None:
    fake = FakeApi(crux=[(404, {}), (200, CRUX_DATA)])
    async with api_session(fake) as s:
        res = await field_data("https://e.com/a?x=1", session=s)
    assert res.ok and res.data is not None and res.data.scope == "origin"
    assert [c["json"] for c in fake.calls] == [{"url": "https://e.com/a?x=1"}, {"origin": "https://e.com"}]
    assert {f.id for f in res.findings} == {"cwv-lcp-needs-improvement", "cwv-inp-poor"}
    assert res.status == "ok"
    fake = FakeApi(crux=[(404, {}), (404, {})])
    async with api_session(fake) as s:
        res = await field_data("https://e.com/a", origin="https://www.e.com", session=s)
    assert fake.calls[1]["json"] == {"origin": "https://www.e.com"}
    assert res.data is not None and res.data.error == "no-data" and res.status == "partial"


async def test_the_api_key_never_appears_in_requests_or_results() -> None:
    fake = FakeApi(crux=[(200, CRUX_DATA)])
    async with api_session(fake) as s:
        results = [await pagespeed("https://e.com/", session=s), await field_data("https://e.com/", session=s)]
    for res in results:
        assert res.meta.requests, res.operation
        assert all(KEY not in r.url for r in res.meta.requests)
        assert KEY not in res.to_json()
    assert all(c["headers"] == {"X-Goog-Api-Key": KEY} for c in fake.calls)
    assert all(KEY not in c["url"] and KEY not in json.dumps(c.get("params") or {}) for c in fake.calls)
    async with api_session(FakeApi(crux=[(200, CRUX_DATA)])) as s:
        res = await web_vitals("https://e.com/", session=s)
    assert KEY not in res.to_json()


# ----------------------------------------------------------------------------- web_vitals


async def test_web_vitals_without_key_or_browser_is_partial_with_reasons() -> None:
    async with fakes.session(config=Config(render=False)) as s:
        res = await web_vitals("https://e.com/", session=s)
    assert res.ok and res.status == "partial" and res.data is not None
    assert res.data.lab == [] and res.data.pagespeed is None and res.data.field is None
    reasons = {(n.check_id, n.reason_code) for n in res.not_checked}
    assert ("cwv-inp-poor", "missing-credential:google_api_key") in reasons
    assert ("page-weight-heavy", "render-disabled") in reasons
    assert {n.check_id for n in res.not_checked if n.reason_code == "render-disabled"} == set(rules_perf.LAB_RULE_IDS)
    pairs = [(n.check_id, n.reason_code, n.url) for n in res.not_checked]
    assert len(pairs) == len(set(pairs))  # no entry twice
    inp = [n.reason_code for n in res.not_checked if n.check_id == "cwv-inp-poor"]
    assert "field-data-unavailable" not in inp  # the missing key already says the field data is absent


async def test_web_vitals_merges_lab_pagespeed_and_field(renderer: Renderer, port: int) -> None:
    url = f"http://app.localhost:{port}/"
    async with api_session(FakeApi(), render=True, browser=renderer) as s:
        res = await web_vitals(url, session=s)
    assert res.ok and res.data is not None
    assert [(p.device, p.error) for p in res.data.lab] == [("desktop", None), ("mobile", None)]
    assert res.data.pagespeed is not None and res.data.pagespeed.measured
    assert res.data.field is not None and {m.metric for m in res.data.field.metrics} == {"lcp", "inp", "cls"}
    ids = [f.id for f in res.findings]
    assert "cwv-lcp-poor" in ids and "cwv-inp-poor" in ids  # PageSpeed lab LCP, field INP
    # field data answered INP: the lab and PageSpeed "field-data-unavailable" entries are gone
    assert not [n for n in res.not_checked if n.check_id.startswith("cwv-")]
    assert res.status == "ok"


async def test_web_vitals_keeps_unanswered_field_checks() -> None:
    no_inp = {"record": {"metrics": {"largest_contentful_paint": {"percentiles": {"p75": 1000}}}}}
    async with api_session(FakeApi(crux=[(200, no_inp)])) as s:
        res = await web_vitals("https://e.com/", session=s)
    nc = {(n.check_id, n.reason_code) for n in res.not_checked}
    assert ("cwv-inp-poor", "field-data-unavailable") in nc
    assert ("cwv-lcp-poor", "render-disabled") in nc


def test_merge_field_only_drops_answered_field_data_unavailable() -> None:
    from seoprobe.features.performance import FieldData, FieldMetric

    nc = [
        NotChecked(check_id="cwv-inp-poor", reason_code="field-data-unavailable"),
        NotChecked(check_id="cwv-inp-needs-improvement", reason_code="field-data-unavailable"),
        NotChecked(check_id="cwv-lcp-poor", reason_code="field-data-unavailable"),
        NotChecked(check_id="cwv-inp-poor", reason_code="render-failed", url="u"),
    ]
    fd = FieldData(metrics=[FieldMetric(metric="inp", p75=100)])
    assert [(n.check_id, n.reason_code) for n in merge_field(nc, fd)] == [
        ("cwv-lcp-poor", "field-data-unavailable"),
        ("cwv-inp-poor", "render-failed"),
    ]
    assert merge_field(nc, None) == nc


# ----------------------------------------------------------------------------- access check


@pytest.mark.parametrize(
    "answers,expected",
    [
        ([(200, CRUX_DATA)], (True, "ok")),
        ([(404, {}), (404, {})], (True, "ok")),
        ([(403, {})], (False, "rejected.gkey")),
        ([(400, {})], (False, "rejected.gkey")),
        ([(429, {})] * 4, (False, "rate-limited")),
        ([(500, {})] * 4, (False, "other")),
    ],
)
async def test_google_access_check(answers: list[tuple[int, Any]], expected: tuple[bool, str]) -> None:
    fake = FakeApi(crux=answers)
    async with api_session(fake) as s:
        assert await check_google_key(s) == expected
        if expected[1] == "ok":
            assert fake.calls[0]["json"] == {"url": "https://web.dev/"}
    async with api_session(FakeApi(crux=answers)) as s:
        got = await seoprobe.features.check_access("google", session=s)
    assert (got.ok, got.code) == expected


async def test_google_access_check_without_a_key() -> None:
    async with api_session(FakeApi(), key=None) as s:
        assert await check_google_key(s) == (False, "missing")


def test_test_code_mapping() -> None:
    assert _test_code("gkey", None) == "ok" and _test_code("gkey", "no-data") == "ok"
    assert _test_code("gkey", "http-401") == "rejected.gkey" and _test_code("gkey", "unauthorized") == "rejected.gkey"
    assert _test_code("gkey", "timeout") == "unreachable" and _test_code("gkey", "unreachable") == "unreachable"
    assert _test_code("gkey", "no-api-key") == "missing" and _test_code("gkey", "http-500") == "other"


# ----------------------------------------------------------------------------- catalog


def test_every_rule_id_the_code_emits_is_in_the_catalog() -> None:
    from seoprobe.features.performance.vitals import _IMAGE_ISSUES

    src = Path(rules_perf.__file__).parent
    code = "\n".join((src / f).read_text() for f in ("lab.py", "vitals.py", "google.py", "carbon.py"))
    literal = set(re.findall(r'(?:make_finding|ctx\.finding)\(\s*"([a-z0-9-]+)"', code))
    literal |= set(re.findall(r'out\.append\("([a-z0-9-]+)"\)', code))
    cwv = {f"cwv-{m}-{r}" for m in ("lcp", "inp", "cls") for r in ("poor", "needs-improvement")}
    ids = literal | cwv | set(_IMAGE_ISSUES) | set(rules_perf.LAB_RULE_IDS) | set(rules_perf.MOBILE_RULE_IDS)
    rules = catalog.rules()
    assert ids and not [i for i in ids if i not in rules]
    own = {rid for rid in rules if catalog.rule_feature(rid) == "performance"}
    assert own == ids  # and every rule of the catalog is produced by this feature
