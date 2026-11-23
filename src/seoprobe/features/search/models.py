"""Output models of the search feature."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Keyword ranking limits (used by the rankings analysis and by the report)
MAX_KEYWORDS = 10
LIST_LIMIT = 15
MIN_IMPRESSIONS = 50
LOW_CTR_TOP3 = 0.05
LOW_CTR_TOP10 = 0.015


class GscRow(BaseModel):
    key: str  # query or page URL
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0


class GscInspection(BaseModel):
    """URL Inspection result of one page (Google's own view of it)."""

    url: str
    verdict: str | None = None  # PASS | NEUTRAL | FAIL | VERDICT_UNSPECIFIED
    coverage_state: str | None = None  # e.g. "Submitted and indexed", "Discovered - currently not indexed"
    indexing_state: str | None = None
    robots_txt_state: str | None = None
    page_fetch_state: str | None = None
    google_canonical: str | None = None
    user_canonical: str | None = None
    last_crawl_time: str | None = None
    crawled_as: str | None = None
    indexed: bool | None = None
    error: str | None = None


class GscSitemap(BaseModel):
    path: str
    last_submitted: str | None = None
    last_downloaded: str | None = None
    is_pending: bool = False
    errors: int = 0
    warnings: int = 0


class KeywordRankRow(BaseModel):
    """What Search Console reports for one keyword the user asked about (last ``period_days``)."""

    keyword: str
    matched_query: str | None = None  # set when no exact query matched and a containing one was used
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    position: float | None = None
    top_page: str | None = None  # the URL with the most impressions for this query
    status: Literal["ok", "no-data", "error"] = "no-data"
    error: str | None = None  # stable code (rate-limited, http-500, ...) when status == "error"


class RankingQuery(BaseModel):
    """A query from the property's own Search Console data that deserves attention."""

    query: str
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    position: float = 0.0


class KeywordRankings(BaseModel):
    """
    Rankings of the user's keywords plus opportunities from the site's own queries.

    Informational: never part of the score. Search Console hides rare queries, so ``no-data`` does
    not mean "not ranking".
    """

    period_days: int = 28
    keywords_requested: int = 0
    rows: list[KeywordRankRow] = Field(default_factory=list)
    opportunities: list[RankingQuery] = Field(default_factory=list)
    low_ctr: list[RankingQuery] = Field(default_factory=list)
    error: str | None = None  # the opportunity query failed (keyword rows may still be there)


class GscData(BaseModel):
    """Search Console data for the audited site (only with the owner's authorisation)."""

    status: Literal["ok", "not-connected", "not-configured", "property-not-found", "no-permission", "error"] = (
        "not-connected"
    )
    account: str | None = None
    property: str | None = None  # sc-domain:example.com | https://example.com/
    permission: str | None = None  # siteOwner | siteFullUser | siteRestrictedUser | siteUnverifiedUser
    period_days: int = 28
    totals: dict[str, float] = Field(default_factory=dict)  # clicks, impressions, ctr, position
    top_queries: list[GscRow] = Field(default_factory=list)
    top_pages: list[GscRow] = Field(default_factory=list)
    inspections: list[GscInspection] = Field(default_factory=list)
    sitemaps: list[GscSitemap] = Field(default_factory=list)
    rankings: KeywordRankings | None = None  # absent in audits stored before keyword rankings
    error: str | None = None


class AuthRequest(BaseModel):
    """One pending Google authorisation. The caller stores it and hands it to ``exchange_code``."""

    url: str  # where to send the user
    state: str
    verifier: str = Field(repr=False)  # PKCE code verifier: keep it server-side
    redirect_uri: str
    account: str = "default"
    created_at: datetime
    consumed: bool = False  # set by exchange_code: the request is single use
    scopes: list[str] = Field(default_factory=list)  # requested service scopes (empty: Search Console)


class GscConnection(BaseModel):
    account: str = "default"
    configured: bool = False
    connected: bool = False
    email: str | None = None


class GscProperty(BaseModel):
    site_url: str
    permission: str = ""


class GscProperties(BaseModel):
    items: list[GscProperty] = Field(default_factory=list)


class GscAnalyticsRow(BaseModel):
    keys: list[str] = Field(default_factory=list)  # one value per requested dimension
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0


class GscAnalytics(BaseModel):
    property: str
    days: int = 28
    dimensions: list[str] = Field(default_factory=list)
    items: list[GscAnalyticsRow] = Field(default_factory=list)


class GscSitemaps(BaseModel):
    items: list[GscSitemap] = Field(default_factory=list)


class TopQuery(BaseModel):
    query: str
    impressions: int = 0
    clicks: int = 0


class TopQueries(BaseModel):
    """The site's own most shown Search Console queries (last 28 days), never invented."""

    property: str
    status: Literal["ok", "empty"] = "empty"
    items: list[TopQuery] = Field(default_factory=list)


# --------------------------------------------------------------------------- 0.2: opportunities


class OpportunityThresholds(BaseModel):
    """The thresholds an ``opportunities`` run used (defaults in ``opportunities.json``)."""

    min_impressions: int
    striking_min_position: float
    striking_max_position: float
    low_ctr_ratio: float  # CTR below expected_ctr * ratio is low
    cannibal_min_share: float  # a page holds at least this share of the query's impressions
    cannibal_min_pages: int
    decline_min_previous_clicks: float
    decline_min_drop_clicks: float
    decline_min_ratio: float
    row_limit: int
    list_limit: int


class OpportunityPage(BaseModel):
    page: str
    impressions: int = 0
    clicks: int = 0
    position: float = 0.0
    share: float = 0.0  # of the query's impressions


class OpportunityRow(BaseModel):
    query: str | None = None  # None in ``declining`` (page level)
    page: str | None = None
    position: float = 0.0
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    expected_ctr: float | None = None  # low_ctr: from the CTR curve at this position
    missed_clicks: int | None = None  # low_ctr: (expected_ctr - ctr) * impressions
    previous_clicks: float | None = None  # declining: previous period, scaled to ``days``
    change: float | None = None  # declining: clicks - previous_clicks
    change_ratio: float | None = None  # declining: change / previous_clicks
    pages: list[OpportunityPage] = Field(default_factory=list)  # cannibalization
    ga4_key_events: float | None = None
    ga4_revenue: float | None = None


class Opportunities(BaseModel):
    """Search Console opportunities of one property. Informational: never part of the score."""

    property: str
    days: int = 28
    compare_days: int = 28
    thresholds: OpportunityThresholds
    ctr_curve_source: str = ""
    striking: list[OpportunityRow] = Field(default_factory=list)
    low_ctr: list[OpportunityRow] = Field(default_factory=list)
    cannibalization: list[OpportunityRow] = Field(default_factory=list)
    declining: list[OpportunityRow] = Field(default_factory=list)
    rows_analysed: int = 0
    truncated: bool = False  # a request hit ``row_limit``: rarer rows are missing
    sorted_by: Literal["search", "ga4-value"] = "search"
    ga4_property: str | None = None
    ga4_error: str | None = None


# --------------------------------------------------------------------------- 0.2: Google Analytics 4


class Ga4Property(BaseModel):
    property: str  # properties/123
    display_name: str = ""
    account: str = ""  # accounts/9
    account_name: str = ""


class Ga4Properties(BaseModel):
    items: list[Ga4Property] = Field(default_factory=list)


class Ga4LandingPage(BaseModel):
    page: str  # landing page path
    sessions: int = 0
    engaged_sessions: int = 0
    engagement_rate: float = 0.0
    key_events: float = 0.0
    revenue: float = 0.0


class Ga4LandingPages(BaseModel):
    property: str
    days: int = 28
    channel: str | None = "Organic Search"
    items: list[Ga4LandingPage] = Field(default_factory=list)
    totals: dict[str, float] = Field(default_factory=dict)  # sessions, engaged_sessions, key_events, revenue
    truncated: bool = False


class Ga4Summary(BaseModel):
    """GA4 organic landing pages of the property that serves a site (owner's authorisation)."""

    status: Literal["ok", "not-connected", "not-configured", "property-not-found", "no-permission", "error"] = (
        "not-connected"
    )
    account: str | None = None
    property: str | None = None
    display_name: str | None = None
    stream_uri: str | None = None
    days: int = 28
    channel: str | None = "Organic Search"
    totals: dict[str, float] = Field(default_factory=dict)
    landing_pages: list[Ga4LandingPage] = Field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------- 0.2: Bing Webmaster


class BingSite(BaseModel):
    url: str
    is_verified: bool = False


class BingSites(BaseModel):
    items: list[BingSite] = Field(default_factory=list)


class BingQueryStat(BaseModel):
    query: str
    date: str | None = None  # ISO 8601 UTC
    impressions: int = 0
    clicks: int = 0
    avg_impression_position: float | None = None
    avg_click_position: float | None = None


class BingQueryStats(BaseModel):
    site: str
    items: list[BingQueryStat] = Field(default_factory=list)


class BingCrawlStat(BaseModel):
    date: str | None = None
    crawled_pages: int = 0
    crawl_errors: int = 0
    in_index: int = 0
    in_links: int = 0
    code_2xx: int = 0
    code_301: int = 0
    code_302: int = 0
    code_4xx: int = 0
    code_5xx: int = 0
    all_other_codes: int = 0
    blocked_by_robots_txt: int = 0
    contains_malware: int = 0


class BingCrawlStats(BaseModel):
    site: str
    items: list[BingCrawlStat] = Field(default_factory=list)


class BingCrawlIssue(BaseModel):
    url: str
    http_code: int | None = None
    in_links: int = 0
    issues: list[str] = Field(default_factory=list)  # decoded CrawlIssues flags, e.g. ["Code4xx"]
    issue_flags: int = 0


class BingCrawlIssues(BaseModel):
    site: str
    items: list[BingCrawlIssue] = Field(default_factory=list)


class BingUrlInfo(BaseModel):
    site: str
    url: str
    http_status: int | None = None
    is_page: bool = False
    document_size: int = 0
    anchor_count: int = 0
    total_child_url_count: int = 0
    discovery_date: str | None = None
    last_crawled_date: str | None = None


# --------------------------------------------------------------------------- 0.2: IndexNow


class IndexNowKeyFile(BaseModel):
    """The file to publish at ``https://<host><path>`` before submitting."""

    key: str
    path: str  # /<key>.txt
    content: str
    content_type: str = "text/plain; charset=utf-8"


class IndexNowVerification(BaseModel):
    host: str
    key_url: str
    status: int | None = None
    found: bool = False  # 200 answer
    matches: bool = False  # body (trimmed) equals the key
    content_type: str | None = None


class IndexNowBatch(BaseModel):
    index: int
    size: int
    status: int | None = None
    result: str  # ok | accepted | bad-request | key-not-valid | unprocessable | rate-limited | error
    accepted: bool = False
    error: str | None = None


class IndexNowSubmission(BaseModel):
    host: str
    key_location: str | None = None
    submitted: int = 0  # URLs sent
    accepted: int = 0  # URLs in batches answered 200/202
    batches: list[IndexNowBatch] = Field(default_factory=list)
    rejected_urls: list[str] = Field(default_factory=list)  # not on ``host`` or not http(s) (first 100)
    rejected_count: int = 0
    duplicates: int = 0
    skipped: int = 0  # over ``Config.max_batch_urls``, not sent
