"""The content operations as callers see them: Result envelopes, JSON, the schema contract, catalog."""

from __future__ import annotations

import importlib
import json
import re
from importlib.resources import files
from pathlib import Path

from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import rules
from seoprobe.features.content import (
    FEATURE,
    LocalSeo,
    Previews,
    ReadabilityData,
    SchemaReport,
    SchemaSuggestions,
    local_seo,
    previews,
    readability,
    schema,
    suggest_schema,
)
from tests import fakes

URL = "https://example.com/blog/post"
HTML = (
    "<html lang='en'><head><title>Green tea guide</title>"
    "<meta name='description' content='All about green tea'>"
    "<meta property='og:image' content='https://example.com/og.png'>"
    '<script type="application/ld+json">{"@context":"http://schema.org","@type":"Product","name":"Tea"}</script>'
    "</head><body><h1>Green tea</h1>"
    f"<p>{'Green tea is a drink made from leaves. ' * 20}</p>"
    "<p>Call 0312 555 12 34. Address: Atatürk Cad. No:5 Çankaya</p></body></html>"
)


async def test_every_operation_returns_a_json_serializable_result() -> None:
    async with fakes.session({URL: fakes.html_page(HTML)}) as s:
        results = [
            await readability(URL, keywords=["green tea"], session=s),
            await previews(URL, session=s),
            await schema(URL, session=s),
            await suggest_schema(URL, session=s),
            await local_seo(URL, session=s),
        ]
    models = [ReadabilityData, Previews, SchemaReport, SchemaSuggestions, LocalSeo]
    for res, model in zip(results, models, strict=True):
        assert res.ok, res.error
        assert isinstance(res.data, model)
        doc = json.loads(res.to_json())
        assert doc["operation"] == res.operation and doc["feature"] == "content"


async def test_schema_contract() -> None:
    async with fakes.session({URL: fakes.html_page(HTML)}) as s:
        res = await schema(URL, session=s)
    assert res.ok and res.status == "ok"
    data = res.data
    assert isinstance(data, SchemaReport)
    assert data.url == URL and data.checked and data.types == ["Product"] and data.blocks == 1
    assert data.facts == {"schema_types": ["Product"], "jsonld_blocks": 1}
    ids = {f.id for f in res.findings}
    assert {"schema-context-not-https", "schema-product-missing-fields"} <= ids
    assert all(f.url == URL for f in res.findings)


async def test_schema_on_an_unparsed_page_is_not_checked() -> None:
    async with fakes.session({URL: fakes.html_page("<html></html>", status=500)}) as s:
        res = await schema(URL, session=s)
    assert res.ok and res.data == SchemaReport(url=URL, checked=False)
    assert res.findings == []


async def test_schema_golden_fixture_through_the_operation(fixtures: Path) -> None:
    html = (fixtures / "schema_rich.html").read_text(encoding="utf-8")
    target = Page(url=URL, final_url=URL, status=200, html=html, content_type="text/html")
    async with fakes.session() as s:
        res = await schema(target, session=s)
    assert res.ok and res.data is not None and "HowTo" in res.data.types
    assert "schema-deprecated-type" in {f.id for f in res.findings}


async def test_turkish_session_localizes_findings_and_suggestions() -> None:
    from seoprobe.core.config import Config

    async with fakes.session({URL: fakes.html_page(HTML)}, config=Config(lang="tr")) as s:
        res = await schema(URL, session=s)
        sug = await suggest_schema(URL, session=s)
    f = next(f for f in res.findings if f.id == "schema-context-not-https")
    assert "kullanıyor" in f.message
    assert sug.data is not None and any("doldurun" in i.json_ld for i in sug.data.items)


async def test_parse_failure_gives_empty_models_and_not_checked() -> None:
    async with fakes.session({URL: fakes.html_page("", status=404)}) as s:
        r = await readability(URL, session=s)
        p = await previews(URL, session=s)
    assert r.status == "partial" and r.data == ReadabilityData()
    assert [(n.check_id, n.reason_code, n.url) for n in r.not_checked] == [("readability", "parse-failed", URL)]
    assert p.status == "partial" and p.data == Previews()
    assert [(n.check_id, n.reason_code) for n in p.not_checked] == [("previews", "parse-failed")]


async def test_unreachable_target_is_an_error_result() -> None:
    async with fakes.session(errors={URL: "unreachable"}) as s:
        res = await schema(URL, session=s)
    assert not res.ok and res.error is not None


def test_operations_and_timeouts() -> None:
    timeouts = {op.name: op.timeout_s for op in FEATURE.operations}
    assert timeouts == {
        "readability": 20,
        "previews": 30,
        "schema": 30,
        "suggest_schema": 15,
        "local_seo": 30,
    }


_EMITS = re.compile(r'(?:make_finding|add)\(\s*"([a-z0-9-]+)"')


def test_every_rule_id_in_the_code_exists_in_the_catalog() -> None:
    known = rules()
    own = json.loads(files("seoprobe.features.content").joinpath("catalog.json").read_text(encoding="utf-8"))
    emitted: set[str] = set()
    for name in ("readability", "previews", "schema.checks", "schema.suggest", "local_seo"):
        mod = importlib.import_module(f"seoprobe.features.content.{name}")
        src = Path(mod.__file__ or "").read_text(encoding="utf-8")
        emitted |= set(_EMITS.findall(src))
    declared: set[str] = set()
    for name, const in (
        ("readability", "RULE_IDS"),
        ("previews", "RULE_IDS"),
        ("schema.checks", "SCHEMA_RULE_IDS"),
        ("local_seo", "LOCAL_RULE_IDS"),
    ):
        declared |= set(getattr(importlib.import_module(f"seoprobe.features.content.{name}"), const))
    assert emitted == declared
    assert declared <= set(own["rules"]) and declared <= set(known)
    assert set(own["rules"]) == declared  # the catalog holds no rule the code never emits
    for rid in declared:
        d = known[rid]
        assert own["messages"]["en"].get(d.message_key) and own["messages"]["tr"].get(d.message_key)
