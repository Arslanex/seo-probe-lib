"""``seoprobe.bing`` over a fake API session (no network)."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import seoprobe
from seoprobe.core.config import Config
from seoprobe.core.errors import ErrorCode
from seoprobe.core.net import api
from seoprobe.features.search import bing
from seoprobe.features.search.bing import API_URL
from tests.features.search.support import FakeApi, api_session

KEY = "bing-key-SECRET-5150"
CFG = Config(bing_api_key=KEY)
SITE = "https://example.com/"

DATA: dict[str, Any] = {
    "GetUserSites": {"d": [{"Url": "https://b.com/", "IsVerified": False}, {"Url": SITE, "IsVerified": True}]},
    "GetQueryStats": {
        "d": [
            {"Query": "late", "Date": "/Date(1316156400000-0700)/", "Impressions": 5, "Clicks": 1},
            {
                "Query": "seo",
                "Date": "/Date(1315551600000-0700)/",
                "Impressions": 100,
                "Clicks": 15,
                "AvgImpressionPosition": 17,
                "AvgClickPosition": 18,
            },
        ]
    },
    "GetCrawlStats": {
        "d": [
            {
                "AllOtherCodes": 0,
                "BlockedByRobotsTxt": 2,
                "Code2xx": 9998,
                "Code301": 0,
                "Code302": 0,
                "Code4xx": 1,
                "Code5xx": 1,
                "ContainsMalware": 5,
                "CrawlErrors": 0,
                "CrawledPages": 12,
                "Date": "/Date(1316156400000-0700)/",
                "InIndex": 1000,
                "InLinks": 2048,
            }
        ]
    },
    "GetCrawlIssues": {
        "d": [
            {"HttpCode": 301, "Issues": 1, "Url": "https://example.com/old", "InLinks": 50},
            {"HttpCode": 404, "Issues": 4 | 16, "Url": "https://example.com/gone", "InLinks": 10},
            {"HttpCode": 200, "Issues": 32, "Url": "https://example.com/bad", "InLinks": 3},
        ]
    },
    "GetUrlInfo": {
        "d": {
            "AnchorCount": 50,
            "DiscoveryDate": "/Date(1315349995266-0700)/",
            "DocumentSize": 2048,
            "HttpStatus": 200,
            "IsPage": True,
            "LastCrawledDate": "/Date(1316213995266-0700)/",
            "TotalChildUrlCount": 100,
            "Url": "https://example.com/a",
        }
    },
}


def _api(over: dict[str, tuple[int, Any]] | None = None) -> FakeApi:
    def h(m: str, u: str, kw: dict[str, Any]) -> tuple[int, Any]:
        method = u[len(API_URL) :]
        if over and method in over:
            return over[method]
        return 200, DATA[method]

    return FakeApi(h)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_retry_delay", lambda attempt, retry_after: 0.0)


def test_bing_date_and_issue_flags() -> None:
    assert bing.bing_date("/Date(1316156400000-0700)/") == "2011-09-16T07:00:00Z"
    assert bing.bing_date("2011-09-16T00:00:00-07:00") == "2011-09-16T00:00:00-07:00"
    assert bing.bing_date(None) is None and bing.bing_date("") is None
    assert bing.decode_issues(4 | 16 | 256) == ["Code4xx", "BlockedByRobotsTxt", "TimeOutErrors"]
    assert bing.decode_issues(0) == []


async def test_sites_and_request_shape() -> None:
    fake = _api()
    async with api_session(fake, config=CFG) as s:
        res = await bing.sites(session=s)
    assert [(x.url, x.is_verified) for x in res.unwrap().items] == [("https://b.com/", False), (SITE, True)]
    method, url, kw = fake.calls[0]
    assert (method, url) == ("GET", f"{API_URL}GetUserSites") and kw["params"] == {"apikey": KEY}


async def test_query_and_crawl_stats() -> None:
    fake = _api()
    async with api_session(fake, config=CFG) as s:
        q = (await bing.query_stats(SITE, session=s)).unwrap()
        c = (await bing.crawl_stats(SITE, session=s)).unwrap()
    assert [x.query for x in q.items] == ["seo", "late"]  # oldest first
    seo = q.items[0]
    assert seo.date == "2011-09-09T07:00:00Z" and seo.impressions == 100 and seo.clicks == 15
    assert seo.avg_impression_position == 17.0 and q.items[1].avg_click_position is None
    row = c.items[0]
    assert (row.code_2xx, row.code_4xx, row.blocked_by_robots_txt, row.in_index, row.crawled_pages) == (
        9998,
        1,
        2,
        1000,
        12,
    )
    assert all(kw["params"]["siteUrl"] == SITE for _m, _u, kw in fake.calls)


async def test_crawl_issues_and_finding() -> None:
    async with api_session(_api(), config=CFG) as s:
        res = await bing.crawl_issues(SITE, session=s)
    items = res.unwrap().items
    assert [i.url for i in items] == ["https://example.com/old", "https://example.com/gone", "https://example.com/bad"]
    assert items[1].issues == ["Code4xx", "BlockedByRobotsTxt"] and items[1].issue_flags == 20
    [f] = res.findings
    assert f.id == "bing-crawl-issues" and f.count == 2 and f.url == "https://example.com/gone"
    assert "Code4xx" in (f.evidence or "") and "threshold" in (f.evidence or "")
    async with api_session(_api({"GetCrawlIssues": (200, {"d": [DATA["GetCrawlIssues"]["d"][0]]})}), config=CFG) as s:
        assert (await bing.crawl_issues(SITE, session=s)).findings == []  # redirects only


async def test_url_info() -> None:
    fake = _api()
    async with api_session(fake, config=CFG) as s:
        d = (await bing.url_info(SITE, "https://example.com/a", session=s)).unwrap()
    assert d.http_status == 200 and d.is_page and d.document_size == 2048 and d.anchor_count == 50
    assert d.discovery_date is not None and d.discovery_date.startswith("2011-09-06")
    assert fake.calls[0][2]["params"]["url"] == "https://example.com/a"


async def test_missing_key_path() -> None:
    fake = _api()
    async with api_session(fake) as s:
        res = await bing.sites(session=s)
        pre = await seoprobe.preflight("bing.crawl_issues", session=s)
        check = await seoprobe.check_access("bing", session=s)
    assert res.error is not None and res.error.code is ErrorCode.MISSING_CREDENTIAL
    assert not pre.ready and "credential:bing_api_key" in pre.missing
    assert (check.ok, check.code) == (False, "missing") and fake.calls == []


@pytest.mark.parametrize(
    ("answer", "code", "reason"),
    [
        ((400, {"ErrorCode": 3, "Message": "ERROR!!! InvalidApiKey"}), ErrorCode.NO_PERMISSION, "invalid-api-key"),
        ((400, {"ErrorCode": 14, "Message": "NotAuthorized"}), ErrorCode.NO_PERMISSION, None),
        ((400, {"ErrorCode": 11}), ErrorCode.PROPERTY_NOT_FOUND, None),
        ((400, {"ErrorCode": 7}), ErrorCode.INVALID_INPUT, "invalid-url"),
        ((429, None), ErrorCode.API_QUOTA_EXCEEDED, None),
        ((500, None), ErrorCode.API_ERROR, None),
    ],
)
async def test_error_mapping(answer: tuple[int, Any], code: ErrorCode, reason: str | None) -> None:
    async with api_session(_api({"GetQueryStats": answer}), config=CFG) as s:
        res = await bing.query_stats(SITE, session=s)
    assert res.error is not None and res.error.code is code
    assert res.error.details["service"] == "bing" and res.error.details.get("reason") == reason


async def test_access_check() -> None:
    async with api_session(_api(), config=CFG) as s:
        assert (await seoprobe.check_access("bing", session=s)).code == "ok"
    async with api_session(_api({"GetUserSites": (400, {"ErrorCode": 3})}), config=CFG) as s:
        assert (await seoprobe.check_access("bing", session=s)).code == "rejected"
    async with api_session(_api({"GetUserSites": (429, None)}), config=CFG) as s:
        assert (await seoprobe.check_access("bing", session=s)).code == "rate-limited"


async def test_key_never_in_results_requests_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    texts = []
    async with api_session(_api({"GetUrlInfo": (400, {"ErrorCode": 3, "Message": KEY})}), config=CFG) as s:
        for res in (
            await bing.sites(session=s),
            await bing.query_stats(SITE, session=s),
            await bing.crawl_issues(SITE, session=s),
            await bing.url_info(SITE, "https://example.com/a", session=s),
        ):
            texts.append(res.to_json())
            assert res.meta.requests, res.operation
            for r in res.meta.requests:
                assert KEY not in r.url and parse_qs(urlparse(r.url).query)["apikey"] == ["***"]
    blob = "\n".join(texts) + caplog.text
    assert KEY not in blob
    assert json.loads(texts[0])["operation"] == "bing.sites"
