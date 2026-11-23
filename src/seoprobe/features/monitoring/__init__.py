"""
Monitoring: URL list and redirect map checks, page snapshots and their diffs, access log crawl
analysis.
"""

from seoprobe.core.feature import Feature

from .batch import check_redirects, check_urls
from .logs import analyze_logs
from .models import (
    BotStats,
    FieldChange,
    HreflangPair,
    LogAnalysis,
    PathCount,
    RedirectCheck,
    RedirectChecks,
    Snapshot,
    SnapshotDiff,
    UrlCheck,
    UrlChecks,
    WastedCrawl,
)
from .snapshot import diff, snapshot

FEATURE = Feature(
    name="monitoring",
    package=__name__,
    operations=[check_urls, check_redirects, snapshot, diff, analyze_logs],
    depends_on=["page", "site"],
)

__all__ = [
    "check_urls",
    "check_redirects",
    "snapshot",
    "diff",
    "analyze_logs",
    "BotStats",
    "FieldChange",
    "HreflangPair",
    "LogAnalysis",
    "PathCount",
    "RedirectCheck",
    "RedirectChecks",
    "Snapshot",
    "SnapshotDiff",
    "UrlCheck",
    "UrlChecks",
    "WastedCrawl",
]
