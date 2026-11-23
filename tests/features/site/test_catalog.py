"""Every rule id the site feature emits exists in its catalog.json, and every catalog rule is used."""

from __future__ import annotations

import re
from importlib.resources import files

from seoprobe.core.rules import catalog
from seoprobe.features.site import FEATURE
from seoprobe.features.site.transport import HEADER_RULE_IDS, TLS_RULE_IDS

MODULES = (
    "transport.py",
    "robots.py",
    "sitemap/discovery.py",
    "sitemap/generate.py",
    "setup.py",
    "ai_access.py",
    "techstack.py",
    "dns_health.py",
)
# not_checked ids that name a measurement, not a rule (as in the source)
NOT_RULES = {"markdown-negotiation", "sitemap_generated"}


def _source() -> str:
    pkg = files("seoprobe.features.site")
    return "\n".join(pkg.joinpath(m).read_text("utf-8") for m in MODULES)


def _referenced() -> set[str]:
    src = _source()
    ids = set(re.findall(r'(?:make_finding|mk)\(\s*"([a-z0-9-]+)"', src))
    ids |= set(re.findall(r'check_id="([a-z0-9_-]+)"', src))
    ids |= set(re.findall(r'^\s*\("[a-z-]+", "([a-z0-9-]+-duplicate)"\),$', src, re.M))  # site_setup variants
    return ids | set(TLS_RULE_IDS) | set(HEADER_RULE_IDS)


def test_every_emitted_rule_id_is_in_the_site_catalog() -> None:
    own = FEATURE.catalog.rules
    missing = sorted(i for i in _referenced() - NOT_RULES if i not in own)
    assert missing == []
    for rid in _referenced() - NOT_RULES:
        assert catalog.rule_feature(rid) == "site"


def test_every_catalog_rule_is_referenced_by_the_code() -> None:
    src = _source()
    unused = sorted(rid for rid in FEATURE.catalog.rules if f'"{rid}"' not in src)
    assert unused == []


def test_every_rule_has_texts() -> None:
    msgs = FEATURE.catalog.messages
    for rid, d in FEATURE.catalog.rules.items():
        assert msgs["en"].get(d.message_key) and msgs["tr"].get(d.message_key), rid
