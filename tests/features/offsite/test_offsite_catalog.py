"""
The offsite feature emits no findings (no rule ids, so no catalog.json); its not_checked ids
name measurements, and every reason code it reports has a report text.
"""

from __future__ import annotations

import re
from importlib.resources import files

from seoprobe.core.rules import catalog
from seoprobe.features.offsite import FEATURE

MODULES = ("authority.py", "visibility.py", "competitors.py")
NOT_RULES = {"authority", "competitors", "visibility"}  # measurement names (visibility: its time budget)


def _source() -> str:
    pkg = files("seoprobe.features.offsite")
    return "\n".join(pkg.joinpath(m).read_text("utf-8") for m in MODULES)


def test_no_rule_id_is_emitted() -> None:
    src = _source()
    assert re.findall(r'(?:make_finding|\.finding)\(\s*"([a-z0-9-]+)"', src) == []
    assert set(re.findall(r'not_checked\("([a-z0-9_-]+)"', src)) == NOT_RULES
    assert not files("seoprobe.features.offsite").joinpath("catalog.json").is_file()
    assert FEATURE.catalog.rules == {}


def test_reason_codes_have_texts() -> None:
    codes = ["no-api-key", "no-competitors", "time-budget"] + [
        f"authority-{e}"
        for e in ("no-data", "unauthorized", "rate-limited", "timeout", "unreachable", "target-rejected", "too-large")
    ]
    for code in codes:
        assert catalog.reason_text("en", code) != code, code
