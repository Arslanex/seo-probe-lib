"""Output models of the monitoring feature (the JSON contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from seoprobe.core.models import Hop, Severity

# --- check_urls


class UrlCheck(BaseModel):
    url: str  # as given
    final_url: str | None = None
    status: int | None = None
    error: str | None = None  # client error code ("unreachable", "invalid-url", ...)
    redirect_chain: list[Hop] = Field(default_factory=list)
    hops: int = 0
    canonical: str | None = None
    canonical_elsewhere: bool = False  # the canonical names another page
    noindex_meta: bool = False
    noindex_header: bool = False
    x_robots_tag: str | None = None
    robots_allowed: bool | None = None  # None: not checked or robots.txt unreachable
    in_sitemap: bool | None = None  # None: no sitemap URLs given
    indexable: bool = False
    reasons: list[str] = Field(default_factory=list)  # why not indexable (stable codes)


class UrlChecks(BaseModel):
    items: list[UrlCheck] = Field(default_factory=list)
    checked: int = 0
    skipped: int = 0  # over Config.max_batch_urls, not processed
    duplicates: int = 0  # repeated input URLs, checked once
    user_agent: str = "Googlebot"  # robots.txt token judged
    robots_checked: bool = True
    sitemap_checked: bool = False
    concurrency: int = 0


# --- check_redirects


class RedirectCheck(BaseModel):
    old: str
    expected: str
    chain: list[Hop] = Field(default_factory=list)  # redirect responses, in order
    statuses: list[int] = Field(default_factory=list)  # every status seen, final one last
    final_url: str | None = None
    final_status: int | None = None
    error: str | None = None
    hops: int = 0
    matches_expected: bool = False
    loop: bool = False
    issues: list[str] = Field(default_factory=list)  # not-redirected | wrong-target | chain-too-long | ...


class RedirectChecks(BaseModel):
    items: list[RedirectCheck] = Field(default_factory=list)
    checked: int = 0
    skipped: int = 0  # over Config.max_batch_urls, not processed
    max_hops: int = 1
    temporary_statuses: list[int] = Field(default_factory=list)
    concurrency: int = 0


# --- snapshot / diff


class HreflangPair(BaseModel):
    lang: str
    href: str


class Snapshot(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    error: str | None = None
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    robots_meta: list[str] = Field(default_factory=list)  # content of robots/googlebot meta tags
    x_robots_tag: str = ""
    noindex: bool = False  # meta or header, for everyone or Googlebot
    h1: list[str] = Field(default_factory=list)
    hreflang: list[HreflangPair] = Field(default_factory=list)  # sorted
    schema_types: list[str] = Field(default_factory=list)  # sorted, unique
    content_hash: str = ""  # sha256 of the normalised visible text ("" when nothing was parsed)
    word_count: int = 0
    internal_links: int = 0
    taken_at: datetime | None = None


class FieldChange(BaseModel):
    field: str
    old: Any = None
    new: Any = None
    severity: Severity
    rule: str  # rule id of the finding raised for this change


class SnapshotDiff(BaseModel):
    url: str
    old_taken_at: datetime | None = None
    new_taken_at: datetime | None = None
    changed: bool = False
    changes: list[FieldChange] = Field(default_factory=list)  # most severe first
    max_severity: Severity | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)


# --- analyze_logs


class PathCount(BaseModel):
    path: str
    requests: int


class WastedCrawl(BaseModel):
    parameterised: int = 0  # URL with a query string
    redirect: int = 0  # 3xx
    client_error: int = 0  # 4xx
    total: int = 0  # requests with at least one of the above
    share: float = 0.0  # total / requests


class BotStats(BaseModel):
    name: str
    kind: str  # search | ai
    vendor: str = ""
    verifiable: bool = False  # the catalogue lists reverse-DNS domains for it
    requests: int = 0
    verified_requests: int = 0
    spoofed_requests: int = 0
    unverified_requests: int = 0  # not verifiable, verification off, or over the IP cap
    distinct_ips: int = 0
    spoofed_ips: list[str] = Field(default_factory=list)  # first few, sorted
    status: dict[str, int] = Field(default_factory=dict)  # "200" -> requests, sorted by code
    top_paths: list[PathCount] = Field(default_factory=list)
    paths_truncated: bool = False  # more distinct paths than tracked
    wasted: WastedCrawl = Field(default_factory=WastedCrawl)


class LogAnalysis(BaseModel):
    format: str
    lines_total: int = 0
    lines_parsed: int = 0
    lines_unparsable: int = 0
    bot_requests: int = 0
    other_requests: int = 0
    bots: list[BotStats] = Field(default_factory=list)  # most requests first
    verify_bots: bool = True
    ips_verified: int = 0
    ips_not_verified: int = 0  # over the verification cap
    ips_truncated: bool = False  # more (bot, IP) pairs than tracked
    urls_checked: int = 0
    urls_skipped: int = 0  # over Config.max_batch_urls
    uncrawled_urls: list[str] = Field(default_factory=list)  # never fetched by a verified search bot
    thresholds: dict[str, float] = Field(default_factory=dict)
