"""The report sections: every section is measured (with data) or not measured (with a reason)."""

from __future__ import annotations

from typing import Any

from seoprobe.features.audit import Audit

from .content import local_section, previews_section, readability_section, schema_suggest_section
from .contract import check_rows, not_measured
from .crawl import hreflang_section, indexability_section, links_section, visibility_section
from .offsite import authority_section, competitors_section
from .overview import glance_section, scope_note
from .performance import field_section, images_section, performance_section, rate
from .search import gsc_section, rankings_section
from .site import agentic_section, screenshots_section, site_setup_section, sitemap_gen_section, techstack_section


def build_deep(rec: Audit, lang: str) -> dict[str, Any]:
    return {
        "links": links_section(rec, lang),
        "performance": performance_section(rec, lang),
        "images": images_section(rec, lang),
        "indexability": indexability_section(rec, lang),
        "visibility": visibility_section(rec, lang),
        "gsc": gsc_section(rec, lang),
        "field": field_section(rec, lang),
    }


def build_suite(rec: Audit, lang: str) -> dict[str, Any]:
    tech = techstack_section(rec, lang)
    return {
        "glance": glance_section(rec, lang, tech),
        "techstack": tech,
        "site_setup": site_setup_section(rec, lang),
        "previews": previews_section(rec, lang),
        "screenshots": screenshots_section(rec, lang),
        "readability": readability_section(rec, lang),
        "hreflang": hreflang_section(rec, lang),
        "schema_suggestions": schema_suggest_section(rec, lang),
        "agentic": agentic_section(rec, lang),
    }


def build_extra(rec: Audit, lang: str) -> dict[str, Any]:
    return {
        "rankings": rankings_section(rec, lang),
        "local": local_section(rec, lang),
        "sitemap_gen": sitemap_gen_section(rec, lang),
        "authority": authority_section(rec, lang),
    }


__all__ = [
    "agentic_section",
    "authority_section",
    "build_deep",
    "build_extra",
    "build_suite",
    "check_rows",
    "competitors_section",
    "field_section",
    "glance_section",
    "gsc_section",
    "hreflang_section",
    "images_section",
    "indexability_section",
    "links_section",
    "local_section",
    "not_measured",
    "performance_section",
    "previews_section",
    "rankings_section",
    "rate",
    "readability_section",
    "schema_suggest_section",
    "scope_note",
    "screenshots_section",
    "site_setup_section",
    "sitemap_gen_section",
    "techstack_section",
    "visibility_section",
]
