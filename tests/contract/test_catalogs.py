"""Every rule of the source project is installed, and every catalog is well-formed."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from seoprobe.core.models import Category, Layer, Severity
from seoprobe.core.rules import catalog
from seoprobe.features import FEATURES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_fresh_import_installs_every_source_rule() -> None:
    code = "import json, seoprobe\nfrom seoprobe.core.rules import catalog\nprint(json.dumps(sorted(catalog.rules())))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    installed = set(json.loads(out.stdout))
    source = set(json.loads((FIXTURES / "source_rules.json").read_text()))
    assert source <= installed, sorted(source - installed)


def test_rule_ids_are_unique_across_catalogs() -> None:
    seen: dict[str, str] = {}
    for c in catalog.installed():
        for rid in c.rules:
            assert rid not in seen, f"{rid} in both {seen[rid]} and {c.feature}"
            seen[rid] = c.feature


def test_every_rule_has_a_finding_text_in_both_languages() -> None:
    for c in catalog.installed():
        msgs = {lang: {k for cat in catalog.installed() for k in cat.messages[lang]} for lang in catalog.LANGS}
        for rid, d in c.rules.items():
            for lang in catalog.LANGS:
                assert d.message_key in msgs[lang], f"{c.feature}: {rid} has no {lang} text ({d.message_key})"


def test_both_languages_have_the_same_keys_in_every_catalog() -> None:
    for c in catalog.installed():
        assert set(c.messages["en"]) == set(c.messages["tr"]), c.feature


def test_catalog_files_have_the_documented_shape() -> None:
    packages = [("seoprobe.core", "data/catalog.json")] + [(f.package, "catalog.json") for f in FEATURES]
    for package, path in packages:
        resource = files(package).joinpath(path)
        if not resource.is_file():
            continue
        data = json.loads(resource.read_text(encoding="utf-8"))
        assert set(data) <= {"rules", "messages"}, package
        for rid, row in data.get("rules", {}).items():
            assert rid == rid.lower() and " " not in rid, rid
            Category(row["category"])
            Severity(row["severity"])
            Layer(row.get("layer", "A"))
            assert row.get("scope", "page") in ("page", "site"), rid
        assert set(data.get("messages", {})) <= set(catalog.LANGS), package
