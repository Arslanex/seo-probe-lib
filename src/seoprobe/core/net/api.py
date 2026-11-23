"""
External JSON APIs (PageSpeed, CrUX, Search Console, OpenPageRank, RDAP, Wayback, Common Crawl).

The targets are fixed hosts, but every URL and every redirect hop still goes through
``check_url`` and the session through ``SafeResolver``: the same rule as for user URLs.
Errors never raise: callers get a short stable code (``timeout``, ``unreachable``,
``target-rejected``, ``http-<status>``, ``too-large``, ``rate-limited``).

Only these requests are retried (never the audited site): idempotent calls that failed with 429
or 5xx, at most ``RETRIES`` times, exponential backoff with full jitter, honouring Retry-After.
Keys travel in headers, never in the URL.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp

from seoprobe.core.net.client import budget_exhausted, record
from seoprobe.core.net.safety import BlockedAddressError, URLSafetyError, check_url

MAX_BODY = 2 * 1024 * 1024
MAX_REDIRECTS = 3
RETRIES = 3
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
Session = Any  # aiohttp.ClientSession or a test double with the same ``get`` / ``request`` shape


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(BACKOFF_CAP_S, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return random.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt))


async def get_text(session: Session, url: str, *, timeout_s: float = 30.0) -> tuple[int | None, str | None, str | None]:
    """GET ``url`` (https only). Returns (status, body text, error code)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            target = check_url(current)
        except URLSafetyError:
            return None, None, "target-rejected"
        if target.scheme != "https":
            return None, None, "target-rejected"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        record(current, status=resp.status, started=started, size=0, error=None)
                        current = urljoin(current, resp.headers["Location"])
                        continue
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        buf.extend(chunk)
                        if len(buf) > MAX_BODY:
                            record(current, status=resp.status, started=started, size=len(buf), error="too-large")
                            return resp.status, None, "too-large"
                    raw = bytes(buf)
                    record(current, status=resp.status, started=started, size=len(raw), error=None)
                    return resp.status, raw.decode("utf-8", "replace"), None
        except BlockedAddressError:
            err = "target-rejected"
        except TimeoutError:
            err = "timeout"
        except aiohttp.ClientConnectorError as exc:
            err = "target-rejected" if isinstance(exc.os_error, BlockedAddressError) else "unreachable"
        except aiohttp.ClientError:
            err = "unreachable"
        record(current, status=None, started=started, size=0, error=err)
        return None, None, err
    return None, None, "redirect-loop"


async def get_json(session: Session, url: str, *, timeout_s: float = 30.0) -> tuple[Any, str | None]:
    """Returns (parsed JSON | None, error code | None). 404 is reported as ``http-404``."""
    status, text, err = await get_text(session, url, timeout_s=timeout_s)
    if err:
        return None, err
    if status != 200 or text is None:
        return None, f"http-{status}"
    try:
        return json.loads(text), None
    except ValueError:
        return None, "bad-json"


async def request_json(
    session: Session,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout_s: float = 30.0,
    retries: int = RETRIES,
) -> tuple[int | None, Any, str | None]:
    """
    A keyed JSON API call. Returns (status, parsed body | None, error code | None).

    Redirects are not followed (a redirect is reported as ``http-3xx``). A final 429 is
    ``rate-limited``; other non-2xx answers are ``http-<status>``. The body of an error answer
    is still returned when it is JSON (APIs explain errors there).
    """
    try:
        target = check_url(url)
    except URLSafetyError:
        return None, None, "target-rejected"
    if target.scheme != "https":
        return None, None, "target-rejected"
    shown = f"{url}?{urlencode(params)}" if params else url
    attempt = 0
    while True:
        if budget_exhausted():
            return None, None, "budget-exceeded"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                async with session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as resp:
                    status: int = resp.status
                    retry_after = resp.headers.get("Retry-After")
                    raw = await resp.content.read(MAX_BODY + 1)
        except BlockedAddressError:
            err = "target-rejected"
        except TimeoutError:
            err = "timeout"
        except aiohttp.ClientConnectorError as exc:
            err = "target-rejected" if isinstance(exc.os_error, BlockedAddressError) else "unreachable"
        except aiohttp.ClientError:
            err = "unreachable"
        else:
            if len(raw) > MAX_BODY:
                record(shown, method=method, status=status, started=started, size=len(raw), error="too-large")
                return status, None, "too-large"
            transient = status == 429 or status >= 500
            api_err = None if 200 <= status < 300 else ("rate-limited" if status == 429 else f"http-{status}")
            record(shown, method=method, status=status, started=started, size=len(raw), error=api_err)
            if transient and attempt < retries:
                await asyncio.sleep(_retry_delay(attempt, retry_after))
                attempt += 1
                continue
            body: Any = None
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    if api_err is None:
                        return status, None, "bad-json"
            return status, body, api_err
        record(shown, method=method, status=None, started=started, size=0, error=err)
        if err in ("timeout", "unreachable") and attempt < retries:
            await asyncio.sleep(_retry_delay(attempt, None))
            attempt += 1
            continue
        return None, None, err


def host_of(value: str) -> str:
    """Bare lower-case host from a domain or URL (no scheme, port, path, or leading dot)."""
    v = value.strip().lower()
    if "://" not in v:
        v = "//" + v
    return (urlparse(v).hostname or "").strip(".")
