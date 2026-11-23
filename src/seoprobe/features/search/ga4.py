"""
Google Analytics 4: ``seoprobe.ga4``.

Read-only (``analytics.readonly`` scope, asked by ``gsc.auth_url(..., services=["ga4"])`` or
``["gsc", "ga4"]`` for one consent covering both). Properties come from the Analytics Admin API
v1beta (``accountSummaries``), reports from the Analytics Data API v1beta (``runReport``).
Tokens are the same as Search Console's: the session's ``token_store``, keyed by ``account``.

Property resolution for ``summary`` is fail-closed like Search Console's: a property counts only
when one of its web data streams has a ``defaultUri`` on the site's host (``www.`` ignored).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from typing import Any, NoReturn

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation

from .models import Ga4LandingPage, Ga4LandingPages, Ga4Properties, Ga4Property, Ga4Summary
from .oauth import GSC, SCOPE_GA4_READ, GoogleClient, GscError, MemoryTokenStore, google_oauth
from .summary import _bare, _host

ADMIN_URL = "https://analyticsadmin.googleapis.com/v1beta"
DATA_URL = "https://analyticsdata.googleapis.com/v1beta"
ORGANIC = "Organic Search"
MAX_PAGES = 10  # accountSummaries pages (200 each)
MAX_PROPERTIES_SCANNED = 50  # data stream lookups of ``summary``
LANDING_LIMIT = 250
SUMMARY_PAGES = 25
METRICS = ("sessions", "engagedSessions", "engagementRate", "keyEvents", "totalRevenue")
_PROPERTY = re.compile(r"(?:properties/)?(\d+)")


def property_name(value: str) -> str | None:
    """``properties/<id>`` from ``123`` or ``properties/123``; None when malformed."""
    m = _PROPERTY.fullmatch(value.strip())
    return f"properties/{m.group(1)}" if m else None


class Ga4Client(GoogleClient):
    async def list_properties(self) -> list[Ga4Property]:
        out: list[Ga4Property] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            url = f"{ADMIN_URL}/accountSummaries?pageSize=200" + (f"&pageToken={token}" if token else "")
            body = await self._call("GET", url)
            body = body if isinstance(body, dict) else {}
            for acc in body.get("accountSummaries") or []:
                if not isinstance(acc, dict):
                    continue
                for p in acc.get("propertySummaries") or []:
                    if isinstance(p, dict) and p.get("property"):
                        out.append(
                            Ga4Property(
                                property=str(p["property"]),
                                display_name=str(p.get("displayName") or ""),
                                account=str(acc.get("account") or ""),
                                account_name=str(acc.get("displayName") or ""),
                            )
                        )
            token = body.get("nextPageToken") or None
            if not token:
                break
        return out

    async def stream_uris(self, prop: str) -> list[str]:
        body = await self._call("GET", f"{ADMIN_URL}/{prop}/dataStreams?pageSize=200")
        out = []
        for s in (body.get("dataStreams") or []) if isinstance(body, dict) else []:
            web = s.get("webStreamData") if isinstance(s, dict) else None
            if isinstance(web, dict) and web.get("defaultUri"):
                out.append(str(web["defaultUri"]))
        return out

    async def landing_pages(
        self, prop: str, *, days: int = 28, channel: str | None = ORGANIC, limit: int = LANDING_LIMIT
    ) -> tuple[list[Ga4LandingPage], dict[str, float], bool]:
        """(rows, totals over all rows, truncated) of the ``landingPage`` report, most sessions first."""
        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=days - 1)
        payload: dict[str, Any] = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "landingPage"}],
            "metrics": [{"name": m} for m in METRICS],
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": limit,
            "metricAggregations": ["TOTAL"],
        }
        if channel:
            payload["dimensionFilter"] = {
                "filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"matchType": "EXACT", "value": channel},
                }
            }
        body = await self._call("POST", f"{DATA_URL}/{prop}:runReport", json_body=payload)
        body = body if isinstance(body, dict) else {}
        rows: list[Ga4LandingPage] = []
        for r in body.get("rows") or []:
            dims = r.get("dimensionValues") if isinstance(r, dict) else None
            vals = r.get("metricValues") if isinstance(r, dict) else None
            if not isinstance(dims, list) or not dims or not isinstance(vals, list):
                continue
            v = [_num((x or {}).get("value")) for x in vals] + [0.0] * len(METRICS)
            rows.append(
                Ga4LandingPage(
                    page=str((dims[0] or {}).get("value") or "/"),
                    sessions=int(v[0]),
                    engaged_sessions=int(v[1]),
                    engagement_rate=v[2],
                    key_events=v[3],
                    revenue=v[4],
                )
            )
        agg = body.get("totals")
        vals = agg[0].get("metricValues") if isinstance(agg, list) and agg and isinstance(agg[0], dict) else None
        if isinstance(vals, list) and len(vals) >= len(METRICS):
            t = [_num((x or {}).get("value")) for x in vals]
            sums = {"sessions": t[0], "engaged_sessions": t[1], "key_events": t[3], "revenue": round(t[4], 2)}
        else:
            sums = totals(rows)
        return rows, sums, int(_num(body.get("rowCount"))) > len(rows)


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def totals(rows: list[Ga4LandingPage]) -> dict[str, float]:
    return {
        "sessions": float(sum(r.sessions for r in rows)),
        "engaged_sessions": float(sum(r.engaged_sessions for r in rows)),
        "key_events": float(sum(r.key_events for r in rows)),
        "revenue": round(sum(r.revenue for r in rows), 2),
    }


async def resolve(client: Ga4Client, url: str, props: list[Ga4Property]) -> tuple[Ga4Property, str] | None:
    """The first property (by id) with a web stream on ``url``'s host, and that stream's URI."""
    bare = _bare(_host(url))
    if not bare:
        return None
    sem = asyncio.Semaphore(3)

    async def streams(p: Ga4Property) -> list[str]:
        async with sem:
            try:
                return await client.stream_uris(p.property)
            except GscError:
                return []

    ordered = sorted(props, key=lambda p: int(p.property.rsplit("/", 1)[-1] or 0))[:MAX_PROPERTIES_SCANNED]
    found = await asyncio.gather(*(streams(p) for p in ordered))
    for p, uris in zip(ordered, found, strict=True):
        for uri in uris:
            if _bare(_host(uri)) == bare:
                return p, uri
    return None


# --------------------------------------------------------------------------- operations

_ERRORS = {
    "not-connected": ErrorCode.NOT_CONNECTED,
    "no-permission": ErrorCode.NO_PERMISSION,
    "property-not-found": ErrorCode.PROPERTY_NOT_FOUND,
    "rate-limited": ErrorCode.API_QUOTA_EXCEEDED,
    "not-configured": ErrorCode.MISSING_CREDENTIAL,
}


def _fail(ctx: Context, code: str, **details: Any) -> NoReturn:
    err = _ERRORS.get(code, ErrorCode.API_ERROR)
    ctx.log.debug("analytics call failed: %s", code)
    if err is ErrorCode.MISSING_CREDENTIAL:
        details["missing"] = "credential:google_app"
    elif code.startswith("http-") and code[5:].isdigit():
        details["status"] = code[5:]
    ctx.fail(err, service="ga4", **details)


async def client(ctx: Context, account: str) -> Ga4Client:
    """A connected client with the Analytics scope (fails the operation otherwise)."""
    c = Ga4Client(google_oauth(ctx), account)
    state = await c.oauth.status(account)
    if not state["configured"]:
        _fail(ctx, "not-configured")
    if not state["connected"]:
        _fail(ctx, "not-connected")
    if not await c.oauth.has_scope(account, SCOPE_GA4_READ):
        ctx.fail(ErrorCode.NO_PERMISSION, service="ga4", reason="scope-not-granted")
    return c


def _property(ctx: Context, value: str) -> str:
    name = property_name(value)
    if name is None:
        ctx.fail(ErrorCode.INVALID_INPUT, service="ga4", reason="invalid-property")
    return name


@operation("ga4.properties", output=Ga4Properties, requires=GSC)
async def properties(ctx: Context, *, account: str = "default") -> Outcome[Ga4Properties]:
    """GA4 properties the connected account can read."""
    try:
        items = await (await client(ctx, account)).list_properties()
    except GscError as exc:
        _fail(ctx, exc.code, **({"reason": exc.reason} if exc.reason else {}))
    return Outcome(data=Ga4Properties(items=items))


@operation("ga4.landing_pages", output=Ga4LandingPages, requires=GSC)
async def landing_pages(
    ctx: Context,
    property: str,
    *,
    days: int = 28,
    channel: str | None = ORGANIC,
    limit: int = LANDING_LIMIT,
    account: str = "default",
) -> Outcome[Ga4LandingPages]:
    """Sessions, engagement, key events and revenue per landing page (``channel=None``: all)."""
    prop = _property(ctx, property)
    try:
        rows, sums, truncated = await (await client(ctx, account)).landing_pages(
            prop, days=days, channel=channel, limit=limit
        )
    except GscError as exc:
        _fail(ctx, exc.code, **({"reason": exc.reason} if exc.reason else {}))
    return Outcome(
        data=Ga4LandingPages(property=prop, days=days, channel=channel, items=rows, totals=sums, truncated=truncated)
    )


@operation("ga4.summary", output=Ga4Summary, requires=GSC, timeout_s=60)
async def summary(ctx: Context, url: str, *, days: int = 28, account: str = "default") -> Outcome[Ga4Summary]:
    """
    Organic landing pages of the GA4 property serving ``url``. Analytics states (not connected,
    no property, no scope, ...) are in ``Ga4Summary.status``, never an error result.
    """
    store = ctx.session.token_store or MemoryTokenStore()
    c = Ga4Client(google_oauth(ctx, store), account)
    state = await c.oauth.status(account)
    if not state["configured"]:
        return Outcome(data=Ga4Summary(status="not-configured", days=days))
    if not state["connected"]:
        return Outcome(data=Ga4Summary(status="not-connected", days=days))
    email = state.get("email")
    if not await c.oauth.has_scope(account, SCOPE_GA4_READ):
        return Outcome(data=Ga4Summary(status="no-permission", account=email, days=days, error="scope-not-granted"))
    try:
        found = await resolve(c, url, await c.list_properties())
        if found is None:
            return Outcome(data=Ga4Summary(status="property-not-found", account=email, days=days))
        prop, uri = found
        rows, sums, _ = await c.landing_pages(prop.property, days=days)
    except GscError as exc:
        status = "not-connected" if exc.code == "not-connected" else "error"
        return Outcome(data=Ga4Summary(status=status, account=email, days=days, error=exc.label))  # type: ignore[arg-type]
    return Outcome(
        data=Ga4Summary(
            status="ok",
            account=email,
            property=prop.property,
            display_name=prop.display_name,
            stream_uri=uri,
            days=days,
            totals=sums,
            landing_pages=rows[:SUMMARY_PAGES],
        )
    )


__all__ = ["landing_pages", "properties", "summary"]
