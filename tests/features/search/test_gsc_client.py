from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from seoprobe.features.search.gsc import INSPECT_URL, SITES_URL, GscClient
from seoprobe.features.search.oauth import TOKEN_URL, HttpResult
from seoprobe.features.search.summary import GscError, resolve_property
from tests.features.search.support import ACCESS, FakeGoogle, make_oauth


def P(url: str, perm: str = "siteOwner") -> dict[str, str]:
    return {"site_url": url, "permission": perm}


# ------------------------------------------------------------------ resolve_property


def test_domain_property_beats_url_prefix() -> None:
    props = [P("https://www.example.com/"), P("sc-domain:example.com")]
    assert resolve_property("https://www.example.com/page", props).property == "sc-domain:example.com"


def test_www_apex_equivalence() -> None:
    props = [P("https://example.com/")]
    assert resolve_property("www.example.com", props).property == "https://example.com/"
    props = [P("https://www.example.com/")]
    assert resolve_property("https://example.com", props).property == "https://www.example.com/"
    assert resolve_property("sc-domain:x", []).property is None


def test_https_preferred_over_http() -> None:
    props = [P("http://example.com/"), P("https://example.com/")]
    assert resolve_property("example.com", props).property == "https://example.com/"
    assert resolve_property("example.com", [P("http://example.com/")]).property == "http://example.com/"


def test_domain_property_covers_subdomains_not_lookalikes() -> None:
    props = [P("sc-domain:example.com")]
    assert resolve_property("blog.example.com", props).property == "sc-domain:example.com"
    assert resolve_property("notexample.com", props).property is None
    assert resolve_property("example.com.evil.io", props).property is None


def test_exact_domain_beats_parent_domain() -> None:
    props = [P("sc-domain:example.com"), P("sc-domain:blog.example.com")]
    assert resolve_property("blog.example.com", props).property == "sc-domain:blog.example.com"


def test_unverified_does_not_count() -> None:
    r = resolve_property("example.com", [P("sc-domain:example.com", "siteUnverifiedUser")])
    assert r.property is None and r.reason == "no-permission"
    r = resolve_property(
        "example.com",
        [
            P("sc-domain:example.com", "siteUnverifiedUser"),
            P("https://example.com/", "siteFullUser"),
        ],
    )
    assert r.property == "https://example.com/" and r.permission == "siteFullUser"


def test_no_match_never_falls_back() -> None:
    props = [P("sc-domain:other.com"), P("https://another.org/")]
    r = resolve_property("example.com", props)
    assert r.property is None and r.reason == "property-not-found"
    assert resolve_property("", props).property is None
    assert resolve_property("example.com", []).reason == "property-not-found"


def test_directory_property_is_not_the_site() -> None:
    assert resolve_property("example.com", [P("https://example.com/blog/")]).property is None


# ------------------------------------------------------------------ client


def _client(handler: Any) -> tuple[GscClient, FakeGoogle]:
    http = FakeGoogle(handler)
    oauth = make_oauth(http, connected=True)
    return GscClient(oauth, http=http), http


async def test_list_properties() -> None:
    c, http = _client(
        lambda *_: HttpResult(
            200,
            {
                "siteEntry": [
                    {"siteUrl": "sc-domain:a.com", "permissionLevel": "siteOwner"},
                    {"x": 1},
                ]
            },
        ),
    )
    assert await c.list_properties() == [{"site_url": "sc-domain:a.com", "permission": "siteOwner"}]
    m, url, kw = http.calls[0]
    assert (m, url) == ("GET", SITES_URL) and kw["headers"]["Authorization"] == f"Bearer {ACCESS}"


async def test_search_analytics_request() -> None:
    c, http = _client(lambda *_: HttpResult(200, {"rows": [{"keys": ["q"], "clicks": 3}]}))
    rows = await c.search_analytics("sc-domain:a.com", days=28, dimensions=["query"], row_limit=20)
    assert rows[0]["clicks"] == 3
    _m, url, kw = http.calls[0]
    assert url == f"{SITES_URL}/{quote('sc-domain:a.com', safe='')}/searchAnalytics/query"
    assert "%3A" in url and "sc-domain:" not in url
    body = kw["json_body"]
    assert body["dimensions"] == ["query"] and body["rowLimit"] == 20
    from datetime import date, timedelta

    assert body["endDate"] == (date.today() - timedelta(days=3)).isoformat()
    assert (date.fromisoformat(body["endDate"]) - date.fromisoformat(body["startDate"])).days == 27


async def test_inspect_and_sitemaps() -> None:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == INSPECT_URL:
            assert kw["json_body"] == {
                "inspectionUrl": "https://a.com/x",
                "siteUrl": "sc-domain:a.com",
            }
            return HttpResult(200, {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}})
        return HttpResult(200, {"sitemap": [{"path": "https://a.com/s.xml"}]})

    c, _ = _client(h)
    assert (await c.inspect_url("sc-domain:a.com", "https://a.com/x"))["indexStatusResult"]["verdict"] == "PASS"
    assert (await c.list_sitemaps("sc-domain:a.com"))[0]["path"] == "https://a.com/s.xml"


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "no-permission"), (404, "property-not-found"), (429, "rate-limited"), (500, "http-500")],
)
async def test_error_map(status: int, code: str) -> None:
    c, http = _client(lambda *_: HttpResult(status, {"error": {"message": "secret?"}}))
    with pytest.raises(GscError) as e:
        await c.list_sitemaps("sc-domain:a.com")
    assert e.value.code == code
    assert len(http.calls) == 1  # 429 and others are not retried


async def test_network_error_code() -> None:
    c, _ = _client(lambda *_: HttpResult(None, error="timeout"))
    with pytest.raises(GscError) as e:
        await c.list_properties()
    assert e.value.code == "timeout"


async def test_401_refreshes_once_then_succeeds() -> None:
    seen: list[str] = []

    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == TOKEN_URL:
            return HttpResult(200, {"access_token": "fresh", "expires_in": 3600})
        seen.append(kw["headers"]["Authorization"])
        return HttpResult(401) if len(seen) == 1 else HttpResult(200, {"siteEntry": []})

    c, _ = _client(h)
    assert await c.list_properties() == []
    assert seen == [f"Bearer {ACCESS}", "Bearer fresh"]


async def test_401_twice_gives_not_connected() -> None:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == TOKEN_URL:
            return HttpResult(200, {"access_token": "fresh", "expires_in": 3600})
        return HttpResult(401)

    c, http = _client(h)
    with pytest.raises(GscError) as e:
        await c.list_properties()
    assert e.value.code == "not-connected"
    assert sum(1 for x in http.calls if x[1] == TOKEN_URL) == 1


async def test_not_connected() -> None:
    http = FakeGoogle(lambda *_: HttpResult(200, {}))
    c = GscClient(make_oauth(http), http=http)
    with pytest.raises(GscError) as e:
        await c.list_properties()
    assert e.value.code == "not-connected" and http.calls == []
