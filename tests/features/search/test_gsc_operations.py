"""The public ``seoprobe.gsc`` operations (plumbing, error mapping, contract) over a fake Google."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import seoprobe
from seoprobe.core.errors import ErrorCode
from seoprobe.core.result import Status
from seoprobe.features import search
from seoprobe.features.search import bing, ga4, gsc, indexnow
from seoprobe.features.search.gsc import INSPECT_URL, SITES_URL
from seoprobe.features.search.models import AuthRequest, GscData
from seoprobe.features.search.oauth import (
    REVOKE_URL,
    SCOPE_GSC_READ,
    TOKEN_URL,
    USERINFO_URL,
    HttpResult,
    MemoryTokenStore,
)
from tests.features.search.support import (
    ACCESS,
    REFRESH,
    SECRET,
    FakeGoogle,
    connected_store,
    gsc_session,
    use_google,
)

BASE = "http://127.0.0.1:8000"
REDIRECT = f"{BASE}/oauth/google/callback"
SITE = "sc-domain:example.com"
SITES = {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]}


def _google(handler: Any = None, **over: Any) -> FakeGoogle:
    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == TOKEN_URL:
            assert kw["form"]["redirect_uri"] == REDIRECT
            return HttpResult(
                200,
                {"access_token": ACCESS, "refresh_token": REFRESH, "scope": SCOPE_GSC_READ, "expires_in": 3600},
            )
        if u == USERINFO_URL:
            return HttpResult(200, {"email": "me@example.com"})
        if u == SITES_URL:
            return over.get("sites", HttpResult(200, SITES))
        if u == INSPECT_URL:
            return HttpResult(
                200,
                {
                    "inspectionResult": {
                        "indexStatusResult": {
                            "verdict": "FAIL",
                            "coverageState": "Crawled - currently not indexed",
                            "robotsTxtState": "ALLOWED",
                            "pageFetchState": "SUCCESSFUL",
                        }
                    }
                },
            )
        if u.endswith("/sitemaps"):
            return over.get("sitemaps", HttpResult(200, {"sitemap": [{"path": "https://example.com/s.xml"}]}))
        if "searchAnalytics" in u:
            if "analytics" in over:
                return over["analytics"](kw["json_body"])  # type: ignore[no-any-return]
            keys = ["seo", "https://example.com/"][: len(kw["json_body"].get("dimensions") or [1])]
            row = {"keys": keys, "clicks": 9, "impressions": 90, "ctr": 0.1, "position": 4.0}
            return HttpResult(200, {"rows": [row]})
        return HttpResult(404)

    return FakeGoogle(handler or h)


# ------------------------------------------------------------------ OAuth flow


async def test_auth_url_redirects_to_google() -> None:
    async with gsc_session() as s:
        res = await gsc.auth_url(REDIRECT, session=s)
    assert res.ok and isinstance(res.data, AuthRequest)
    loc = urlparse(res.data.url)
    q = parse_qs(loc.query)
    assert loc.netloc == "accounts.google.com"
    assert q["redirect_uri"] == [REDIRECT]
    assert q["scope"] == [f"{SCOPE_GSC_READ} openid email"]
    assert q["state"] == [res.data.state]
    assert SECRET not in res.data.url and SECRET not in res.to_json()


async def test_auth_url_not_configured() -> None:
    async with gsc_session(with_client=False) as s:
        res = await gsc.auth_url(REDIRECT, session=s)
    assert res.status is Status.ERROR and res.error is not None
    assert res.error.code is ErrorCode.MISSING_CREDENTIAL
    assert res.error.details == {"missing": "credential:google_app", "service": "gsc"}


async def test_callback_success_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    store = MemoryTokenStore()
    async with gsc_session(store=store) as s:
        req = (await gsc.auth_url(REDIRECT, account="u1", session=s)).unwrap()
        stored = req.model_dump_json()  # the caller keeps it until the callback
        state = parse_qs(urlparse(req.url).query)["state"][0]
        res = await gsc.exchange_code(AuthRequest.model_validate_json(stored), "abc", state=state, session=s)
        assert res.ok and res.data is not None
        assert res.data.model_dump() == {
            "account": "u1",
            "configured": True,
            "connected": True,
            "email": "me@example.com",
        }
        st = (await gsc.connection(account="u1", session=s)).unwrap()
        assert (st.configured, st.connected, st.email) == (True, True, "me@example.com")
        assert not (await gsc.connection(session=s)).unwrap().connected
        # the same stored request a second time: rejected (single use)
        again = await gsc.exchange_code(AuthRequest.model_validate_json(stored), "abc", session=s)
    assert again.error is not None and again.error.code is ErrorCode.INVALID_INPUT
    assert again.error.details == {"service": "gsc", "reason": "state-reused"}
    assert await store.get("u1") is not None


async def test_callback_bad_state_and_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=MemoryTokenStore()) as s:
        req = (await gsc.auth_url(REDIRECT, session=s)).unwrap()
        bad = await gsc.exchange_code(req, "y", state="x", session=s)
        old = (await gsc.auth_url(REDIRECT, session=s)).unwrap()
        old = old.model_copy(update={"created_at": old.created_at - timedelta(minutes=11)})
        expired = await gsc.exchange_code(old, "y", session=s)
    for r, reason in ((bad, "state-mismatch"), (expired, "state-expired")):
        assert r.error is not None and r.error.code is ErrorCode.INVALID_INPUT
        assert r.error.details == {"service": "gsc", "reason": reason}
    assert google.calls == []


async def test_exchange_upstream_failure_is_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, FakeGoogle(lambda *_: HttpResult(400, {"error": "invalid_grant"})))
    async with gsc_session(store=MemoryTokenStore()) as s:
        req = (await gsc.auth_url(REDIRECT, session=s)).unwrap()
        res = await gsc.exchange_code(req, "c", session=s)
    assert res.error is not None and res.error.code is ErrorCode.API_ERROR
    assert res.error.details["service"] == "gsc"


async def test_exchange_needs_a_token_store(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session() as s:
        req = (await gsc.auth_url(REDIRECT, session=s)).unwrap()
        res = await gsc.exchange_code(req, "c", session=s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED


async def test_connection_states() -> None:
    async with gsc_session(store=MemoryTokenStore(), with_client=False) as s:
        st = (await gsc.connection(session=s)).unwrap()
    assert (st.configured, st.connected, st.email) == (False, False, None)
    async with gsc_session() as s:
        res = await gsc.connection(session=s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED


async def test_disconnect_revokes_at_google_and_deletes_the_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, FakeGoogle(lambda m, u, kw: HttpResult(200, {})))
    store = connected_store("acme")
    async with gsc_session(store=store) as s:
        res = (await gsc.disconnect(account="acme", session=s)).unwrap()
        again = (await gsc.disconnect(account="acme", session=s)).unwrap()  # nothing left: a no-op
    assert (res.account, res.configured, res.connected) == ("acme", True, False)
    assert again.connected is False
    assert await store.get("acme") is None
    assert [(m, u, kw["form"]) for m, u, kw in google.calls] == [("POST", REVOKE_URL, {"token": REFRESH})]


async def test_disconnect_deletes_the_tokens_even_when_google_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        raise ConnectionError("google is down")

    use_google(monkeypatch, FakeGoogle(broken))
    store = connected_store()
    async with gsc_session(store=store) as s:
        with caplog.at_level(logging.WARNING):
            res = await gsc.disconnect(session=s)
    assert res.ok and res.data is not None and res.data.connected is False
    assert await store.get("default") is None
    assert "revocation failed" in caplog.text and REFRESH not in caplog.text


async def test_disconnect_logs_a_revocation_google_rejects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    body = {"error": "invalid_token", "error_description": f"Token {REFRESH} is not valid"}
    use_google(monkeypatch, FakeGoogle(lambda m, u, kw: HttpResult(400, body)))
    store = connected_store()
    async with gsc_session(store=store) as s:
        with caplog.at_level(logging.WARNING):
            res = await gsc.disconnect(session=s)
    assert res.data is not None and res.data.connected is False
    assert await store.get("default") is None
    assert "revocation rejected: invalid_token" in caplog.text
    assert REFRESH not in caplog.text and "not valid" not in caplog.text


async def test_disconnect_needs_a_token_store_but_not_the_google_app(monkeypatch: pytest.MonkeyPatch) -> None:
    async with gsc_session() as s:  # no token store: nothing to disconnect from
        res = await gsc.disconnect(session=s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED
    use_google(monkeypatch, FakeGoogle(lambda m, u, kw: HttpResult(200, {})))
    store = connected_store()
    async with gsc_session(store=store, with_client=False) as s:  # app removed: tokens can still go
        res = await gsc.disconnect(session=s)
    assert res.data is not None and (res.data.configured, res.data.connected) == (False, False)
    assert await store.get("default") is None


async def test_access_check() -> None:
    async with gsc_session(store=connected_store()) as s:
        ok = await seoprobe.check_access("search_console", session=s)
    assert ok.ok and ok.code == "ok"
    async with gsc_session(store=MemoryTokenStore()) as s:
        off = await seoprobe.check_access("search_console", session=s)
    assert not off.ok and off.code == "not-connected"
    async with gsc_session(with_client=False) as s:
        assert (await seoprobe.check_access("search_console", session=s)).code == "missing"


# ------------------------------------------------------------------ data operations


async def test_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=MemoryTokenStore()) as s:
        off = await gsc.properties(session=s)
    assert off.error is not None and off.error.code is ErrorCode.NOT_CONNECTED
    assert off.error.details == {"service": "gsc"}
    async with gsc_session() as s:
        no_store = await gsc.properties(session=s)
    assert no_store.error is not None and no_store.error.code is ErrorCode.NOT_CONNECTED
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.properties(session=s)
    assert res.unwrap().model_dump() == {"items": [{"site_url": SITE, "permission": "siteOwner"}]}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, ErrorCode.NO_PERMISSION),
        (404, ErrorCode.PROPERTY_NOT_FOUND),
        (429, ErrorCode.API_QUOTA_EXCEEDED),
        (500, ErrorCode.API_ERROR),
    ],
)
async def test_upstream_errors_map_to_error_codes(
    monkeypatch: pytest.MonkeyPatch, status: int, code: ErrorCode
) -> None:
    use_google(monkeypatch, FakeGoogle(lambda *_: HttpResult(status, {"error": "x"})))
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.sitemaps(SITE, session=s)
    assert res.error is not None and res.error.code is code and res.error.details["service"] == "gsc"
    if status == 500:
        assert res.error.details["status"] == "500"


async def test_a_disabled_search_console_api_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": "accessNotConfigured"}]}}
    use_google(monkeypatch, _google(sites=HttpResult(403, body)))
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.properties(session=s)
        summary = (await gsc.summary("https://example.com/", session=s)).unwrap()
    assert res.error is not None and res.error.code is ErrorCode.NO_PERMISSION
    assert res.error.details == {"service": "gsc", "reason": "api-not-enabled"}
    assert summary.status == "error" and summary.error == "api-not-enabled"


async def test_find_property(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.find_property("https://www.example.com/page", session=s)
        miss = await gsc.find_property("https://other.org/", session=s)
    assert res.unwrap().site_url == SITE and res.unwrap().permission == "siteOwner"
    assert miss.error is not None and miss.error.code is ErrorCode.PROPERTY_NOT_FOUND

    unverified = HttpResult(200, {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteUnverifiedUser"}]})
    use_google(monkeypatch, _google(sites=unverified))
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.find_property("example.com", session=s)
    assert res.error is not None and res.error.code is ErrorCode.NO_PERMISSION


async def test_analytics_inspect_sitemaps(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        a = (await gsc.analytics(SITE, dimensions=["query", "page"], row_limit=5, query="seo", session=s)).unwrap()
        i = (await gsc.inspect(SITE, "https://example.com/x", session=s)).unwrap()
        sm = (await gsc.sitemaps(SITE, session=s)).unwrap()
    assert a.property == SITE and a.days == 28 and a.dimensions == ["query", "page"]
    assert a.items[0].keys == ["seo", "https://example.com/"] and a.items[0].impressions == 90
    body = next(c[2]["json_body"] for c in google.calls if "searchAnalytics" in c[1])
    assert body["rowLimit"] == 5 and body["dimensionFilterGroups"][0]["filters"][0]["expression"] == "seo"
    assert i.url == "https://example.com/x" and i.indexed is False
    assert [x.path for x in sm.items] == ["https://example.com/s.xml"]


async def test_rankings(monkeypatch: pytest.MonkeyPatch) -> None:
    use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        r = (await gsc.rankings(SITE, keywords=["seo", "SEO"], session=s)).unwrap()
    async with gsc_session(store=MemoryTokenStore()) as s:
        off = await gsc.rankings(SITE, keywords=["seo"], session=s)
    assert r.keywords_requested == 1 and r.rows[0].status == "ok" and r.rows[0].position == 4.0
    assert off.error is not None and off.error.code is ErrorCode.NOT_CONNECTED


async def test_top_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"keys": ['"seo tool"'], "impressions": 40, "clicks": 2}, {"keys": []}]
    google = use_google(monkeypatch, _google(analytics=lambda body: HttpResult(200, {"rows": rows})))
    async with gsc_session(store=connected_store()) as s:
        res = (await gsc.top_queries("https://example.com/", session=s)).unwrap()
    assert res.status == "ok" and res.property == SITE
    assert [q.model_dump() for q in res.items] == [{"query": "seo tool", "impressions": 40, "clicks": 2}]
    body = next(c[2]["json_body"] for c in google.calls if "searchAnalytics" in c[1])
    assert body["dimensions"] == ["query"] and body["rowLimit"] == 12

    use_google(monkeypatch, _google(analytics=lambda body: HttpResult(200, {})))
    async with gsc_session(store=connected_store()) as s:
        assert (await gsc.top_queries("https://example.com/", session=s)).unwrap().status == "empty"
    async with gsc_session(store=connected_store(), with_client=False) as s:
        res2 = await gsc.top_queries("https://example.com/", session=s)
    assert res2.error is not None and res2.error.code is ErrorCode.MISSING_CREDENTIAL
    async with gsc_session(store=MemoryTokenStore()) as s:
        res3 = await gsc.top_queries("https://example.com/", session=s)
    assert res3.error is not None and res3.error.code is ErrorCode.NOT_CONNECTED


# ------------------------------------------------------------------ summary


async def test_summary_ok_with_findings_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store()) as s:
        res = await gsc.summary("https://example.com/", keywords=["seo"], session=s)
    assert res.ok and isinstance(res.data, GscData)
    d = res.data
    assert d.status == "ok" and d.property == SITE and d.account == "me@example.com"
    assert [i.url for i in d.inspections] == ["https://example.com/"]  # sample_urls default [url]
    assert d.rankings is not None and d.rankings.rows[0].keyword == "seo"
    assert {f.id for f in res.findings} == {"gsc-page-not-indexed"}
    assert sum(1 for c in google.calls if c[1] == INSPECT_URL) == 1
    out = json.loads(res.to_json())
    assert out["operation"] == "gsc.summary" and out["feature"] == "search"
    assert out["data"]["status"] == "ok"


async def test_summary_inspect_limit_defaults_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    urls = [f"https://example.com/p{i}" for i in range(30)]
    async with gsc_session(store=connected_store()) as s:
        d = (await gsc.summary("https://example.com/", sample_urls=urls, session=s)).unwrap()
        assert len(d.inspections) == s.config.gsc_inspect_limit + 1
        d2 = (await gsc.summary("https://example.com/", sample_urls=urls, inspect_limit=2, session=s)).unwrap()
    assert len(d2.inspections) == 3
    assert sum(1 for c in google.calls if c[1] == INSPECT_URL) == s.config.gsc_inspect_limit + 1 + 3


async def test_summary_never_fails_for_search_console_states(monkeypatch: pytest.MonkeyPatch) -> None:
    google = use_google(monkeypatch, _google())
    async with gsc_session(store=connected_store(), with_client=False) as s:
        assert (await gsc.summary("https://example.com/", session=s)).unwrap().status == "not-configured"
    async with gsc_session() as s:  # no token store
        assert (await gsc.summary("https://example.com/", session=s)).unwrap().status == "not-connected"
    async with gsc_session(store=MemoryTokenStore()) as s:
        res = await gsc.summary("https://example.com/", session=s)
    assert res.status is Status.OK and res.unwrap().status == "not-connected" and res.findings == []
    assert google.calls == []
    use_google(monkeypatch, _google(sites=HttpResult(500)))
    async with gsc_session(store=connected_store()) as s:
        d = (await gsc.summary("https://example.com/", session=s)).unwrap()
    assert d.status == "error" and d.error == "http-500"


async def test_no_secret_leaks_in_results_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    use_google(
        monkeypatch, FakeGoogle(lambda *_: HttpResult(400, {"error": "invalid_grant", "error_description": REFRESH}))
    )
    texts = []
    store = connected_store()
    await store.set("default", {**(await store.get("default") or {}), "expires_at": 0})
    async with gsc_session(store=store) as s:
        for res in (
            await gsc.connection(session=s),
            await gsc.properties(session=s),
            await gsc.auth_url(REDIRECT, session=s),
            await gsc.summary("https://example.com/", session=s),
        ):
            texts.append(res.to_json())
        req = AuthRequest(url="x", state="s", verifier="v", redirect_uri=REDIRECT, created_at=res.meta.started_at)
        texts.append((await gsc.exchange_code(req, "c", session=s)).to_json())
    blob = "\n".join(texts) + caplog.text
    for secret in (SECRET, REFRESH, ACCESS):
        assert secret not in blob


# ------------------------------------------------------------------ contract


def test_namespace_and_exports() -> None:
    assert sorted(gsc.__all__) == sorted(
        [
            "auth_url",
            "exchange_code",
            "connection",
            "disconnect",
            "properties",
            "find_property",
            "analytics",
            "inspect",
            "sitemaps",
            "rankings",
            "top_queries",
            "summary",
            "opportunities",
        ]
    )
    names = {op.name for op in search.FEATURE.operations}
    spaces = {"gsc": gsc, "ga4": ga4, "bing": bing, "indexnow": indexnow}
    assert names == {f"{ns}.{n}" for ns, mod in spaces.items() for n in mod.__all__}
    for op in search.FEATURE.operations:
        if op.name.startswith(("gsc.", "ga4.")):
            assert op.requires.network and op.requires.oauth
    for name in ("gsc", "ga4", "bing", "indexnow", "AuthRequest", "TokenStore", "MemoryTokenStore", "FileTokenStore"):
        assert name in search.__all__ and hasattr(search, name)
    from seoprobe.features.search import models

    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and obj.__module__ == models.__name__:
            assert name in search.__all__, name


def test_every_rule_id_in_code_is_in_the_catalog() -> None:
    pkg = files("seoprobe.features.search")
    catalog = json.loads(pkg.joinpath("catalog.json").read_text())
    rules = catalog["rules"]
    used: set[str] = set()
    for f in pkg.iterdir():
        if f.name.endswith(".py"):
            used |= set(re.findall(r'"((?:gsc|bing|indexnow)-[a-z0-9-]+)"', f.read_text()))
    assert used and used <= set(rules)
    assert used == set(rules)  # every declared rule is emitted somewhere
    for rid, rule in rules.items():
        for key in (rule["message_key"], rule["recommendation_key"]):
            for lang in ("en", "tr"):
                assert catalog["messages"][lang][key], (rid, lang)
    new = ["gsc-striking-distance", "gsc-low-ctr", "gsc-cannibalization", "gsc-declining-clicks"]
    for rid in [*new, "bing-crawl-issues", "indexnow-key-not-verified"]:
        assert rules[rid]["scored"] is False and rules[rid]["layer"] == "A"
        assert seoprobe.core.rules.catalog.get(rid).scored is False
    assert rules["gsc-low-ctr"]["heuristic"] is True


GSC_CALLS = [
    ("gsc.properties", lambda s: gsc.properties(session=s)),
    ("gsc.find_property", lambda s: gsc.find_property("https://a.org/", session=s)),
    ("gsc.analytics", lambda s: gsc.analytics("sc-domain:a.org", session=s)),
    ("gsc.inspect", lambda s: gsc.inspect("sc-domain:a.org", "https://a.org/", session=s)),
    ("gsc.sitemaps", lambda s: gsc.sitemaps("sc-domain:a.org", session=s)),
    ("gsc.top_queries", lambda s: gsc.top_queries("https://a.org/", session=s)),
    ("gsc.rankings", lambda s: gsc.rankings("sc-domain:a.org", keywords=["x"], session=s)),
    ("gsc.opportunities", lambda s: gsc.opportunities("sc-domain:a.org", session=s)),
]


@pytest.mark.parametrize(("name", "call"), GSC_CALLS, ids=[n for n, _ in GSC_CALLS])
async def test_every_gsc_call_says_why_it_cannot_run_the_same_way(name: str, call: Any) -> None:
    async with gsc_session(store=MemoryTokenStore(), with_client=False) as s:  # no Google app at all
        res = await call(s)
    assert res.error is not None and res.error.code is ErrorCode.MISSING_CREDENTIAL, name
    async with gsc_session(store=MemoryTokenStore()) as s:  # the app, but no connected account
        res = await call(s)
    assert res.error is not None and res.error.code is ErrorCode.NOT_CONNECTED, name
