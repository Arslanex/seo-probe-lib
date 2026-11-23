"""
The single outbound HTTP client for user-supplied URLs.

Every request and every redirect hop passes ``check_url`` first; hostnames are additionally
judged by ``SafeResolver`` at connect time. Redirects are followed manually
(``allow_redirects=False``). Each response is recorded in the running operation's request log
(URL secrets masked) and counted against its byte budget.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from seoprobe.core.config import Config
from seoprobe.core.models import FetchResult, Hop
from seoprobe.core.net.politeness import HostLimiter
from seoprobe.core.net.safety import (
    BlockedAddressError,
    DNSResolutionError,
    SafeResolver,
    Target,
    URLSafetyError,
    check_url,
)
from seoprobe.core.parse.text import decode_body
from seoprobe.core.result import RequestLog

MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT = {301, 302, 303, 307, 308}
_TEXT_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")
_SECRET_PARAMS = frozenset({"key", "api_key", "apikey", "access_token", "token", "code", "state"})


@dataclass
class RawResponse:
    """One HTTP response as bytes, before any decoding."""

    url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    truncated: bool
    error: str | None = None
    redirect_chain: list[Hop] | None = None
    ttfb_ms: int | None = None
    total_ms: int | None = None


def mask_url(url: str) -> str:
    """The URL with secret-looking query parameters replaced by ``***``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = [
        (k, "***" if k.lower() in _SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="*")))


@dataclass
class Recorder:
    """Requests and downloaded bytes of one running operation."""

    byte_limit: int
    requests: list[RequestLog] = field(default_factory=list)
    size_bytes: int = 0

    def over_budget(self) -> bool:
        return self.size_bytes >= self.byte_limit

    def add(self, entry: RequestLog) -> None:
        self.requests.append(entry)
        self.size_bytes += entry.size_bytes


_recorder: ContextVar[Recorder | None] = ContextVar("seoprobe_recorder", default=None)


@contextmanager
def recording(byte_limit: int) -> Iterator[Recorder]:
    """Collect the requests made inside this block; a nested block's requests also count for its parent."""
    rec = Recorder(byte_limit=byte_limit)
    parent = _recorder.get()
    if parent is not None:  # the inner operation spends the outer operation's budget too
        rec.byte_limit = min(byte_limit, max(0, parent.byte_limit - parent.size_bytes))
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)
        if parent is not None:
            parent.requests.extend(rec.requests)
            parent.size_bytes += rec.size_bytes


def record(url: str, *, method: str = "GET", status: int | None, started: float, size: int, error: str | None) -> None:
    """Add one request to the running operation's log (no-op outside an operation)."""
    rec = _recorder.get()
    if rec is None:
        return
    rec.add(
        RequestLog(
            url=mask_url(url),
            method=method,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            size_bytes=size,
            error=(error or "").partition(":")[0] or None,  # code only, never exception text
        )
    )


def budget_exhausted() -> bool:
    rec = _recorder.get()
    return rec is not None and rec.over_budget()


def _collect_headers(raw: Any) -> dict[str, str]:
    """Lower-cased headers; repeated headers are joined (X-Robots-Tag: a, b), cookies by newline."""
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        out.setdefault(k.lower(), []).append(v)
    return {k: ("\n" if k == "set-cookie" else ", ").join(v) for k, v in out.items()}


class HttpClient:
    """SSRF-safe client for user-supplied URLs; also owns the session used for external APIs."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        limiter: HostLimiter | None = None,
        resolver: SafeResolver | None = None,
        url_check: Callable[[str], Target] = check_url,  # injection seam for tests only
    ) -> None:
        self.config = config or Config()
        self.limiter = limiter or HostLimiter(self.config.per_host_concurrency, self.config.request_delay_s)
        self._url_check = url_check
        self._resolver = resolver or SafeResolver()
        self._session: aiohttp.ClientSession | None = None
        self._api_session: aiohttp.ClientSession | None = None
        self.user_agent = self.config.user_agent

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                resolver=self._resolver, use_dns_cache=False, limit=self.config.max_connections
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.config.fetch_timeout_s),
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",  # Accept-Language deliberately absent
                },
                auto_decompress=True,
            )
        return self._session

    def api_session(self) -> aiohttp.ClientSession:
        """Session for external JSON APIs (same resolver; JSON accept header)."""
        if self._api_session is None or self._api_session.closed:
            self._api_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=self._resolver, use_dns_cache=False, limit=min(10, self.config.max_connections)
                ),
                timeout=aiohttp.ClientTimeout(total=30.0),
                headers={"User-Agent": self.config.api_user_agent, "Accept": "application/json, */*;q=0.5"},
            )
        return self._api_session

    async def close(self) -> None:
        for s in (self._session, self._api_session):
            if s is not None and not s.closed:
                await s.close()

    # ------------------------------------------------------------------ core

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        limit = self.config.max_response_bytes if max_bytes is None else max_bytes
        t0 = time.monotonic()
        chain: list[Hop] = []
        current = url
        if budget_exhausted():
            return RawResponse(url, current, None, {}, b"", False, "budget-exceeded", chain)
        session = self._get_session()
        for _ in range(MAX_REDIRECTS + 1):
            try:
                target = self._url_check(current)
            except URLSafetyError as exc:
                return RawResponse(url, current, None, {}, b"", False, f"target-rejected: {exc}", chain)
            host = target.hostname
            if self.limiter.is_open(host):
                return RawResponse(url, current, None, {}, b"", False, "circuit-open", chain)
            hop_started = time.monotonic()
            try:
                async with self.limiter.slot(host):
                    async with session.get(current, allow_redirects=False) as resp:
                        ttfb = int((time.monotonic() - t0) * 1000)
                        headers = _collect_headers(resp.headers)
                        status = resp.status
                        if status in _REDIRECT and headers.get("location"):
                            chain.append(Hop(url=current, status=status))
                            record(current, status=status, started=hop_started, size=0, error=None)
                            current = urljoin(current, headers["location"])
                            self.limiter.record(host, True)
                            continue
                        body = bytearray()
                        truncated = False
                        async for chunk in resp.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > limit:
                                truncated = True
                                del body[limit:]
                                break
                        self.limiter.record(host, status < 500 and status != 429)
                        record(current, status=status, started=hop_started, size=len(body), error=None)
                        return RawResponse(
                            url,
                            current,
                            status,
                            headers,
                            bytes(body),
                            truncated,
                            None,
                            chain,
                            ttfb,
                            int((time.monotonic() - t0) * 1000),
                        )
            except BlockedAddressError as exc:
                err = f"target-rejected: {exc}"
            except aiohttp.ClientConnectorCertificateError as exc:
                self.limiter.record(host, False)
                err = f"tls-handshake-failed: {exc}"
            except (ssl.SSLError, aiohttp.ClientSSLError) as exc:
                self.limiter.record(host, False)
                err = f"tls-handshake-failed: {exc}"
            except aiohttp.ClientConnectorError as exc:
                self.limiter.record(host, False)
                inner = exc.os_error
                code = "target-rejected" if isinstance(inner, BlockedAddressError) else "unreachable"
                if isinstance(inner, DNSResolutionError):
                    code = "dns-failed"
                err = f"{code}: {inner}"
            except TimeoutError:
                self.limiter.record(host, False)
                err = "timeout"
            except aiohttp.ClientError as exc:
                self.limiter.record(host, False)
                err = f"fetch-error: {exc}"
            record(current, status=None, started=hop_started, size=0, error=err)
            return RawResponse(url, current, None, {}, b"", False, err, chain)
        return RawResponse(url, current, None, {}, b"", False, "redirect-loop", chain)

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        raw = await self.fetch_bytes(url, max_bytes=max_bytes)
        ctype = raw.headers.get("content-type")
        html: str | None = None
        if raw.error is None and raw.body and (ctype is None or ctype.lower().startswith(_TEXT_TYPES)):
            html = decode_body(raw.body, ctype)
        code, _, detail = (raw.error or "").partition(": ")
        return FetchResult(
            url=url,
            final_url=raw.final_url,
            status=raw.status,
            headers=raw.headers,
            redirect_chain=raw.redirect_chain or [],
            content_type=ctype,
            html=html,
            html_bytes=len(raw.body),
            truncated=raw.truncated,
            ttfb_ms=raw.ttfb_ms,
            total_ms=raw.total_ms,
            error=code or None,
            error_detail=detail or None,
            fetched_at=datetime.now(UTC),
        )


class RequestBudget:
    """
    At most ``limit`` requests for one analysis; beyond it every call answers ``budget-exceeded``
    (the same code as the byte budget of the running operation).
    """

    def __init__(self, http: HttpClient, limit: int) -> None:
        self.http = http
        self.left = limit
        self.used = 0

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        if self.left <= 0:
            return RawResponse(url, url, None, {}, b"", False, "budget-exceeded")
        self.left -= 1
        self.used += 1
        try:
            return await self.http.fetch_bytes(url, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 - one fetch must not break the analysis
            return RawResponse(url, url, None, {}, b"", False, f"fetch-error: {type(exc).__name__}")
