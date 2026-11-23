"""
Bing Webmaster Tools: ``seoprobe.bing``.

JSON endpoints of the Bing Webmaster API (``https://ssl.bing.com/webmaster/api.svc/json/<Method>``)
with ``Config.bing_api_key``. The API accepts the key only as the ``apikey`` query parameter (no
header variant is documented; the alternative is OAuth), so the key does travel in the URL:
``RequestLog`` masks ``apikey`` and the key is never logged or put into a result or error.

Answers are wrapped in ``{"d": ...}``; dates look like ``/Date(1316156400000-0700)/`` and are
returned as ISO 8601 UTC. Errors carry an ``ApiErrorCode`` (``ErrorCode`` field) that is mapped to
the library's codes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.api import request_json

from .models import (
    BingCrawlIssue,
    BingCrawlIssues,
    BingCrawlStat,
    BingCrawlStats,
    BingQueryStat,
    BingQueryStats,
    BingSite,
    BingSites,
    BingUrlInfo,
)

API_URL = "https://ssl.bing.com/webmaster/api.svc/json/"
BING = Requirements(network=True, credentials=["bing_api_key"])
# UrlWithCrawlIssues.CrawlIssues flags
CRAWL_ISSUES = (
    (1, "Code301"),
    (2, "Code302"),
    (4, "Code4xx"),
    (8, "Code5xx"),
    (16, "BlockedByRobotsTxt"),
    (32, "ContainsMalware"),
    (64, "ImportantUrlBlockedByRobotsTxt"),
    (128, "DnsErrors"),
    (256, "TimeOutErrors"),
)
# ApiErrorCode -> library code
_API_ERRORS = {
    3: ErrorCode.NO_PERMISSION,  # InvalidApiKey
    4: ErrorCode.API_QUOTA_EXCEEDED,  # ThrottleUser
    5: ErrorCode.API_QUOTA_EXCEEDED,  # ThrottleHost
    6: ErrorCode.NO_PERMISSION,  # UserBlocked
    7: ErrorCode.INVALID_INPUT,  # InvalidUrl
    8: ErrorCode.INVALID_INPUT,  # InvalidParameter
    10: ErrorCode.NO_PERMISSION,  # UserNotFound
    11: ErrorCode.PROPERTY_NOT_FOUND,  # NotFound
    13: ErrorCode.NO_PERMISSION,  # NotAllowed
    14: ErrorCode.NO_PERMISSION,  # NotAuthorized
}
_API_REASONS = {3: "invalid-api-key", 4: "throttled", 5: "throttled", 7: "invalid-url", 8: "invalid-parameter"}
_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def bing_date(value: Any) -> str | None:
    """ISO 8601 UTC from ``/Date(ms+zone)/`` (or an ISO string, returned as is)."""
    if not isinstance(value, str) or not value:
        return None
    m = _DATE.fullmatch(value)
    if not m:
        return value
    return datetime.fromtimestamp(int(m.group(1)) / 1000, UTC).isoformat().replace("+00:00", "Z")


def decode_issues(flags: int) -> list[str]:
    return [name for bit, name in CRAWL_ISSUES if flags & bit]


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _api_code(body: Any) -> int | None:
    fault = body.get("d", body) if isinstance(body, dict) else None
    code = fault.get("ErrorCode") if isinstance(fault, dict) else None
    return code if isinstance(code, int) else None


def _fail(ctx: Context, status: int | None, body: Any, error: str) -> NoReturn:
    api = _api_code(body)
    ctx.log.debug("bing api failed: %s %s", error, api)
    details: dict[str, Any] = {"service": "bing"}
    if status is not None:
        details["status"] = status
    if api is not None and api in _API_REASONS:
        details["reason"] = _API_REASONS[api]
    if api is not None and api in _API_ERRORS:
        ctx.fail(_API_ERRORS[api], **details)
    if error == "rate-limited":
        ctx.fail(ErrorCode.API_QUOTA_EXCEEDED, **details)
    if status in (401, 403):
        ctx.fail(ErrorCode.NO_PERMISSION, **details)
    ctx.fail(ErrorCode.API_ERROR, **details)


async def call(session: Any, key: str, method: str, **params: str) -> tuple[int | None, Any, str | None]:
    return await request_json(session, "GET", API_URL + method, params={**params, "apikey": key})


async def _get(ctx: Context, method: str, **params: str) -> Any:
    key = ctx.config.bing_api_key or ""
    status, body, error = await call(ctx.http.api_session(), key, method, **params)
    if error is not None or status != 200:
        _fail(ctx, status, body, error or f"http-{status}")
    return body.get("d") if isinstance(body, dict) else None


def _list(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


@operation("bing.sites", output=BingSites, requires=BING, timeout_s=30)
async def sites(ctx: Context) -> Outcome[BingSites]:
    """Sites of the Bing Webmaster account the key belongs to."""
    rows = _list(await _get(ctx, "GetUserSites"))
    items = [BingSite(url=str(r.get("Url") or ""), is_verified=bool(r.get("IsVerified"))) for r in rows]
    return Outcome(data=BingSites(items=sorted(items, key=lambda s: s.url)))


@operation("bing.query_stats", output=BingQueryStats, requires=BING, timeout_s=30)
async def query_stats(ctx: Context, site: str) -> Outcome[BingQueryStats]:
    """Top queries of the site: impressions, clicks, average positions (weekly data)."""
    rows = _list(await _get(ctx, "GetQueryStats", siteUrl=site))
    items = [
        BingQueryStat(
            query=str(r.get("Query") or ""),
            date=bing_date(r.get("Date")),
            impressions=_int(r.get("Impressions")),
            clicks=_int(r.get("Clicks")),
            avg_impression_position=_float(r.get("AvgImpressionPosition")),
            avg_click_position=_float(r.get("AvgClickPosition")),
        )
        for r in rows
    ]
    items.sort(key=lambda q: (q.date or "", -q.impressions, q.query))
    return Outcome(data=BingQueryStats(site=site, items=items))


@operation("bing.crawl_stats", output=BingCrawlStats, requires=BING, timeout_s=30)
async def crawl_stats(ctx: Context, site: str) -> Outcome[BingCrawlStats]:
    """Daily crawl statistics of the last six months."""
    rows = _list(await _get(ctx, "GetCrawlStats", siteUrl=site))
    items = [
        BingCrawlStat(
            date=bing_date(r.get("Date")),
            crawled_pages=_int(r.get("CrawledPages")),
            crawl_errors=_int(r.get("CrawlErrors")),
            in_index=_int(r.get("InIndex")),
            in_links=_int(r.get("InLinks")),
            code_2xx=_int(r.get("Code2xx")),
            code_301=_int(r.get("Code301")),
            code_302=_int(r.get("Code302")),
            code_4xx=_int(r.get("Code4xx")),
            code_5xx=_int(r.get("Code5xx")),
            all_other_codes=_int(r.get("AllOtherCodes")),
            blocked_by_robots_txt=_int(r.get("BlockedByRobotsTxt")),
            contains_malware=_int(r.get("ContainsMalware")),
        )
        for r in rows
    ]
    items.sort(key=lambda c: c.date or "")
    return Outcome(data=BingCrawlStats(site=site, items=items))


@operation("bing.crawl_issues", output=BingCrawlIssues, requires=BING, timeout_s=30)
async def crawl_issues(ctx: Context, site: str) -> Outcome[BingCrawlIssues]:
    """URLs with crawl issues (redirects, 4xx/5xx, robots.txt blocks, malware, DNS, timeouts)."""
    rows = _list(await _get(ctx, "GetCrawlIssues", siteUrl=site))
    items = []
    for r in rows:
        flags = _int(r.get("Issues"))
        items.append(
            BingCrawlIssue(
                url=str(r.get("Url") or ""),
                http_code=_int(r.get("HttpCode")) or None,
                in_links=_int(r.get("InLinks")),
                issues=decode_issues(flags),
                issue_flags=flags,
            )
        )
    items.sort(key=lambda i: (-i.in_links, i.url))
    findings = []
    serious = [i for i in items if set(i.issues) - {"Code301", "Code302"}]
    if serious:
        top = serious[0]
        findings.append(
            ctx.finding(
                "bing-crawl-issues",
                url=top.url,
                count=len(serious),
                evidence=f"{top.url}: {', '.join(top.issues)} (HTTP {top.http_code}); threshold: any issue "
                "other than a 301/302 redirect",
            )
        )
    return Outcome(data=BingCrawlIssues(site=site, items=items), findings=findings)


@operation("bing.url_info", output=BingUrlInfo, requires=BING, timeout_s=30)
async def url_info(ctx: Context, site: str, url: str) -> Outcome[BingUrlInfo]:
    """Bing's index details of one URL (status, crawl and discovery dates, size, links)."""
    r = await _get(ctx, "GetUrlInfo", siteUrl=site, url=url)
    r = r if isinstance(r, dict) else {}
    return Outcome(
        data=BingUrlInfo(
            site=site,
            url=str(r.get("Url") or url),
            http_status=_int(r.get("HttpStatus")) or None,
            is_page=bool(r.get("IsPage")),
            document_size=_int(r.get("DocumentSize")),
            anchor_count=_int(r.get("AnchorCount")),
            total_child_url_count=_int(r.get("TotalChildUrlCount")),
            discovery_date=bing_date(r.get("DiscoveryDate")),
            last_crawled_date=bing_date(r.get("LastCrawledDate")),
        )
    )


async def access_check(session: Any) -> tuple[bool, str]:
    """``check_access("bing")``: one ``GetUserSites`` call with the configured key."""
    key = session.config.bing_api_key
    if not key:
        return False, "missing"
    status, body, error = await call(session.http.api_session(), key, "GetUserSites")
    if status == 200 and error is None:
        return True, "ok"
    api = _api_code(body)
    if api == 3 or status in (401, 403):
        return False, "rejected"
    if error == "rate-limited" or api in (4, 5):
        return False, "rate-limited"
    if status is None:
        return False, "unreachable"
    return False, error or f"http-{status}"


__all__ = ["crawl_issues", "crawl_stats", "query_stats", "sites", "url_info"]
