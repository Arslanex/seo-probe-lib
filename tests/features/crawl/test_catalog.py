"""Every rule id the crawl feature emits exists in its catalog.json, and every catalog rule is used."""

from __future__ import annotations

import re
from importlib.resources import files

from seoprobe.core.rules import catalog
from seoprobe.features.crawl import FEATURE
from seoprobe.features.crawl.hreflang import HREFLANG_RULE_IDS
from seoprobe.features.crawl.suggest import RULE_ID as SUGGEST_RULE_ID

MODULES = ("crawl.py", "links/graph.py", "links/indexability.py", "duplicates.py", "hreflang.py", "suggest.py")
# not_checked ids that name a measurement, not a rule (as in the source)
NOT_RULES = {"hreflang", "link-health"}  # link-health: a time budget left the link checks partial


def _source() -> str:
    pkg = files("seoprobe.features.crawl")
    return "\n".join(pkg.joinpath(m).read_text("utf-8") for m in MODULES)


def _referenced() -> set[str]:
    src = _source()
    ids = set(re.findall(r'(?:make_finding|add)\(\s*"([a-z0-9-]+)"', src))
    ids |= set(re.findall(r'check_id="([a-z0-9_-]+)"', src))
    ids |= set(re.findall(r'rule = "([a-z0-9-]+)"', src))
    ids |= set(re.findall(r'"[a-z0-9-]+" if raw\.kind == "internal" else "([a-z0-9-]+)"', src))
    return ids | set(HREFLANG_RULE_IDS) | {SUGGEST_RULE_ID}


def test_every_emitted_rule_id_is_in_the_crawl_catalog() -> None:
    own = FEATURE.catalog.rules
    missing = sorted(i for i in _referenced() - NOT_RULES if i not in own)
    assert missing == []
    for rid in _referenced() - NOT_RULES:
        assert catalog.rule_feature(rid) == "crawl"


def test_every_catalog_rule_is_emitted_and_has_texts() -> None:
    own = FEATURE.catalog.rules
    assert sorted(set(own) - _referenced()) == []
    for d in own.values():
        for key in (d.message_key, d.recommendation_key, d.failure_check_key, d.leading_indicator_key):
            if not key:
                continue
            assert all(catalog.message(key, lang) != key for lang in ("en", "tr")), key


def test_rules_the_source_did_not_have_are_not_scored() -> None:
    own = FEATURE.catalog.rules
    assert own[SUGGEST_RULE_ID].scored is False
    assert all(d.scored for rid, d in own.items() if rid != SUGGEST_RULE_ID)  # ported rules keep scoring
