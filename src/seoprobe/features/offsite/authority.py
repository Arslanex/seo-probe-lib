"""
Domain authority: OpenPageRank estimate for the site's registrable domain.

``GET https://openpagerank.keywordseverywhere.com/api/v1.0/getPageRank?domains[]=<domain>`` with the
key (``Config.openpagerank_key``) in the ``API-OPR`` header. The number is a THIRD-PARTY estimate
(built from Common Crawl), not a Google metric: it produces no findings and never touches a score.
Without a key the result is ``NotChecked("authority", "no-api-key")``.

The key travels only in that header: it is never put in a URL, a log line, an error code or a
result. Same safety rules as the other public lookups (``check_url`` on the URL, the
``SafeResolver`` of the API session at connect time, no redirects, a small body cap). Never
raises: callers get a short stable code in ``AuthoritySignal.error`` (``no-api-key``, ``timeout``,
``unreachable``, ``target-rejected``, ``unauthorized``, ``rate-limited``, ``http-<status>``,
``too-large``, ``bad-json``, ``no-data``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import aiohttp

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.api import Session
from seoprobe.core.net.client import record
from seoprobe.core.net.safety import BlockedAddressError, URLSafetyError, check_url
from seoprobe.core.page import Page
from seoprobe.core.parse.urls import origin_of

from .models import AuthoritySignal
from .visibility import registrable_domain

if TYPE_CHECKING:
    from seoprobe.core.session import Session as ProbeSession

# The service moved from openpagerank.com (now a 301) to Keywords Everywhere; keys of the old
# service no longer work. The legacy path still takes the key in the API-OPR header.
ENDPOINT = "https://openpagerank.keywordseverywhere.com/api/v1.0/getPageRank"
MAX_BODY = 64 * 1024
TIMEOUT_S = 20.0


def parse_response(data: Any, domain: str) -> AuthoritySignal:
    """First record of the ``response`` list; ``status_code`` 200 means the domain was found."""
    rows = data.get("response") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return AuthoritySignal(domain=domain, error="bad-json")
    row = rows[0]
    if row.get("status_code") != 200:
        return AuthoritySignal(domain=domain, error="no-data")

    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    dec = num(row.get("page_rank_decimal"))
    integer = num(row.get("page_rank_integer"))
    rank_raw = row.get("rank")
    rank = int(rank_raw) if isinstance(rank_raw, str) and rank_raw.isdigit() else None
    if isinstance(rank_raw, int) and not isinstance(rank_raw, bool):
        rank = rank_raw
    return AuthoritySignal(
        domain=domain,
        page_rank_decimal=dec,
        page_rank_integer=int(integer) if integer is not None else None,
        rank=rank,
    )


async def _get(session: Session, url: str, key: str, timeout_s: float) -> tuple[int | None, bytes, str | None]:
    try:
        check_url(url)
    except URLSafetyError:
        return None, b"", "target-rejected"
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            async with session.get(
                url, headers={"API-OPR": key, "Accept": "application/json"}, allow_redirects=False
            ) as resp:
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(8192):
                    buf.extend(chunk)
                    if len(buf) > MAX_BODY:
                        record(url, status=resp.status, started=started, size=len(buf), error="too-large")
                        return resp.status, b"", "too-large"
                record(url, status=resp.status, started=started, size=len(buf), error=None)
                return resp.status, bytes(buf), None
    except BlockedAddressError:
        err = "target-rejected"
    except TimeoutError:
        err = "timeout"
    except aiohttp.ClientConnectorError as exc:
        err = "target-rejected" if isinstance(exc.os_error, BlockedAddressError) else "unreachable"
    except aiohttp.ClientError:
        err = "unreachable"
    record(url, status=None, started=started, size=0, error=err)
    return None, b"", err


async def fetch_authority(
    host: str, *, session: Session, key: str | None, timeout_s: float = TIMEOUT_S
) -> AuthoritySignal:
    domain = registrable_domain(host)
    if not key:
        return AuthoritySignal(domain=domain, error="no-api-key")
    if not domain:
        return AuthoritySignal(domain=domain, error="no-data")
    url = f"{ENDPOINT}?domains%5B%5D={quote(domain, safe='')}"
    status, body, err = await _get(session, url, key, timeout_s)
    if err:
        return AuthoritySignal(domain=domain, error=err)
    if status in (401, 403):
        return AuthoritySignal(domain=domain, error="unauthorized")
    if status == 429:
        return AuthoritySignal(domain=domain, error="rate-limited")
    if status != 200:
        return AuthoritySignal(domain=domain, error=f"http-{status}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return AuthoritySignal(domain=domain, error="bad-json")
    return parse_response(data, domain)


@operation("authority", output=AuthoritySignal, requires=Requirements(network=True), timeout_s=TIMEOUT_S)
async def authority(ctx: Context, target: str | Page) -> Outcome[AuthoritySignal]:
    """OpenPageRank estimate of the site's registrable domain (third-party, never scored)."""
    key = ctx.config.openpagerank_key
    if not key:
        url = target.effective_url if isinstance(target, Page) else target
        return Outcome(
            data=AuthoritySignal(domain=registrable_domain(urlparse(url).hostname or ""), error="no-api-key"),
            not_checked=[ctx.not_checked("authority", "no-api-key")],
        )
    page = await ctx.page(target)
    host = urlparse(origin_of(page.effective_url)).hostname or ""
    remaining = ctx.remaining_s()
    timeout_s = TIMEOUT_S if remaining is None else max(1.0, min(TIMEOUT_S, remaining - 0.5))
    data = await fetch_authority(host, session=ctx.http.api_session(), key=key, timeout_s=timeout_s)
    if data.error:
        return Outcome(data=data, not_checked=[ctx.not_checked("authority", f"authority-{data.error}")])
    return Outcome(data=data)


# ----------------------------------------------------------------------------- access check


def _test_code(name: str, error: str | None) -> str:
    """Map a provider's error code onto the few outcomes the settings page can explain."""
    if error is None or error == "no-data":  # no data for the sample site still means the key works
        return "ok"
    if error in ("unauthorized", "http-400", "http-401", "http-403"):
        return f"rejected.{name}"
    if error in ("rate-limited", "unreachable", "timeout"):
        return "unreachable" if error == "timeout" else error
    if error == "no-api-key":
        return "missing"
    return "other"


async def check_openpagerank_key(session: ProbeSession) -> tuple[bool, str]:
    """Try the configured OpenPageRank key with one harmless request. Never echoes the key."""
    key = session.config.openpagerank_key
    if not key:
        return False, _test_code("opr", "no-api-key")
    sig = await fetch_authority("example.com", session=session.http.api_session(), key=key)
    code = _test_code("opr", sig.error)
    return code == "ok", code
