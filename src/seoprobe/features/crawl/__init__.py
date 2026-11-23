"""
Discovery and sampling of a site's pages, and the analyses over the sample: link graph and
link health, indexability, near-duplicate content and hreflang.
"""

from seoprobe.core.feature import Feature

from .crawl import crawl
from .duplicates import duplicates
from .hreflang import hreflang
from .links import indexability, links
from .models import (
    BLOCKED_STATUSES,
    CrawlReport,
    DuplicateReport,
    HreflangEntry,
    HreflangPageRow,
    HreflangReport,
    IndexabilityRow,
    IndexabilityTable,
    LinkRecord,
    LinkSuggestion,
    LinkSuggestions,
    LinkSummary,
    LinkTarget,
    PageLinkStats,
    Verdict,
    classify_link,
)
from .suggest import suggest_links

FEATURE = Feature(
    name="crawl",
    package=__name__,
    operations=[crawl, links, indexability, duplicates, hreflang, suggest_links],
    depends_on=["page", "site"],
)

__all__ = [
    "crawl",
    "links",
    "indexability",
    "duplicates",
    "hreflang",
    "suggest_links",
    "BLOCKED_STATUSES",
    "Verdict",
    "classify_link",
    "CrawlReport",
    "LinkRecord",
    "PageLinkStats",
    "LinkSummary",
    "LinkSuggestion",
    "LinkSuggestions",
    "LinkTarget",
    "IndexabilityRow",
    "IndexabilityTable",
    "DuplicateReport",
    "HreflangEntry",
    "HreflangPageRow",
    "HreflangReport",
]
