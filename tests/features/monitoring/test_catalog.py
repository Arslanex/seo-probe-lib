"""
The monitoring rules: every emitted id exists, is unscored and has texts in both languages;
the operations are registered with their requirements.
"""

from __future__ import annotations

import re
from pathlib import Path

import seoprobe
from seoprobe.core.rules import catalog
from seoprobe.core.session import Session
from seoprobe.features.monitoring import batch
from seoprobe.features.monitoring.snapshot import change_rules

SRC = Path(batch.__file__).parent


def _emitted() -> set[str]:
    code = "\n".join((SRC / f).read_text() for f in ("batch.py", "snapshot.py", "logs.py"))
    ids = set(re.findall(r'ctx\.finding\(\s*"([a-z0-9-]+)"', code))
    ids |= set(re.findall(r'ctx\.not_checked\(\s*"([a-z0-9-]+)"', code))
    ids |= set(batch._REASON_RULES.values()) | set(batch._ISSUE_RULES.values())
    ids |= {row["rule"] for row in change_rules().values()}
    return ids


def test_every_emitted_rule_is_in_the_catalog_unscored_with_both_languages() -> None:
    ids = _emitted()
    rules = catalog.rules()
    assert ids and not [i for i in ids if i not in rules]
    own = {rid for rid in rules if catalog.rule_feature(rid) == "monitoring"}
    assert own == ids  # and nothing unused in the catalog
    for rid in ids:
        d = rules[rid]
        assert d.scored is False and d.layer.value == "A", rid
        for prefix in ("finding", "rec"):
            en, tr = catalog.t("en", f"{prefix}.{rid}"), catalog.t("tr", f"{prefix}.{rid}")
            assert en != f"{prefix}.{rid}" and tr != f"{prefix}.{rid}" and en != tr, rid


def test_rule_ids_are_kebab_case() -> None:
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", rid) for rid in _emitted())


def test_reason_codes_have_texts() -> None:
    for code in ("verification-cap", "robots-unreachable"):
        assert catalog.reason_text("tr", code) != code and catalog.reason_text("en", code) != code


async def test_operations_are_registered_and_ready_without_credentials() -> None:
    ops = {o.name: o for o in seoprobe.operations() if o.feature == "monitoring"}
    assert set(ops) == {"check_urls", "check_redirects", "snapshot", "diff", "analyze_logs"}
    assert ops["diff"].requires.network is False and ops["check_urls"].requires.network is True
    assert all(not o.requires.credentials and not o.requires.extras and not o.requires.oauth for o in ops.values())
    async with Session() as s:
        for name in ops:
            pf = await seoprobe.preflight(name, session=s)
            assert pf.ready and pf.missing == []
