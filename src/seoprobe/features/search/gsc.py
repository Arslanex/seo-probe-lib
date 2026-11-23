"""
Google Search Console: ``seoprobe.gsc``.

The API client (``GscClient``) and the operations. Every operation needs ``Config.google_app``
and an account connected through ``auth_url`` / ``exchange_code`` (tokens in the session's
``token_store``, keyed by ``account``).

Quotas worth knowing: Search Analytics data lags 2-3 days; the URL Inspection API allows
2000 queries/day/property (600/min), so callers must keep their use small.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable
from typing import Any, NoReturn, TypeVar
from urllib.parse import quote

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation

from . import ga4
from . import opportunities as opp
from .models import (
    GscAnalytics,
    GscAnalyticsRow,
    GscData,
    GscInspection,
    GscProperties,
    GscProperty,
    GscSitemaps,
    KeywordRankings,
    Opportunities,
    OpportunityThresholds,
    TopQueries,
    TopQuery,
)
from .oauth import GSC, SCOPE_GA4_READ, GoogleClient, GscError, MemoryTokenStore, google_oauth
from .oauth import auth_url as auth_url
from .oauth import connection as connection
from .oauth import disconnect as disconnect
from .oauth import exchange_code as exchange_code
from .summary import _inspection, _num, _sitemap, collect_gsc, collect_rankings, findings, resolve_property

T = TypeVar("T")
SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
DATA_LAG_DAYS = 3
SUMMARY_TIMEOUT_S = 100.0  # collect_gsc stops itself after 90 s


def _quote(prop: str) -> str:
    return quote(prop, safe="")


class GscClient(GoogleClient):
    async def list_properties(self) -> list[dict[str, str]]:
        body = await self._call("GET", SITES_URL)
        out: list[dict[str, str]] = []
        for e in body.get("siteEntry", []) if isinstance(body, dict) else []:
            if isinstance(e, dict) and e.get("siteUrl"):
                out.append({"site_url": str(e["siteUrl"]), "permission": str(e.get("permissionLevel", ""))})
        return out

    async def search_analytics(
        self,
        property: str,
        *,
        days: int = 28,
        dimensions: list[str] | None = None,
        row_limit: int = 25,
        query: str | None = None,
        query_operator: str = "contains",
        offset_days: int = 0,
    ) -> list[dict[str, Any]]:
        """Rows of the ``days`` ending ``DATA_LAG_DAYS + offset_days`` days ago."""
        end = dt.date.today() - dt.timedelta(days=DATA_LAG_DAYS + offset_days)
        start = end - dt.timedelta(days=days - 1)
        payload: dict[str, Any] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "rowLimit": row_limit,
        }
        if dimensions:
            payload["dimensions"] = dimensions
        if query is not None:  # case-insensitive on Google's side
            payload["dimensionFilterGroups"] = [
                {
                    "groupType": "and",
                    "filters": [{"dimension": "query", "operator": query_operator, "expression": query}],
                }
            ]
        body = await self._call("POST", f"{SITES_URL}/{_quote(property)}/searchAnalytics/query", json_body=payload)
        rows = body.get("rows", []) if isinstance(body, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    async def inspect_url(self, property: str, url: str) -> dict[str, Any]:
        body = await self._call("POST", INSPECT_URL, json_body={"inspectionUrl": url, "siteUrl": property})
        result = body.get("inspectionResult", {}) if isinstance(body, dict) else {}
        return result if isinstance(result, dict) else {}

    async def list_sitemaps(self, property: str) -> list[dict[str, Any]]:
        body = await self._call("GET", f"{SITES_URL}/{_quote(property)}/sitemaps")
        items = body.get("sitemap", []) if isinstance(body, dict) else []
        return [s for s in items if isinstance(s, dict)]


# --------------------------------------------------------------------------- operations

_GSC_ERRORS = {
    "not-connected": ErrorCode.NOT_CONNECTED,
    "no-permission": ErrorCode.NO_PERMISSION,
    "property-not-found": ErrorCode.PROPERTY_NOT_FOUND,
    "rate-limited": ErrorCode.API_QUOTA_EXCEEDED,
    "not-configured": ErrorCode.MISSING_CREDENTIAL,
}


def _fail(ctx: Context, code: str, **details: Any) -> NoReturn:
    err = _GSC_ERRORS.get(code, ErrorCode.API_ERROR)
    ctx.log.debug("search console call failed: %s", code)
    if err is ErrorCode.MISSING_CREDENTIAL:
        details["missing"] = "credential:google_app"
    elif code.startswith("http-") and code[5:].isdigit():
        details["status"] = code[5:]
    ctx.fail(err, service="gsc", **details)


async def _call(ctx: Context, call: Awaitable[T]) -> T:
    try:
        return await call
    except GscError as exc:
        _fail(ctx, exc.code, **({"reason": exc.reason} if exc.reason else {}))


async def _client(ctx: Context, account: str) -> GscClient:
    """
    A client for a connected account. Fails first with ``missing-credential`` when the Google
    app is not configured, then with ``not-connected``: every operation answers the same way.
    """
    client = GscClient(google_oauth(ctx), account)
    await _connected(ctx, client)
    return client


async def _connected(ctx: Context, client: GscClient) -> None:
    state = await client.oauth.status(client.account)
    if not state["configured"]:
        _fail(ctx, "not-configured")
    if not state["connected"]:
        _fail(ctx, "not-connected")


async def _resolve(ctx: Context, client: GscClient, url: str) -> GscProperty:
    res = resolve_property(url, await _call(ctx, client.list_properties()))
    if res.property is None:
        _fail(ctx, "no-permission" if res.reason == "no-permission" else "property-not-found", url=url)
    return GscProperty(site_url=res.property, permission=res.permission or "")


@operation("gsc.properties", output=GscProperties, requires=GSC)
async def properties(ctx: Context, *, account: str = "default") -> Outcome[GscProperties]:
    """Search Console properties the connected account can access."""
    props = await _call(ctx, (await _client(ctx, account)).list_properties())
    return Outcome(data=GscProperties(items=[GscProperty(**p) for p in props]))


@operation("gsc.find_property", output=GscProperty, requires=GSC)
async def find_property(ctx: Context, url: str, *, account: str = "default") -> Outcome[GscProperty]:
    """The property that covers ``url`` (fail-closed: never another site's property)."""
    return Outcome(data=await _resolve(ctx, await _client(ctx, account), url))


@operation("gsc.analytics", output=GscAnalytics, requires=GSC)
async def analytics(
    ctx: Context,
    property: str,
    *,
    days: int = 28,
    dimensions: list[str] | None = None,
    row_limit: int = 25,
    query: str | None = None,
    query_operator: str = "contains",
    account: str = "default",
) -> Outcome[GscAnalytics]:
    """Clicks, impressions, CTR and position (Search Analytics)."""
    rows = await _call(
        ctx,
        (await _client(ctx, account)).search_analytics(
            property,
            days=days,
            dimensions=dimensions,
            row_limit=row_limit,
            query=query,
            query_operator=query_operator,
        ),
    )
    items = [
        GscAnalyticsRow(
            keys=[str(k) for k in r.get("keys") or []],
            clicks=int(_num(r.get("clicks"))),
            impressions=int(_num(r.get("impressions"))),
            ctr=_num(r.get("ctr")),
            position=_num(r.get("position")),
        )
        for r in rows
    ]
    return Outcome(data=GscAnalytics(property=property, days=days, dimensions=dimensions or [], items=items))


@operation("gsc.inspect", output=GscInspection, requires=GSC)
async def inspect(ctx: Context, property: str, url: str, *, account: str = "default") -> Outcome[GscInspection]:
    """URL Inspection: Google's own view of one page."""
    result = await _call(ctx, (await _client(ctx, account)).inspect_url(property, url))
    return Outcome(data=_inspection(url, result))


@operation("gsc.sitemaps", output=GscSitemaps, requires=GSC)
async def sitemaps(ctx: Context, property: str, *, account: str = "default") -> Outcome[GscSitemaps]:
    """Sitemaps submitted to the property."""
    items = await _call(ctx, (await _client(ctx, account)).list_sitemaps(property))
    return Outcome(data=GscSitemaps(items=[_sitemap(s) for s in items]))


@operation("gsc.rankings", output=KeywordRankings, requires=GSC)
async def rankings(
    ctx: Context, property: str, *, keywords: list[str], account: str = "default"
) -> Outcome[KeywordRankings]:
    """Keyword -> position and page, plus opportunities from the property's own queries."""
    client = await _client(ctx, account)
    return Outcome(data=await collect_rankings(client, property, keywords, 28))


@operation("gsc.top_queries", output=TopQueries, requires=GSC)
async def top_queries(ctx: Context, url: str, *, account: str = "default") -> Outcome[TopQueries]:
    """The site's most shown real Search Console queries of the last 28 days."""
    client = await _client(ctx, account)
    prop = await _resolve(ctx, client, url)
    rows = await _call(ctx, client.search_analytics(prop.site_url, days=28, dimensions=["query"], row_limit=12))
    found = [
        TopQuery(
            query=str(r["keys"][0]).strip(" \"'")[:80],
            impressions=int(r.get("impressions", 0)),
            clicks=int(r.get("clicks", 0)),
        )
        for r in rows
        if r.get("keys")
    ]
    return Outcome(data=TopQueries(property=prop.site_url, status="ok" if found else "empty", items=found))


@operation("gsc.opportunities", output=Opportunities, requires=GSC, timeout_s=90)
async def opportunities(
    ctx: Context,
    property: str,
    *,
    days: int = 28,
    compare_days: int = 28,
    min_impressions: int | None = None,
    striking_min_position: float | None = None,
    striking_max_position: float | None = None,
    low_ctr_ratio: float | None = None,
    cannibal_min_share: float | None = None,
    decline_min_ratio: float | None = None,
    decline_min_drop_clicks: float | None = None,
    decline_min_previous_clicks: float | None = None,
    ga4_property: str | None = None,
    account: str = "default",
) -> Outcome[Opportunities]:
    """
    Striking-distance queries, low CTR for the position, cannibalization and declining pages.

    Thresholds left at ``None`` use ``opportunities.json``. With ``ga4_property`` every list is
    sorted by the landing page's GA4 revenue, then key events (organic, same ``days``); a GA4
    failure keeps the search order and sets ``ga4_error`` (partial result).
    """
    given = {
        "min_impressions": min_impressions,
        "striking_min_position": striking_min_position,
        "striking_max_position": striking_max_position,
        "low_ctr_ratio": low_ctr_ratio,
        "cannibal_min_share": cannibal_min_share,
        "decline_min_ratio": decline_min_ratio,
        "decline_min_drop_clicks": decline_min_drop_clicks,
        "decline_min_previous_clicks": decline_min_previous_clicks,
    }
    t = OpportunityThresholds(**{**opp.default_thresholds(), **{k: v for k, v in given.items() if v is not None}})
    client = await _client(ctx, account)
    pairs = await _call(
        ctx, client.search_analytics(property, days=days, dimensions=["query", "page"], row_limit=t.row_limit)
    )
    current = await _call(ctx, client.search_analytics(property, days=days, dimensions=["page"], row_limit=t.row_limit))
    previous = await _call(
        ctx,
        client.search_analytics(
            property, days=compare_days, dimensions=["page"], row_limit=t.row_limit, offset_days=days
        ),
    )
    data = opp.analyse(property, pairs, current, previous, days=days, compare_days=compare_days, thresholds=t)
    partial = False
    if ga4_property is not None:
        data.ga4_property = ga4.property_name(ga4_property)
        if data.ga4_property is None:
            ctx.fail(ErrorCode.INVALID_INPUT, service="ga4", reason="invalid-property")
        try:
            if not await client.oauth.has_scope(account, SCOPE_GA4_READ):
                raise GscError("scope-not-granted")
            rows, _sums, _more = await ga4.Ga4Client(client.oauth, account).landing_pages(data.ga4_property, days=days)
        except GscError as exc:
            data.ga4_error, partial = exc.label, True
        else:
            data = opp.rank_by_value(data, rows)
    return Outcome(data=data, findings=opp.findings(data, ctx.finding), partial=partial)


@operation("gsc.summary", output=GscData, requires=GSC, timeout_s=SUMMARY_TIMEOUT_S)
async def summary(
    ctx: Context,
    url: str,
    *,
    sample_urls: list[str] | None = None,
    keywords: list[str] | None = None,
    inspect_limit: int | None = None,
    account: str = "default",
) -> Outcome[GscData]:
    """
    Everything above for one site plus the Search Console rules. Search Console states
    (not connected, no property, ...) are in ``GscData.status``, never an error result.
    """
    store = ctx.session.token_store or MemoryTokenStore()  # no store: "not-connected"
    client = GscClient(google_oauth(ctx, store), account)
    data = await collect_gsc(
        client,
        entry_url=url,
        sample_urls=sample_urls or [url],
        inspect_limit=inspect_limit or ctx.config.gsc_inspect_limit,
        keywords=keywords,
    )
    out = findings(data)
    return Outcome(data=data, findings=out.findings, not_checked=out.not_checked)


__all__ = [
    "analytics",
    "opportunities",
    "auth_url",
    "connection",
    "disconnect",
    "exchange_code",
    "find_property",
    "inspect",
    "properties",
    "rankings",
    "sitemaps",
    "summary",
    "top_queries",
]
