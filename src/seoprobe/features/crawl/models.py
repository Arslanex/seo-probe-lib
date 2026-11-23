"""Output models of the crawl feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from seoprobe.core.page import Page

BLOCKED_STATUSES = frozenset({401, 403, 429, 999})

Verdict = Literal["ok", "blocked", "broken", "rejected", "unchecked"]


def classify_link(status: int | None, error: str | None) -> Verdict:
    """Link-health verdict of one target (used by the link analysis and by the report)."""
    if error == "target-rejected":
        return "rejected"
    if error == "circuit-open":
        return "unchecked"
    if error:  # unreachable, timeout, TLS failure, redirect loop, fetch-error ...
        return "broken"
    if status is None:
        return "unchecked"
    if status in BLOCKED_STATUSES:
        return "blocked"
    if status >= 400:
        return "broken"
    return "ok"


class LinkRecord(BaseModel):
    source_url: str
    target_url: str  # normalised
    anchor: str = ""  # <= 120 chars
    kind: Literal["internal", "external"]
    rel: list[str] = Field(default_factory=list)  # nofollow, sponsored, ugc, ...
    zone: Literal["nav", "footer", "main", "other"] = "other"
    target_status: int | None = None
    final_url: str | None = None
    redirect_hops: int = 0
    error: str | None = None  # "timeout" | "unreachable" | "target-rejected" | ...
    checked: bool = False  # True when a status was obtained (sample or HEAD/GET)


class PageLinkStats(BaseModel):
    url: str
    outlinks_internal: int = 0
    outlinks_external: int = 0
    inlinks: int = 0  # from sampled pages only
    depth: int | None = None  # clicks from the entry page (BFS over the sampled graph)
    in_sitemap: bool | None = None


class LinkSummary(BaseModel):
    internal_total: int = 0
    external_total: int = 0
    internal_unique: int = 0
    external_unique: int = 0
    external_domains: list[dict[str, Any]] = Field(default_factory=list)  # [{domain, count}]
    checked: int = 0
    skipped: int = 0  # not checked because of the per-audit limit
    skipped_time_budget: int = 0  # of those: not answered before the link-check time budget ran out
    broken: int = 0
    redirecting: int = 0
    nofollow: int = 0
    max_depth: int | None = None
    orphans: list[str] = Field(default_factory=list)
    pages: list[PageLinkStats] = Field(default_factory=list)
    links: list[LinkRecord] = Field(default_factory=list)  # capped (<= 2000)
    truncated: bool = False


class IndexabilityRow(BaseModel):
    url: str
    status: int | None = None
    indexable: bool = True
    reasons: list[str] = Field(
        default_factory=list
    )  # noindex, noindex-header, canonical-elsewhere, blocked-by-robots, status-4xx, status-5xx
    canonical: str | None = None
    in_sitemap: bool | None = None
    inlinks: int | None = None
    depth: int | None = None


# --- from hreflang.py


class HreflangEntry(BaseModel):
    lang: str
    href: str
    source: str = "html"  # html | header
    valid: bool = True


class HreflangPageRow(BaseModel):
    url: str
    html_lang: str = ""
    canonical: str | None = None
    entries: list[HreflangEntry] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)  # stable rule ids


class HreflangReport(BaseModel):
    applicable: bool = False
    multilingual_signals: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    has_x_default: bool = False
    pages: list[HreflangPageRow] = Field(default_factory=list)
    probed: list[str] = Field(default_factory=list)  # targets fetched outside the sample
    unchecked: int = 0  # return links not verifiable (probe limit, fetch failure)
    notes: list[str] = Field(default_factory=list)


# --- operation outputs


class CrawlReport(BaseModel):
    entry_url: str
    pages: list[Page] = Field(default_factory=list)  # entry first, then the sample order
    templates: dict[str, str | None] = Field(default_factory=dict)  # url -> template key
    coverage: dict[str, Any] = Field(default_factory=dict)
    sitemap_urls: list[str] = Field(default_factory=list)  # normalised sitemap locs, sorted
    partial_sample: bool = False  # fewer pages fetched than planned


class IndexabilityTable(BaseModel):
    items: list[IndexabilityRow] = Field(default_factory=list)


class DuplicateReport(BaseModel):
    duplicate_pairs: int = 0


# --- suggest_links


class LinkSuggestion(BaseModel):
    source_url: str
    similarity: float  # TF-IDF cosine of the main texts, rounded
    anchor_candidates: list[str] = Field(default_factory=list)  # target title/H1, then shared n-grams
    shared_terms: list[str] = Field(default_factory=list)  # terms of both texts, strongest first


class LinkTarget(BaseModel):
    url: str
    inlinks: int = 0  # internal links from other sampled pages
    suggestions: list[LinkSuggestion] = Field(default_factory=list)


class LinkSuggestions(BaseModel):
    targets: list[LinkTarget] = Field(default_factory=list)
    thresholds: dict[str, float | int] = Field(default_factory=dict)  # max_inlinks, min_similarity, ...
    pages_considered: int = 0  # pages with enough main text to compare
    pages_skipped: int = 0  # pages beyond config.max_pages_cap
    targets_skipped: int = 0  # given targets beyond config.max_batch_urls
    unknown_targets: list[str] = Field(default_factory=list)  # given targets that are not sampled pages
