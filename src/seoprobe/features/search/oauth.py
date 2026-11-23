"""
Google OAuth 2.0 for Search Console (installed-app / loopback flow with PKCE).

Design:

* one OAuth client for the whole installation (``Config.google_app``), but the tokens are stored
  **per account** (``account``; ``"default"`` unless the caller names one), so the same code
  serves a multi-user product: each customer authorises their own Google account; we never see
  a password and only ask for the read-only Search Console scope;
* stateless: ``auth_url`` returns an ``AuthRequest`` (state, PKCE verifier, redirect URI, creation
  time) that the caller stores and hands back to ``exchange_code``; nothing is kept in memory, so
  it works across the instances of a multi-instance server;
* PKCE (S256) and a single-use ``state`` guard the authorisation response: the request expires
  after ``STATE_TTL_S``, is marked ``consumed`` on first use, and the token record remembers the
  state that created it, so a replayed request is rejected;
* nothing secret is ever logged or placed in an error message: errors carry a short stable
  ``code`` only, and every Google response body is reduced to its ``error`` code.

``FileTokenStore`` keeps refresh tokens in a 0600 file. That is acceptable for a single-user
machine; **a product needs encrypted storage** (a KMS-wrapped column, per-tenant keys) and a
revocation path from the user's account page.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from seoprobe.core.config import GoogleApp
from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.client import HttpClient, budget_exhausted, record
from seoprobe.core.net.safety import BlockedAddressError, URLSafetyError, check_url

from .models import AuthRequest, GscConnection

log = logging.getLogger("seoprobe.search.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPE_GSC_READ = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_GA4_READ = "https://www.googleapis.com/auth/analytics.readonly"
SERVICE_SCOPES = {"gsc": SCOPE_GSC_READ, "ga4": SCOPE_GA4_READ}
SCOPES = (SCOPE_GSC_READ, "openid", "email")
Service = Literal["gsc", "ga4"]
STATE_TTL_S = 600.0
GSC = Requirements(network=True, oauth=True)
_EXPIRY_SKEW_S = 60.0
_MAX_BODY = 1024 * 1024
_SAFE_CODE = re.compile(r"[^a-z0-9_\-]")
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")  # RFC 7636 code verifier


class OAuthError(Exception):
    """An OAuth failure. ``code`` is a short stable identifier; the message never has secrets."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason  # finer stable code (invalid-state: state-expired, pkce-mismatch, ...)


class GscError(Exception):
    """
    A Search Console (or Analytics) call failed. ``code`` is stable: no-permission,
    property-not-found, rate-limited, not-connected, http-<status>, timeout, unreachable, ...
    ``reason`` is Google's own cause when it is one we recognise (``api-not-enabled``,
    ``scope-not-granted``), so a disabled API is not mistaken for a missing permission.
    """

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason

    @property
    def label(self) -> str:
        """The most specific stable text: the reason when there is one, else the code."""
        return self.reason or self.code


# --------------------------------------------------------------------------- HTTP seam


@dataclass
class HttpResult:
    status: int | None  # None when the request itself failed
    json: Any = None
    error: str | None = None  # timeout | unreachable | target-rejected | too-large | bad-json


class Http(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> HttpResult: ...


class ApiHttp:
    """
    The production transport over the session's API client: https only, ``SafeResolver``,
    no redirects, bounded body, no retries (a token exchange is not idempotent).

    Response text is never kept (token responses are parsed to JSON and dropped).
    """

    def __init__(self, client: HttpClient) -> None:
        self._client = client  # its API session is opened on the first request

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> HttpResult:
        try:
            if check_url(url).scheme != "https":
                return HttpResult(None, error="target-rejected")
        except URLSafetyError:
            return HttpResult(None, error="target-rejected")
        if budget_exhausted():
            return HttpResult(None, error="budget-exceeded")
        started = time.monotonic()
        res = await self._send(method, url, headers, json_body, form, timeout_s)
        record(url, method=method, status=res.status, started=started, size=0, error=res.error)
        return res

    async def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        json_body: Any,
        form: dict[str, str] | None,
        timeout_s: float,
    ) -> HttpResult:
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client.api_session().request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    data=form,
                    allow_redirects=False,
                ) as resp:
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        buf.extend(chunk)
                        if len(buf) > _MAX_BODY:
                            return HttpResult(resp.status, error="too-large")
                    if not buf:
                        return HttpResult(resp.status)
                    try:
                        return HttpResult(resp.status, json=json.loads(bytes(buf)))
                    except ValueError:
                        return HttpResult(resp.status, error="bad-json")
        except BlockedAddressError:
            return HttpResult(None, error="target-rejected")
        except TimeoutError:
            return HttpResult(None, error="timeout")
        except aiohttp.ClientConnectorError as exc:
            blocked = isinstance(exc.os_error, BlockedAddressError)
            return HttpResult(None, error="target-rejected" if blocked else "unreachable")
        except aiohttp.ClientError:
            return HttpResult(None, error="unreachable")


def transport(ctx: Context) -> Http:
    """The transport for Google calls of this operation (tests replace this function)."""
    return ApiHttp(ctx.http)


def google_error_code(res: HttpResult) -> str:
    """Reduce a Google error body to its short ``error`` code (never the description)."""
    body = res.json
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, dict):  # APIs v1 style: {"error": {"status": "PERMISSION_DENIED"}}
        code = code.get("status")
    if isinstance(code, str) and code:
        return _SAFE_CODE.sub("", code.lower())[:40] or "error"
    return res.error or f"http-{res.status}"


# Google's machine-readable causes of a 403 that the caller can act on. Newer APIs put them in
# error.details[].reason (google.rpc.ErrorInfo), Search Console v3 in error.errors[].reason.
_GOOGLE_REASONS = {
    "SERVICE_DISABLED": "api-not-enabled",
    "accessNotConfigured": "api-not-enabled",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "scope-not-granted",
    "insufficientPermissions": "scope-not-granted",
}


def google_error_reason(res: HttpResult) -> str | None:
    """The recognised cause in a Google error body, or None (never free text from the body)."""
    body = res.json
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return None
    for key in ("details", "errors"):
        items = err.get(key)
        for item in items if isinstance(items, list) else []:
            reason = item.get("reason") if isinstance(item, dict) else None
            if isinstance(reason, str) and reason in _GOOGLE_REASONS:
                return _GOOGLE_REASONS[reason]
    return None


# --------------------------------------------------------------------------- token store


class TokenStore(Protocol):
    """
    Where the OAuth tokens of each account live. Async, so a database or a secret manager can
    back it without blocking the event loop; the library keeps no token in memory between calls.
    """

    async def get(self, account: str) -> dict[str, Any] | None: ...
    async def set(self, account: str, record: dict[str, Any]) -> None: ...
    async def delete(self, account: str) -> None: ...


class MemoryTokenStore:
    """Tokens in this process only (tests, scripts); lost on exit and not shared between processes."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}

    async def get(self, account: str) -> dict[str, Any] | None:
        r = self._d.get(account)
        return dict(r) if r else None

    async def set(self, account: str, record: dict[str, Any]) -> None:
        self._d[account] = dict(record)

    async def delete(self, account: str) -> None:
        self._d.pop(account, None)


class FileTokenStore:
    """
    JSON file keyed by account, mode 0600, written atomically (see the module docstring).

    File access runs on a worker thread; a read-modify-write holds a lock, so concurrent tasks and
    threads of one process never lose an update. It is not safe across processes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".google-tokens-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _update(self, account: str, record: dict[str, Any] | None) -> None:
        with self._lock:
            data = self._read()
            if record is not None:
                data[account] = record
            elif data.pop(account, None) is None:
                return
            self._write(data)

    async def get(self, account: str) -> dict[str, Any] | None:
        r = (await asyncio.to_thread(self._read)).get(account)
        return dict(r) if isinstance(r, dict) else None

    async def set(self, account: str, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._update, account, record)

    async def delete(self, account: str) -> None:
        await asyncio.to_thread(self._update, account, None)


# --------------------------------------------------------------------------- OAuth


def _requested(request: AuthRequest) -> list[str]:
    return request.scopes or [SCOPE_GSC_READ]  # no scopes recorded: Search Console, the default service


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class GoogleOAuth:
    def __init__(
        self,
        app: GoogleApp | None,
        store: TokenStore,
        http: Http,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.store = store
        self.http = http
        self._now = now
        self._locks: dict[str, asyncio.Lock] = {}

    # -- configuration / status --------------------------------------------------

    def is_configured(self) -> bool:
        return self.app is not None

    async def status(self, account: str = "default") -> dict[str, Any]:
        rec = await self.store.get(account)
        connected = bool(rec and rec.get("refresh_token"))
        return {
            "configured": self.is_configured(),
            "connected": connected,
            "email": rec.get("email") if connected and rec else None,
        }

    # -- authorisation --------------------------------------------------------------

    async def has_scope(self, account: str, scope: str) -> bool:
        rec = await self.store.get(account)
        return bool(rec) and scope in str((rec or {}).get("scope") or "").split()

    def authorization_request(
        self, redirect_uri: str, account: str = "default", services: Sequence[Service] = ("gsc",)
    ) -> AuthRequest:
        """
        One consent for every service in ``services`` (``("gsc", "ga4")`` asks for both).

        ``include_granted_scopes`` keeps the scopes the account granted earlier, so connecting
        Analytics later does not drop Search Console from the token (and the other way round).
        """
        if self.app is None:
            raise OAuthError("not-configured")
        wanted = [SERVICE_SCOPES[x] for x in dict.fromkeys(services or ("gsc",))]
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": self.app.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join([*wanted, "openid", "email"]),
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",  # always returns a refresh token
            }
        )
        return AuthRequest(
            url=f"{AUTH_URL}?{query}",
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            account=account,
            created_at=datetime.fromtimestamp(self._now(), UTC),
            scopes=wanted,
        )

    def _rejection(self, request: AuthRequest, state: str | None) -> str | None:
        """
        Why the request is refused, or None when it is fresh, unused, untampered (its URL
        carries the same state, redirect URI and the S256 challenge of its verifier) and matches
        the returned ``state``.
        """
        if request.consumed:
            return "state-reused"
        if request.created_at.timestamp() < self._now() - STATE_TTL_S:
            return "state-expired"
        if state is not None and not hmac.compare_digest(state, request.state):
            return "state-mismatch"
        q = {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}
        if q.get("state") != request.state:
            return "state-mismatch"
        if q.get("redirect_uri") != request.redirect_uri:
            return "redirect-mismatch"
        if self.app is None or q.get("client_id") != self.app.client_id:
            return "client-mismatch"
        if not set(_requested(request)) <= set(q.get("scope", "").split()):
            return "scope-mismatch"
        if (
            not _VERIFIER.fullmatch(request.verifier)
            or q.get("code_challenge_method") != "S256"
            or not hmac.compare_digest(q.get("code_challenge", ""), _challenge(request.verifier))
        ):
            return "pkce-mismatch"
        return None

    async def exchange_code(self, request: AuthRequest, code: str, *, state: str | None = None) -> dict[str, Any]:
        """Trade the authorisation code for tokens. The request is single use."""
        if self.app is None:
            raise OAuthError("not-configured")
        reason = self._rejection(request, state)
        request.consumed = True  # one-time: a replayed request is rejected
        prev = await self.store.get(request.account)
        if reason is None and prev and prev.get("auth_state") == request.state:
            reason = "state-reused"
        if reason is not None:
            raise OAuthError("invalid-state", reason)
        res = await self.http.request(
            "POST",
            TOKEN_URL,
            form={
                "code": code,
                "client_id": self.app.client_id,
                "client_secret": self.app.client_secret,
                "redirect_uri": request.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": request.verifier,
            },
        )
        body = res.json if isinstance(res.json, dict) else {}
        if res.status != 200 or not body.get("access_token"):
            log.warning("token exchange failed: %s", google_error_code(res))
            raise OAuthError("token-exchange-failed")
        if not body.get("refresh_token"):
            raise OAuthError("missing-refresh-token")
        granted = str(body.get("scope", "")).split()
        if any(sc not in granted for sc in _requested(request)):
            raise OAuthError("scope-not-granted")
        expires = self._now() + float(body.get("expires_in") or 3600)
        email = await self._email(str(body["access_token"]))
        await self.store.set(
            request.account,
            {
                "refresh_token": body["refresh_token"],
                "access_token": body["access_token"],
                "expires_at": expires,
                "email": email,
                "scope": body.get("scope"),
                "auth_state": request.state,
            },
        )
        log.info("google account connected for account %s", request.account)
        return {"email": email, "account": request.account}

    async def _email(self, access_token: str) -> str | None:
        res = await self.http.request("GET", USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        body = res.json if isinstance(res.json, dict) else {}
        email = body.get("email") if res.status == 200 else None
        return str(email) if email else None

    # -- tokens ---------------------------------------------------------------------

    async def access_token(self, account: str = "default", *, force_refresh: bool = False) -> str | None:
        """
        A valid access token, refreshed when needed.

        ``None`` means "not connected" (never connected, or Google revoked the grant).
        A transient failure raises ``OAuthError("refresh-failed")`` instead, so a network
        blip is not mistaken for a revoked connection.
        """
        rec = await self.store.get(account)
        if not rec or not rec.get("refresh_token"):
            return None
        fresh = (
            not force_refresh
            and rec.get("access_token")
            and float(rec.get("expires_at", 0)) - _EXPIRY_SKEW_S > self._now()
        )
        if fresh:
            return str(rec["access_token"])
        if self.app is None:
            raise OAuthError("not-configured")
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            rec = await self.store.get(account)  # another task may have refreshed meanwhile
            if not rec or not rec.get("refresh_token"):
                return None
            if (
                not force_refresh
                and rec.get("access_token")
                and float(rec.get("expires_at", 0)) - _EXPIRY_SKEW_S > self._now()
            ):
                return str(rec["access_token"])
            res = await self.http.request(
                "POST",
                TOKEN_URL,
                form={
                    "client_id": self.app.client_id,
                    "client_secret": self.app.client_secret,
                    "refresh_token": str(rec["refresh_token"]),
                    "grant_type": "refresh_token",
                },
            )
            body = res.json if isinstance(res.json, dict) else {}
            if res.status == 400 and google_error_code(res) == "invalid_grant":
                await self.store.delete(account)  # revoked or expired: the user must reconnect
                log.warning("google grant revoked for account %s", account)
                return None
            if res.status != 200 or not body.get("access_token"):
                log.warning("token refresh failed: %s", google_error_code(res))
                raise OAuthError("refresh-failed")
            rec["access_token"] = body["access_token"]
            rec["expires_at"] = self._now() + float(body.get("expires_in") or 3600)
            if body.get("refresh_token"):  # Google may rotate it
                rec["refresh_token"] = body["refresh_token"]
            await self.store.set(account, rec)
            return str(rec["access_token"])

    async def disconnect(self, account: str = "default") -> None:
        rec = await self.store.get(account)
        if rec and rec.get("refresh_token"):
            try:  # best effort: the local deletion below must happen either way
                res = await self.http.request("POST", REVOKE_URL, form={"token": str(rec["refresh_token"])})
            except Exception:  # noqa: BLE001
                log.warning("google token revocation failed")
            else:
                if res.status != 200:  # e.g. already revoked or expired: the grant is gone or unknown
                    log.warning("google token revocation rejected: %s", google_error_code(res))
        await self.store.delete(account)


# --------------------------------------------------------------------------- operations

_OAUTH_ERRORS = {
    "not-configured": ErrorCode.MISSING_CREDENTIAL,
    "invalid-state": ErrorCode.INVALID_INPUT,
    "scope-not-granted": ErrorCode.NO_PERMISSION,
}


def token_store(ctx: Context) -> TokenStore:
    store: TokenStore | None = ctx.session.token_store
    if store is None:
        ctx.fail(ErrorCode.NOT_CONNECTED, service="gsc")
    return store


def google_oauth(ctx: Context, store: TokenStore | None = None) -> GoogleOAuth:
    return GoogleOAuth(ctx.config.google_app, store or token_store(ctx), transport(ctx))


async def _connection(oauth: GoogleOAuth, account: str) -> GscConnection:
    st = await oauth.status(account)
    return GscConnection(account=account, configured=st["configured"], connected=st["connected"], email=st["email"])


@operation("gsc.auth_url", output=AuthRequest, requires=GSC, timeout_s=10)
async def auth_url(
    ctx: Context, redirect_uri: str, *, account: str = "default", services: list[Service] | None = None
) -> Outcome[AuthRequest]:
    """
    Google authorisation URL plus the pending request the caller stores until the callback.

    ``services`` picks the read-only scopes of one consent: ``["gsc"]`` (default), ``["ga4"]`` or
    ``["gsc", "ga4"]``.
    """
    oauth = GoogleOAuth(ctx.config.google_app, MemoryTokenStore(), transport(ctx))
    try:
        return Outcome(data=oauth.authorization_request(redirect_uri, account, services or ("gsc",)))
    except OAuthError:
        ctx.fail(ErrorCode.MISSING_CREDENTIAL, missing="credential:google_app", service="gsc")


@operation("gsc.exchange_code", output=GscConnection, requires=GSC, timeout_s=30)
async def exchange_code(
    ctx: Context, request: AuthRequest, code: str, *, state: str | None = None
) -> Outcome[GscConnection]:
    """Trade the authorisation code of a stored ``AuthRequest`` for tokens (kept in the token store)."""
    oauth = google_oauth(ctx)
    try:
        await oauth.exchange_code(request, code, state=state)
    except OAuthError as exc:
        err = _OAUTH_ERRORS.get(exc.code, ErrorCode.API_ERROR)
        ctx.log.debug("oauth exchange failed: %s %s", exc.code, exc.reason or "")
        if err is ErrorCode.MISSING_CREDENTIAL:
            ctx.fail(err, missing="credential:google_app", service="gsc")
        if exc.reason:
            ctx.fail(err, service="gsc", reason=exc.reason)
        ctx.fail(err, service="gsc")
    return Outcome(data=await _connection(oauth, request.account))


@operation("gsc.disconnect", output=GscConnection, requires=GSC, timeout_s=30)
async def disconnect(ctx: Context, *, account: str = "default") -> Outcome[GscConnection]:
    """
    Forget the account's Google connection: revoke the grant at Google, then delete its tokens.

    The revocation is best effort (a failure is logged, never returned); the tokens are deleted
    from the token store either way. Disconnecting an account that is not connected is a no-op.
    """
    oauth = google_oauth(ctx)
    await oauth.disconnect(account)
    return Outcome(data=await _connection(oauth, account))


@operation("gsc.connection", output=GscConnection, requires=GSC, timeout_s=10)
async def connection(ctx: Context, *, account: str = "default") -> Outcome[GscConnection]:
    """Whether the Google app is configured and the account is connected (no network)."""
    return Outcome(data=await _connection(google_oauth(ctx), account))


async def access_check(session: Any) -> tuple[bool, str]:
    """``check_access("search_console")``: is the default account connected?"""
    if session.config.google_app is None:
        return False, "missing"
    store = session.token_store
    rec = await store.get("default") if store is not None else None
    return (True, "ok") if rec and rec.get("refresh_token") else (False, "not-connected")


async def ga4_access_check(session: Any) -> tuple[bool, str]:
    """``check_access("ga4")``: is the default account connected with the Analytics scope?"""
    ok, code = await access_check(session)
    if not ok:
        return ok, code
    rec = await session.token_store.get("default") or {}
    return (True, "ok") if SCOPE_GA4_READ in str(rec.get("scope") or "").split() else (False, "scope-not-granted")


# --------------------------------------------------------------------------- Google API client


class GoogleClient:
    """Bearer-token JSON calls for one account: refreshes once on 401, errors as ``GscError``."""

    def __init__(self, oauth: GoogleOAuth, account: str = "default", http: Http | None = None) -> None:
        self.oauth = oauth
        self.account = account
        self.http: Http = http or oauth.http

    async def _call(self, method: str, url: str, *, json_body: Any = None, timeout_s: float = 30.0) -> Any:
        refreshed = False
        while True:
            try:
                token = await self.oauth.access_token(self.account, force_refresh=refreshed)
            except OAuthError as exc:
                raise GscError(exc.code) from None
            if token is None:
                raise GscError("not-connected")
            res: HttpResult = await self.http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json_body=json_body,
                timeout_s=timeout_s,
            )
            if res.status == 401 and not refreshed:
                refreshed = True  # the access token was rejected: refresh once and retry
                continue
            if res.status is None:
                raise GscError(res.error or "unreachable")
            if res.status == 200:
                return res.json if res.json is not None else {}
            raise GscError(
                {
                    401: "not-connected",
                    403: "no-permission",
                    404: "property-not-found",
                    429: "rate-limited",
                }.get(res.status, f"http-{res.status}"),
                google_error_reason(res) if res.status == 403 else None,
            )
