from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from typing import Any

from seoprobe.core.models import Category, Severity
from seoprobe.core.rules import catalog
from seoprobe.features.search import summary as gsc_mod
from seoprobe.features.search.gsc import INSPECT_URL, SITES_URL, GscClient
from seoprobe.features.search.models import GscData, GscInspection, GscSitemap
from seoprobe.features.search.oauth import HttpResult
from seoprobe.features.search.summary import collect_gsc, findings
from tests.features.search.support import FakeGoogle, make_oauth

SITE = "sc-domain:example.com"
MESSAGES = json.loads(files("seoprobe.features.search").joinpath("catalog.json").read_text())["messages"]
SITES = {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]}


def _handler(**over: Any) -> Any:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == SITES_URL:
            return over.get("sites", HttpResult(200, SITES))
        if u == INSPECT_URL:
            return (
                over["inspect"](kw["json_body"]["inspectionUrl"])
                if "inspect" in over
                else HttpResult(
                    200,
                    {
                        "inspectionResult": {
                            "indexStatusResult": {
                                "verdict": "PASS",
                                "coverageState": "Submitted and indexed",
                                "robotsTxtState": "ALLOWED",
                                "pageFetchState": "SUCCESSFUL",
                                "googleCanonical": kw["json_body"]["inspectionUrl"],
                                "userCanonical": kw["json_body"]["inspectionUrl"],
                            }
                        }
                    },
                )
            )
        if u.endswith("/sitemaps"):
            return over.get("sitemaps", HttpResult(200, {"sitemap": [{"path": "https://example.com/s.xml"}]}))
        if "searchAnalytics" in u:
            if "perf" in over:
                return over["perf"](kw["json_body"])  # type: ignore[no-any-return]
            rows = [{"keys": ["kelime"], "clicks": 4, "impressions": 40, "ctr": 0.1, "position": 5.5}]
            return HttpResult(200, {"rows": rows})
        return HttpResult(404)

    return h


def _client(handler: Any, **kw: Any) -> tuple[GscClient, FakeGoogle]:
    http = FakeGoogle(handler)
    return GscClient(make_oauth(http, connected=True, **kw), http=http), http


async def test_not_configured_and_not_connected() -> None:
    http = FakeGoogle(_handler())
    c = GscClient(make_oauth(http, with_client=False), http=http)
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "not-configured" and http.calls == []
    c = GscClient(make_oauth(http), http=http)
    assert (await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])).status == "not-connected"


async def test_ok_full() -> None:
    c, http = _client(_handler())
    urls = [f"https://example.com/p{i}" for i in range(30)]
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=urls, inspect_limit=5)
    assert d.status == "ok" and d.property == SITE and d.permission == "siteOwner"
    assert d.account == "me@example.com"
    assert d.totals["impressions"] == 40 and d.top_queries[0].key == "kelime"
    assert len(d.inspections) == 6  # entry + 5 samples, quota-aware
    assert d.inspections[0].indexed is True
    assert d.sitemaps[0].path == "https://example.com/s.xml"
    assert sum(1 for x in http.calls if x[1] == INSPECT_URL) == 6


async def test_property_not_found_never_calls_data() -> None:
    sites = HttpResult(200, {"siteEntry": [{"siteUrl": "sc-domain:other.com", "permissionLevel": "siteOwner"}]})
    c, http = _client(_handler(sites=sites))
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "property-not-found" and d.property is None and d.top_queries == []
    assert [x[1] for x in http.calls] == [SITES_URL]


async def test_unverified_is_no_permission() -> None:
    sites = HttpResult(200, {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteUnverifiedUser"}]})
    c, _ = _client(_handler(sites=sites))
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "no-permission"


async def test_properties_call_failure_is_error() -> None:
    c, _ = _client(_handler(sites=HttpResult(500)))
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "error" and d.error == "http-500"


async def test_partial_failure_keeps_other_sections() -> None:
    def inspect(u: str) -> HttpResult:
        return (
            HttpResult(429)
            if u.endswith("p1")
            else HttpResult(
                200,
                {
                    "inspectionResult": {
                        "indexStatusResult": {
                            "verdict": "FAIL",
                            "coverageState": "Crawled - currently not indexed",
                        }
                    }
                },
            )
        )

    c, _ = _client(_handler(inspect=inspect, sitemaps=HttpResult(403)))
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=["https://example.com/p1"])
    assert d.status == "ok" and d.totals["clicks"] == 4  # performance survived
    assert d.sitemaps == [] and d.error == "sitemaps: no-permission"
    by = {i.url: i for i in d.inspections}
    assert by["https://example.com/p1"].error == "rate-limited"
    assert by["https://example.com/"].indexed is False  # "currently not indexed"
    f = findings(d)
    assert not any(x.url == "https://example.com/p1" for x in f.findings)  # errored: no claim
    assert "gsc-sitemap-not-submitted" not in {x.id for x in f.findings}  # unknown, not absent


async def test_total_timeout(monkeypatch: Any) -> None:
    monkeypatch.setattr(gsc_mod, "TOTAL_TIMEOUT_S", 0.05)

    class Slow(FakeGoogle):
        async def request(self, method: str, url: str, **kw: Any) -> HttpResult:
            await asyncio.sleep(1)
            return HttpResult(200, SITES)

    http = Slow(_handler())
    c = GscClient(make_oauth(http, connected=True), http=http)
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "error" and d.error == "timeout"


async def test_unexpected_exception_is_contained() -> None:
    def boom(*_: Any) -> HttpResult:
        raise ValueError("secret detail")

    c, _ = _client(boom)
    d = await collect_gsc(c, entry_url="https://example.com/", sample_urls=[])
    assert d.status == "error" and "secret" not in (d.error or "")


# ------------------------------------------------------------------ findings


def _gsc(**kw: Any) -> GscData:
    base: dict[str, Any] = {
        "status": "ok",
        "property": SITE,
        "totals": {"clicks": 1, "impressions": 10, "ctr": 0.1, "position": 3},
        "sitemaps": [GscSitemap(path="https://example.com/s.xml")],
    }
    return GscData(**{**base, **kw})


def test_findings_only_when_ok() -> None:
    d = GscData(status="not-connected")
    assert findings(d).findings == []
    d = _gsc(status="property-not-found")
    assert findings(d).findings == []


def test_rule_not_indexed_canonical_robots_fetch() -> None:
    ins = [
        GscInspection(
            url="https://example.com/a",
            indexed=False,
            coverage_state="Discovered - currently not indexed",
        ),
        GscInspection(
            url="https://example.com/b",
            indexed=True,
            user_canonical="https://example.com/b/",
            google_canonical="https://example.com/b",
        ),  # trailing slash only: same page
        GscInspection(
            url="https://example.com/c",
            indexed=True,
            user_canonical="https://example.com/c",
            google_canonical="https://example.com/other",
        ),
        GscInspection(url="https://example.com/d", robots_txt_state="DISALLOWED", page_fetch_state="SOFT_404"),
        GscInspection(
            url="https://example.com/e",
            indexed=True,
            page_fetch_state="SUCCESSFUL",
            robots_txt_state="ALLOWED",
        ),
    ]
    out = findings(_gsc(inspections=ins))
    got = {(f.id, f.url) for f in out.findings}
    assert ("gsc-page-not-indexed", "https://example.com/a") in got
    assert ("gsc-canonical-mismatch", "https://example.com/c") in got
    assert ("gsc-canonical-mismatch", "https://example.com/b") not in got
    assert ("gsc-robots-blocked", "https://example.com/d") in got
    assert ("gsc-fetch-failed", "https://example.com/d") in got
    assert not any(u == "https://example.com/e" for _i, u in got)
    a = next(f for f in out.findings if f.id == "gsc-page-not-indexed")
    assert a.evidence == "Discovered - currently not indexed" and a.severity is Severity.HIGH


def test_site_rules() -> None:
    out = findings(
        _gsc(
            sitemaps=[GscSitemap(path="https://example.com/s.xml", errors=2)],
            totals={"impressions": 0},
        )
    )
    ids = {f.id for f in out.findings}
    assert {"gsc-sitemap-errors", "gsc-no-impressions"} <= ids
    assert "gsc-sitemap-not-submitted" not in ids
    ni = next(f for f in out.findings if f.id == "gsc-no-impressions")
    assert ni.heuristic and "new" in ni.message
    assert "gsc-sitemap-not-submitted" in {f.id for f in findings(_gsc(sitemaps=[])).findings}


def test_registry_definitions() -> None:
    ids = [
        "gsc-page-not-indexed",
        "gsc-canonical-mismatch",
        "gsc-robots-blocked",
        "gsc-fetch-failed",
        "gsc-sitemap-errors",
        "gsc-sitemap-not-submitted",
        "gsc-no-impressions",
    ]
    for i in ids:
        d = catalog.get(i)
        assert d.requires == ("gsc",) and d.category is Category.TECHNICAL
        for key in (
            d.message_key,
            d.recommendation_key,
            d.failure_check_key,
            d.leading_indicator_key,
        ):
            for lang in ("en", "tr"):
                assert MESSAGES[lang][key], (key, lang)
    assert catalog.get("gsc-no-impressions").heuristic
    assert catalog.get("gsc-robots-blocked").severity is Severity.HIGH
