"""
seoprobe: async SEO analysis for Python applications.

Every operation is ``await seoprobe.<name>(...)`` and returns a ``Result``. Namespaces group the
operations of one service: ``seoprobe.gsc``, ``seoprobe.ga4``, ``seoprobe.bing``,
``seoprobe.indexnow`` and ``seoprobe.report``. Only the names in ``__all__`` are public API.
"""

import logging

from seoprobe.core import (
    AuditMode,
    Category,
    Config,
    ErrorCode,
    ErrorInfo,
    Event,
    Finding,
    GoogleApp,
    Meta,
    NormalizedUrl,
    NotChecked,
    Page,
    Preflight,
    RequestLog,
    Requirements,
    Result,
    SeoprobeError,
    Session,
    Severity,
    Status,
    fetch,
    normalize,
)
from seoprobe.core.config import package_version
from seoprobe.features import check_access, operations, preflight, report, requirements, rules
from seoprobe.features.audit import Audit, AuditOptions, audit_page, audit_site, audit_stream, rerun
from seoprobe.features.content import local_seo, previews, readability, schema, suggest_schema
from seoprobe.features.crawl import crawl, duplicates, hreflang, indexability, links, suggest_links
from seoprobe.features.monitoring import analyze_logs, check_redirects, check_urls, diff, snapshot
from seoprobe.features.offsite import authority, competitors, visibility
from seoprobe.features.page import classify, onpage
from seoprobe.features.performance import carbon, field_data, lab_metrics, pagespeed, web_vitals
from seoprobe.features.render import accessibility, mobile, render, screenshots
from seoprobe.features.report import AuditReport
from seoprobe.features.search import (
    AuthRequest,
    FileTokenStore,
    MemoryTokenStore,
    TokenStore,
    bing,
    ga4,
    gsc,
    indexnow,
)
from seoprobe.features.site import (
    ai_access,
    dns_health,
    generate_sitemap,
    robots,
    site_setup,
    sitemap,
    techstack,
    transport,
)

logging.getLogger("seoprobe").addHandler(logging.NullHandler())

__version__ = package_version()

__all__ = [
    # types
    "Audit",
    "AuditMode",
    "AuditOptions",
    "AuditReport",
    "AuthRequest",
    "Category",
    "Config",
    "ErrorCode",
    "ErrorInfo",
    "Event",
    "FileTokenStore",
    "Finding",
    "GoogleApp",
    "MemoryTokenStore",
    "Meta",
    "NormalizedUrl",
    "NotChecked",
    "Page",
    "Preflight",
    "RequestLog",
    "Requirements",
    "Result",
    "SeoprobeError",
    "Session",
    "Severity",
    "Status",
    "TokenStore",
    "__version__",
    # registry
    "check_access",
    "operations",
    "preflight",
    "requirements",
    "rules",
    # core
    "fetch",
    "normalize",
    # page
    "classify",
    "onpage",
    # content
    "local_seo",
    "previews",
    "readability",
    "schema",
    "suggest_schema",
    # site
    "ai_access",
    "dns_health",
    "generate_sitemap",
    "robots",
    "site_setup",
    "sitemap",
    "techstack",
    "transport",
    # crawl
    "crawl",
    "duplicates",
    "hreflang",
    "indexability",
    "links",
    "suggest_links",
    # render
    "accessibility",
    "mobile",
    "render",
    "screenshots",
    # performance
    "carbon",
    "field_data",
    "lab_metrics",
    "pagespeed",
    "web_vitals",
    # offsite
    "authority",
    "competitors",
    "visibility",
    # monitoring
    "analyze_logs",
    "check_redirects",
    "check_urls",
    "diff",
    "snapshot",
    # audit
    "audit_page",
    "audit_site",
    "audit_stream",
    "rerun",
    # namespaces
    "bing",
    "ga4",
    "gsc",
    "indexnow",
    "report",
]
