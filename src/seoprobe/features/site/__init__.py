"""
Site-level setup: transport security, robots.txt, sitemaps, redirects and well-known files,
AI agent access, the technology stack and DNS health.
"""

from seoprobe.core.feature import Feature

from .ai_access import ai_access
from .dns_health import dns_health
from .models import (
    AgenticCheck,
    AgenticReadiness,
    AICrawlerStatus,
    CaaInfo,
    DmarcInfo,
    DnsHealth,
    DnssecInfo,
    ExcludedPage,
    Group,
    HostAddresses,
    MxInfo,
    NsInfo,
    Probe,
    RedirectVariant,
    RobotsReport,
    RobotsRules,
    SitemapFile,
    SitemapGenerated,
    SitemapReport,
    SitemapUrl,
    SiteSetup,
    SpfInfo,
    Status,
    TechItem,
    TechStack,
    TransportReport,
)
from .robots import can_fetch, parse_robots, robots
from .setup import site_setup
from .sitemap import generate_sitemap, header_directives, meta_noindex, same_page, sitemap
from .techstack import MAX_HTML, detect, techstack
from .transport import transport

FEATURE = Feature(
    name="site",
    package=__name__,
    operations=[transport, site_setup, robots, sitemap, generate_sitemap, ai_access, techstack, dns_health],
    depends_on=["page"],
)

__all__ = [
    "transport",
    "site_setup",
    "robots",
    "sitemap",
    "generate_sitemap",
    "ai_access",
    "techstack",
    "dns_health",
    "header_directives",
    "meta_noindex",
    "same_page",
    "detect",
    "MAX_HTML",
    "can_fetch",
    "parse_robots",
    "RedirectVariant",
    "Probe",
    "SiteSetup",
    "TechItem",
    "TechStack",
    "Status",
    "AgenticCheck",
    "AgenticReadiness",
    "ExcludedPage",
    "SitemapGenerated",
    "Group",
    "RobotsRules",
    "AICrawlerStatus",
    "RobotsReport",
    "SitemapUrl",
    "SitemapFile",
    "SitemapReport",
    "TransportReport",
    "DnsHealth",
    "SpfInfo",
    "DmarcInfo",
    "MxInfo",
    "CaaInfo",
    "DnssecInfo",
    "HostAddresses",
    "NsInfo",
]
