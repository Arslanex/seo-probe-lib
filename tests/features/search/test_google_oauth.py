from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from seoprobe.core.config import Config, GoogleApp
from seoprobe.features.search.oauth import (
    REVOKE_URL,
    SCOPE_GSC_READ,
    STATE_TTL_S,
    TOKEN_URL,
    USERINFO_URL,
    FileTokenStore,
    GoogleOAuth,
    HttpResult,
    MemoryTokenStore,
    OAuthError,
)
from tests.features.search.support import ACCESS, APP, REFRESH, SECRET, FakeGoogle, make_oauth

REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"


def _grant(method: str, url: str, kw: dict[str, Any]) -> HttpResult:
    if url == TOKEN_URL:
        return HttpResult(
            200,
            {
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "scope": f"openid email {SCOPE_GSC_READ}",
            },
        )
    if url == USERINFO_URL:
        return HttpResult(200, {"email": "me@example.com"})
    return HttpResult(200, {})


async def test_not_configured() -> None:
    o = make_oauth(with_client=False)
    assert not o.is_configured()
    assert await o.status() == {"configured": False, "connected": False, "email": None}
    with pytest.raises(OAuthError) as e:
        o.authorization_request(REDIRECT)
    assert e.value.code == "not-configured"


def test_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # client credentials come from Config.google_app (Config.from_env reads the variables)
    monkeypatch.setenv("SEOPROBE_GOOGLE_CLIENT_ID", "envid")
    monkeypatch.setenv("SEOPROBE_GOOGLE_CLIENT_SECRET", "envsecret")
    cfg = Config.from_env()
    assert GoogleOAuth(cfg.google_app, MemoryTokenStore(), FakeGoogle(_grant)).is_configured()


def test_authorization_url_pkce_and_scopes() -> None:
    o = make_oauth()
    req = o.authorization_request(REDIRECT)
    url, state = req.url, req.state
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["state"] == state and q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert set(q["scope"].split()) == {SCOPE_GSC_READ, "openid", "email"}
    verifier = req.verifier
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == expect.decode()
    assert SECRET not in url
    assert o.authorization_request(REDIRECT).state != state
    assert req.redirect_uri == REDIRECT and req.account == "default" and not req.consumed
    assert verifier not in repr(req)


async def test_exchange_ok_stores_token_and_email() -> None:
    http = FakeGoogle(_grant)
    o = make_oauth(http)
    req = o.authorization_request(REDIRECT)
    verifier = req.verifier
    res = await o.exchange_code(req, "the-code")
    assert res["email"] == "me@example.com"
    token_call = next(c for c in http.calls if c[1] == TOKEN_URL)
    assert token_call[2]["form"]["code_verifier"] == verifier
    userinfo = next(c for c in http.calls if c[1] == USERINFO_URL)
    assert userinfo[2]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert await o.status() == {"configured": True, "connected": True, "email": "me@example.com"}
    assert await o.access_token() == ACCESS


async def test_wrong_state_rejected() -> None:
    http = FakeGoogle(_grant)
    o = make_oauth(http)
    req = o.authorization_request(REDIRECT)
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req, "c", state="forged")
    assert e.value.code == "invalid-state" and e.value.reason == "state-mismatch"
    # a request whose state was swapped (no longer matches its own URL) is rejected too
    forged = o.authorization_request(REDIRECT).model_copy(update={"state": "forged"})
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(forged, "c")
    assert e.value.code == "invalid-state" and e.value.reason == "state-mismatch"
    assert http.calls == []


async def test_state_is_single_use() -> None:
    o = make_oauth(FakeGoogle(_grant))
    req = o.authorization_request(REDIRECT)
    stored = req.model_dump_json()  # what a multi-instance server keeps before the callback
    await o.exchange_code(req, "c")
    assert req.consumed
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req, "c")
    assert e.value.reason == "state-reused"
    # a copy reloaded from storage is rejected as well: the token record knows its state
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(type(req).model_validate_json(stored), "c")
    assert e.value.code == "invalid-state" and e.value.reason == "state-reused"


async def test_failed_exchange_consumes_request() -> None:
    o = make_oauth(FakeGoogle(lambda *_: HttpResult(400, {"error": "invalid_grant"})))
    req = o.authorization_request(REDIRECT)
    with pytest.raises(OAuthError):
        await o.exchange_code(req, "c")
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req, "c")
    assert e.value.code == "invalid-state" and e.value.reason == "state-reused"


async def test_expired_state_rejected() -> None:
    t = [1000.0]
    http = FakeGoogle(_grant)
    o = make_oauth(http, now=lambda: t[0])
    req = o.authorization_request(REDIRECT)
    t[0] += STATE_TTL_S + 1
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req, "c")
    assert e.value.code == "invalid-state" and e.value.reason == "state-expired"
    t[0] = 1000.0
    fresh = o.authorization_request(REDIRECT)
    t[0] += STATE_TTL_S  # exactly at the end of the window: still accepted
    await o.exchange_code(fresh, "c")
    assert (await o.status())["connected"]


async def test_redirect_uri_must_match() -> None:
    o = make_oauth(FakeGoogle(_grant))
    req = o.authorization_request(REDIRECT)
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req.model_copy(update={"redirect_uri": "http://evil.example/cb"}), "c")
    assert e.value.code == "invalid-state" and e.value.reason == "redirect-mismatch"


async def test_pkce_verifier_must_match_the_challenge() -> None:
    http = FakeGoogle(_grant)
    o = make_oauth(http)
    req = o.authorization_request(REDIRECT)
    other = o.authorization_request(REDIRECT).verifier
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req.model_copy(update={"verifier": other}), "c")
    assert e.value.code == "invalid-state" and e.value.reason == "pkce-mismatch"
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(o.authorization_request(REDIRECT).model_copy(update={"verifier": "short"}), "c")
    assert e.value.reason == "pkce-mismatch"
    assert http.calls == []


async def test_request_of_another_client_rejected() -> None:
    req = make_oauth().authorization_request(REDIRECT)
    o = GoogleOAuth(GoogleApp("other-client", "s"), MemoryTokenStore(), FakeGoogle(_grant))
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(req, "c")
    assert e.value.code == "invalid-state" and e.value.reason == "client-mismatch"


async def test_exchange_failures() -> None:
    def bad(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        return HttpResult(400, {"error": "invalid_grant", "error_description": SECRET})

    o = make_oauth(FakeGoogle(bad))
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(o.authorization_request(REDIRECT), "c")
    assert e.value.code == "token-exchange-failed" and SECRET not in str(e.value)

    def no_refresh(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        return HttpResult(200, {"access_token": "a", "scope": SCOPE_GSC_READ})

    o = make_oauth(FakeGoogle(no_refresh))
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(o.authorization_request(REDIRECT), "c")
    assert e.value.code == "missing-refresh-token"

    def wrong_scope(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        return HttpResult(200, {"access_token": "a", "refresh_token": "r", "scope": "openid"})

    o = make_oauth(FakeGoogle(wrong_scope))
    with pytest.raises(OAuthError) as e:
        await o.exchange_code(o.authorization_request(REDIRECT), "c")
    assert e.value.code == "scope-not-granted"
    assert not (await o.status())["connected"]


async def test_refresh_when_expired() -> None:
    t = [1000.0]

    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        assert u == TOKEN_URL and kw["form"]["grant_type"] == "refresh_token"
        return HttpResult(200, {"access_token": "new-access", "expires_in": 3600})

    http = FakeGoogle(h)
    o = make_oauth(http, connected=True, now=lambda: t[0])
    assert await o.access_token() == ACCESS  # fresh: no HTTP
    assert http.calls == []
    t[0] = 9e12 + 1
    assert await o.access_token() == "new-access"
    assert len(http.calls) == 1
    assert await o.access_token() == "new-access"  # cached again
    assert len(http.calls) == 1
    assert (await o.store.get("default"))["refresh_token"] == REFRESH  # type: ignore[index]


async def test_invalid_grant_deletes_token() -> None:
    http = FakeGoogle(lambda *_: HttpResult(400, {"error": "invalid_grant"}))
    o = make_oauth(http, connected=True)
    assert await o.access_token(force_refresh=True) is None
    assert await o.store.get("default") is None
    assert (await o.status())["connected"] is False


async def test_transient_refresh_failure_keeps_token() -> None:
    http = FakeGoogle(lambda *_: HttpResult(None, error="timeout"))
    o = make_oauth(http, connected=True)
    with pytest.raises(OAuthError) as e:
        await o.access_token(force_refresh=True)
    assert e.value.code == "refresh-failed"
    assert await o.store.get("default") is not None


async def test_not_connected_returns_none() -> None:
    assert await make_oauth().access_token() is None


async def test_per_user_isolation() -> None:
    o = make_oauth(connected=True)
    assert await o.access_token("someone-else") is None
    assert (await o.status("someone-else"))["connected"] is False


async def test_account_from_request_is_the_one_stored() -> None:
    o = make_oauth(FakeGoogle(_grant))
    res = await o.exchange_code(o.authorization_request(REDIRECT, "tenant-7"), "c")
    assert res["account"] == "tenant-7"
    assert (await o.status("tenant-7"))["connected"] and not (await o.status())["connected"]


async def test_disconnect_revokes_and_deletes() -> None:
    http = FakeGoogle(lambda *_: HttpResult(200, {}))
    o = make_oauth(http, connected=True)
    await o.disconnect()
    assert http.calls[0][1] == REVOKE_URL and http.calls[0][2]["form"] == {"token": REFRESH}
    assert await o.store.get("default") is None


async def test_disconnect_deletes_even_if_revoke_fails() -> None:
    def boom(*_: Any) -> HttpResult:
        raise RuntimeError("network")

    o = make_oauth(FakeGoogle(boom), connected=True)
    await o.disconnect()
    assert await o.store.get("default") is None


async def test_file_token_store(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "tokens.json"
    s = FileTokenStore(p)
    assert await s.get("local") is None
    await s.set("local", {"refresh_token": "r"})
    await s.set("u2", {"refresh_token": "r2"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert await FileTokenStore(p).get("u2") == {"refresh_token": "r2"}
    await s.delete("local")
    assert await s.get("local") is None and await s.get("u2") is not None
    assert not [f for f in p.parent.iterdir() if f.name != "tokens.json"]  # no temp leftovers
    p.write_text("not json")
    assert await s.get("u2") is None


async def test_file_token_store_loses_no_update_under_concurrent_writes(tmp_path: Path) -> None:
    s = FileTokenStore(tmp_path / "tokens.json")
    await asyncio.gather(*(s.set(f"u{i}", {"refresh_token": str(i)}) for i in range(40)))
    assert [await s.get(f"u{i}") for i in range(40)] == [{"refresh_token": str(i)} for i in range(40)]


async def test_no_secrets_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def h(m: str, u: str, kw: dict[str, Any]) -> HttpResult:
        if u == TOKEN_URL and kw["form"]["grant_type"] == "refresh_token":
            return HttpResult(400, {"error": "invalid_grant", "error_description": REFRESH})
        return _grant(m, u, kw)

    o = make_oauth(FakeGoogle(h))
    await o.exchange_code(o.authorization_request(REDIRECT), "auth-code-999")
    await o.access_token(force_refresh=True)
    text = caplog.text + json.dumps(await o.status())
    for secret in (SECRET, REFRESH, ACCESS, "auth-code-999"):
        assert secret not in text


def test_credentials_repr_hides_secret() -> None:
    assert SECRET not in repr(APP) and SECRET not in repr(Config(google_app=APP))
