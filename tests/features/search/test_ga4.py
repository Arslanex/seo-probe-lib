"""``seoprobe.ga4`` and the combined Search Console + Analytics consent."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import seoprobe
from seoprobe.core.errors import ErrorCode
from seoprobe.features.search import ga4, gsc
from seoprobe.features.search.ga4 import ADMIN_URL, DATA_URL
from seoprobe.features.search.models import AuthRequest, Ga4LandingPages, Ga4Summary
from seoprobe.features.search.oauth import (
    SCOPE_GA4_READ,
    SCOPE_GSC_READ,
    TOKEN_URL,
    USERINFO_URL,
    HttpResult,
    MemoryTokenStore,
    OAuthError,
)
from tests.features.search.support import (
    ACCESS,
    REFRESH,
    FakeGoogle,
    connected_store,
    gsc_session,
    make_oauth,
    use_google,
)

REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
BOTH = f"{SCOPE_GSC_READ} {SCOPE_GA4_READ}"


def _store(scope: str = BOTH) -> MemoryTokenStore:
    store = connected_store()
    store._d["default"]["scope"] = scope  # seeded directly: a sync test fixture
    return store


def _report(*rows: tuple[str, list[str]], row_count: int | None = None, totals: list[str] | None = None) -> HttpResult:
    body: dict[str, Any] = {
        "rows": [{"dimensionValues": [{"value": p}], "metricValues": [{"value": v} for v in vals]} for p, vals in rows],
        "rowCount": row_count if row_count is not None else len(rows),
    }
    if totals is not None:
        body["totals"] = [{"metricValues": [{"value": v} for v in totals]}]
    return HttpResult(200, body)


def _google(**over: Any) -> FakeGoogle:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u.startswith(f"{ADMIN_URL}/accountSummaries"):
            if "pageToken=p2" in u:
                return HttpResult(
                    200,
                    {
                        "accountSummaries": [
                            {
                                "account": "accounts/2",
                                "displayName": "B",
                                "propertySummaries": [{"property": "properties/7", "displayName": "Shop"}],
                            }
                        ]
                    },
                )
            return over.get(
                "summaries",
                HttpResult(
                    200,
                    {
                        "accountSummaries": [
                            {
                                "account": "accounts/1",
                                "displayName": "A",
                                "propertySummaries": [
                                    {"property": "properties/42", "displayName": "Blog"},
                                    {"x": 1},
                                ],
                            }
                        ],
                        "nextPageToken": "p2",
                    },
                ),
            )
        if u.endswith("/dataStreams?pageSize=200"):
            uri = {"properties/42": "https://blog.other.org", "properties/7": "https://www.example.com/"}
            prop = u[len(ADMIN_URL) + 1 :].split("/dataStreams")[0]
            return HttpResult(
                200, {"dataStreams": [{"type": "WEB_DATA_STREAM", "webStreamData": {"defaultUri": uri[prop]}}]}
            )
        if u.endswith(":runReport"):
            return over.get(
                "report",
                _report(
                    ("/", ["100", "60", "0.6", "3", "250.5"]),
                    ("/blog", ["40", "20", "0.5", "1", "0"]),
                    row_count=5,
                    totals=["150", "90", "0.6", "5", "300"],
                ),
            )
        return HttpResult(404)

    return FakeGoogle(h)


# ------------------------------------------------------------------ consent


def test_one_consent_can_cover_both_services() -> None:
    o = make_oauth()
    default = o.authorization_request(REDIRECT)
    assert default.scopes == [SCOPE_GSC_READ]
    q = parse_qs(urlparse(default.url).query)
    assert q["scope"] == [f"{SCOPE_GSC_READ} openid email"] and q["include_granted_scopes"] == ["true"]
    both = o.authorization_request(REDIRECT, services=("gsc", "ga4", "gsc"))
    assert both.scopes == [SCOPE_GSC_READ, SCOPE_GA4_READ]
    assert parse_qs(urlparse(both.url).query)["scope"] == [f"{BOTH} openid email"]
    assert o.authorization_request(REDIRECT, services=("ga4",)).scopes == [SCOPE_GA4_READ]


def _grant(scope: str) -> FakeGoogle:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == TOKEN_URL:
            return HttpResult(200, {"access_token": ACCESS, "refresh_token": REFRESH, "scope": scope})
        assert u == USERINFO_URL
        return HttpResult(200, {"email": "me@example.com"})

    return FakeGoogle(h)


async def test_every_requested_scope_must_be_granted() -> None:
    o = make_oauth(_grant(f"openid {SCOPE_GSC_READ}"))
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(o.authorization_request(REDIRECT, services=("gsc", "ga4")), "c")
    assert e.value.code == "scope-not-granted"
    o = make_oauth(_grant(f"openid {SCOPE_GA4_READ}"))
    await o.exchange_code(o.authorization_request(REDIRECT, services=("ga4",)), "c")
    assert await o.has_scope("default", SCOPE_GA4_READ) and not await o.has_scope("default", SCOPE_GSC_READ)


async def test_scopes_cannot_be_swapped_and_old_requests_still_work() -> None:
    o = make_oauth(_grant(BOTH))
    req = o.authorization_request(REDIRECT)
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req.model_copy(update={"scopes": [SCOPE_GA4_READ]}), "c")
    assert e.value.reason == "scope-mismatch"
    old = o.authorization_request(REDIRECT).model_copy(update={"scopes": []})  # stored by 0.1
    await o.exchange_code(old, "c")
    assert (await o.status())["connected"]


async def test_auth_url_services_and_ga4_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _grant(f"openid email {BOTH}"))
    store = MemoryTokenStore()
    async with gsc_session(store=store) as s:
        req = (await gsc.auth_url(REDIRECT, services=["gsc", "ga4"], session=s)).unwrap()
        assert req.scopes == [SCOPE_GSC_READ, SCOPE_GA4_READ]
        assert (await gsc.exchange_code(AuthRequest.model_validate_json(req.model_dump_json()), "c", session=s)).ok
        assert (await seoprobe.check_access("ga4", session=s)).code == "ok"
        assert (await seoprobe.check_access("search_console", session=s)).code == "ok"


# ------------------------------------------------------------------ operations


async def test_properties_follow_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=_store()) as s:
        res = await ga4.properties(session=s)
    items = res.unwrap().items
    assert [(p.property, p.display_name, p.account, p.account_name) for p in items] == [
        ("properties/42", "Blog", "accounts/1", "A"),
        ("properties/7", "Shop", "accounts/2", "B"),
    ]
    assert len(google.calls) == 2


async def test_landing_pages_request_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=_store()) as s:
        res = await ga4.landing_pages("7", days=7, session=s)
        all_channels = await ga4.landing_pages("properties/7", channel=None, limit=10, session=s)
    d = res.unwrap()
    assert isinstance(d, Ga4LandingPages) and d.property == "properties/7" and d.truncated
    assert [p.page for p in d.items] == ["/", "/blog"]
    first = d.items[0]
    assert (first.sessions, first.engaged_sessions, first.engagement_rate, first.key_events, first.revenue) == (
        100,
        60,
        0.6,
        3.0,
        250.5,
    )
    assert d.totals == {"sessions": 150.0, "engaged_sessions": 90.0, "key_events": 5.0, "revenue": 300.0}
    calls = [c for c in google.calls if c[1] == f"{DATA_URL}/properties/7:runReport"]
    body = calls[0][2]["json_body"]
    end = date.today() - timedelta(days=1)
    assert body["dateRanges"] == [{"startDate": (end - timedelta(days=6)).isoformat(), "endDate": end.isoformat()}]
    assert body["dimensions"] == [{"name": "landingPage"}] and body["metricAggregations"] == ["TOTAL"]
    assert [m["name"] for m in body["metrics"]] == list(ga4.METRICS)
    assert body["dimensionFilter"]["filter"]["stringFilter"]["value"] == "Organic Search"
    other = calls[1][2]["json_body"]
    assert "dimensionFilter" not in other and other["limit"] == 10
    assert all_channels.unwrap().channel is None
    assert json.loads(res.to_json())["operation"] == "ga4.landing_pages"


async def test_landing_pages_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google(report=HttpResult(403)))
    async with gsc_session(store=_store()) as s:
        bad = await ga4.landing_pages("UA-12", session=s)
        denied = await ga4.landing_pages("7", session=s)
    assert bad.error is not None and bad.error.code is ErrorCode.INVALID_INPUT
    assert bad.error.details == {"service": "ga4", "reason": "invalid-property"}
    assert denied.error is not None and denied.error.code is ErrorCode.NO_PERMISSION
    async with gsc_session(store=_store(SCOPE_GSC_READ)) as s:
        res = await ga4.landing_pages("7", session=s)
    assert res.error is not None and res.error.code is ErrorCode.NO_PERMISSION
    assert res.error.details["reason"] == "scope-not-granted"
    async with gsc_session(store=MemoryTokenStore()) as s:
        res = await ga4.properties(session=s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED
    async with gsc_session(store=_store(), with_client=False) as s:
        res = await ga4.properties(session=s)
    assert res.error is not None and res.error.code is ErrorCode.MISSING_CREDENTIAL


async def test_summary_resolves_by_web_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=_store()) as s:
        res = await ga4.summary("https://example.com/pricing", session=s)
        miss = await ga4.summary("https://example.net/", session=s)
    d = res.unwrap()
    assert isinstance(d, Ga4Summary) and d.status == "ok"
    assert (d.property, d.display_name, d.stream_uri) == ("properties/7", "Shop", "https://www.example.com/")
    assert d.account == "me@example.com" and d.totals["revenue"] == 300.0 and len(d.landing_pages) == 2
    assert miss.unwrap().status == "property-not-found"  # fail-closed: never another site's property


async def test_summary_states_never_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=_store(), with_client=False) as s:
        assert (await ga4.summary("https://example.com/", session=s)).unwrap().status == "not-configured"
    async with gsc_session() as s:
        assert (await ga4.summary("https://example.com/", session=s)).unwrap().status == "not-connected"
    async with gsc_session(store=_store(SCOPE_GSC_READ)) as s:
        d = (await ga4.summary("https://example.com/", session=s)).unwrap()
    assert d.status == "no-permission" and d.error == "scope-not-granted"
    assert google.calls == []
    use_google(monkeypatch, _google(summaries=HttpResult(500)))
    async with gsc_session(store=_store()) as s:
        d = (await ga4.summary("https://example.com/", session=s)).unwrap()
    assert d.status == "error" and d.error == "http-500"


# What Google answers when the Analytics API is not enabled in the Cloud project (shortened).
SERVICE_DISABLED = {
    "error": {
        "code": 403,
        "message": "Google Analytics Admin API has not been used in project 1234 before or it is disabled.",
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "SERVICE_DISABLED",
                "metadata": {"consumer": "projects/1234", "activationUrl": "https://console.developers.google.com/x"},
            }
        ],
    }
}


async def test_a_disabled_api_is_told_apart_from_a_missing_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(
        monkeypatch, _google(summaries=HttpResult(403, SERVICE_DISABLED), report=HttpResult(403, SERVICE_DISABLED))
    )
    async with gsc_session(store=_store()) as s:
        props = await ga4.properties(session=s)
        pages = await ga4.landing_pages("7", session=s)
        summary = (await ga4.summary("https://example.com/", session=s)).unwrap()
    for res in (props, pages):
        assert res.error is not None and res.error.code is ErrorCode.NO_PERMISSION
        assert res.error.details == {"service": "ga4", "reason": "api-not-enabled"}
    assert summary.status == "error" and summary.error == "api-not-enabled"
    text = props.to_json() + pages.to_json() + summary.model_dump_json()
    assert "1234" not in text and "activationUrl" not in text and "has not been used" not in text
    use_google(monkeypatch, _google(summaries=HttpResult(403, {"error": {"status": "PERMISSION_DENIED"}})))
    async with gsc_session(store=_store()) as s:
        plain = await ga4.properties(session=s)
    assert plain.error is not None and plain.error.details == {"service": "ga4"}  # no recognised cause


def test_google_error_reason_reads_both_body_styles() -> None:
    from seoprobe.features.search.oauth import google_error_reason

    v3 = {"error": {"errors": [{"domain": "usageLimits", "reason": "accessNotConfigured"}], "code": 403}}
    scope = {"error": {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    assert google_error_reason(HttpResult(403, SERVICE_DISABLED)) == "api-not-enabled"
    assert google_error_reason(HttpResult(403, v3)) == "api-not-enabled"
    assert google_error_reason(HttpResult(403, scope)) == "scope-not-granted"
    for body in (None, [], {"error": "x"}, {"error": {"details": "x"}}, {"error": {"details": [{"reason": "OTHER"}]}}):
        assert google_error_reason(HttpResult(403, body)) is None


async def test_ga4_access_check() -> None:
    async with gsc_session(store=_store(SCOPE_GSC_READ)) as s:
        assert (await seoprobe.check_access("ga4", session=s)).code == "scope-not-granted"
    async with gsc_session(store=MemoryTokenStore()) as s:
        assert (await seoprobe.check_access("ga4", session=s)).code == "not-connected"
    async with gsc_session(with_client=False) as s:
        assert (await seoprobe.check_access("ga4", session=s)).code == "missing"


def test_property_name() -> None:
    assert ga4.property_name("123") == "properties/123" == ga4.property_name(" properties/123 ")
    assert ga4.property_name("UA-1") is None and ga4.property_name("properties/") is None


async def test_preflight_reports_the_oauth_requirements() -> None:
    async with gsc_session(with_client=False) as s:
        for name in ("ga4.summary", "gsc.opportunities"):
            pre = await seoprobe.preflight(name, session=s)
            assert not pre.ready and {"credential:google_app", "config:token_store"} <= set(pre.missing)
    async with gsc_session(store=MemoryTokenStore()) as s:
        assert (await seoprobe.preflight("ga4.landing_pages", session=s)).ready
