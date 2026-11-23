"""Sitemaps: discovery and analysis of the site's own sitemaps, and a suggested sitemap.xml."""

from .discovery import COMMON_PATHS, _gunzip_bounded, classify, discover_sitemaps, parse_w3c_datetime, sitemap
from .generate import (
    build_sitemap,
    exclusion_reason,
    generate_sitemap,
    header_directives,
    meta_noindex,
    render_xml,
    same_page,
)

__all__ = [
    "COMMON_PATHS",
    "_gunzip_bounded",
    "build_sitemap",
    "classify",
    "discover_sitemaps",
    "exclusion_reason",
    "generate_sitemap",
    "header_directives",
    "meta_noindex",
    "parse_w3c_datetime",
    "render_xml",
    "same_page",
    "sitemap",
]
