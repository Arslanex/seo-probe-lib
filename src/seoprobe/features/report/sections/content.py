"""Report sections for the content module blocks: previews, readability, schema suggestions and local SEO."""

from __future__ import annotations

from typing import Any

from seoprobe.core.parse.text import clean_text
from seoprobe.core.rules.catalog import t
from seoprobe.features.audit import Audit
from seoprobe.features.content import LocalSeo, Previews, ReadabilityData, SchemaSuggestions

from .. import models as s
from .contract import _c, _label, _load, _nm, _why, _yn

LIST_CAP = 30


KEYWORD_CAP = 20


SCHEMA_ITEMS = 12


JSON_LD_CAP = 6000


def _bool_text(lang: str, v: bool | None) -> str:
    return "—" if v is None else t(lang, "idx.yes" if v else "idx.no")


def previews_section(rec: Audit, lang: str) -> s.PreviewsSection:
    data = _load(rec, "previews", Previews)
    if isinstance(data, str):
        return s.PreviewsSection(**_nm(lang, data))
    serp = [
        s.SerpRow(
            device=p.device,
            device_label=_label(lang, "dev", p.device),
            display_url=_c(p.display_url, 200),
            title_shown=_c(p.title_shown, 200),
            title_truncated=p.title_truncated,
            description_shown=_c(p.description_shown, 400),
            description_truncated=p.description_truncated,
            description_missing=p.description_missing,
        )
        for p in data.serp
    ]
    card = data.social
    social = [s.KV(label=f"og:{_c(k, 30)}", value=_c(v, 200)) for k, v in sorted(card.og.items())] + [
        s.KV(label=f"twitter:{_c(k, 30)}", value=_c(v, 200)) for k, v in sorted(card.twitter.items())
    ]
    img = card.image
    image_rows: list[s.KV] = []
    if img.url:
        image_rows.append(s.KV(label=t(lang, "prev.img.url"), value=_c(img.url, 300)))
        image_rows.append(s.KV(label=t(lang, "prev.img.reach"), value=_bool_text(lang, img.reachable)))
        if img.width and img.height:
            image_rows.append(s.KV(label=t(lang, "prev.img.size"), value=f"{img.width}×{img.height}"))
        if img.ratio:
            image_rows.append(s.KV(label=t(lang, "prev.img.ratio"), value=f"{img.ratio:.2f}:1"))
        if img.bytes is not None:
            kb = round(img.bytes / 1024)
            image_rows.append(
                s.KV(
                    label=t(lang, "prev.img.weight"),
                    value=f"{'≥ ' if img.bytes_is_lower_bound else ''}{kb} KB",
                )
            )
        if img.error:
            image_rows.append(
                s.KV(
                    label=t(lang, "prev.img.error"),
                    value=_label(lang, "reason", img.error.split(":")[0]),
                )
            )
    if card.missing_og:
        social.append(s.KV(label=t(lang, "prev.missing"), value=", ".join(_c(x, 30) for x in card.missing_og)))
    if not serp and not social and not image_rows:
        return s.PreviewsSection(**_nm(lang, "suite-no-data"))
    return s.PreviewsSection(
        status="measured",
        serp=serp,
        social=social,
        social_image=image_rows,
        notes=[_label(lang, "prev.note", n) for n in data.notes],
    )


def _term_rows(items: list[dict[str, Any]]) -> list[s.KV]:
    out = []
    for it in items[:LIST_CAP]:
        name = it.get("term") or it.get("phrase") or it.get("text") or ""
        count = it.get("count", it.get("n", ""))
        if name:
            out.append(s.KV(label=_c(name, 80), value=_c(count, 12)))
    return out


def readability_section(rec: Audit, lang: str) -> s.ReadabilitySection:
    data = _load(rec, "readability", ReadabilityData)
    if isinstance(data, str):
        return s.ReadabilitySection(**_nm(lang, data))
    r = data.readability or {}
    scores: list[s.KV] = []
    if isinstance(r.get("score"), (int, float)):
        scores.append(
            s.KV(
                label=_label(lang, "read.formula", str(r.get("formula", ""))),
                value=f"{float(r['score']):.1f}"
                + (f" · {_label(lang, 'read.val', str(r['label']))}" if r.get("label") else ""),
            )
        )
    if isinstance(r.get("grade"), (int, float)):
        scores.append(s.KV(label=t(lang, "read.m.grade"), value=f"{float(r['grade']):.1f}"))
    for key in ("words", "sentences", "paragraphs", "avg_sentence_words", "avg_syllables_per_word"):
        if isinstance(r.get(key), (int, float)):
            scores.append(s.KV(label=t(lang, f"read.m.{key}"), value=_c(r[key], 12)))
    if isinstance(r.get("long_sentence_share"), (int, float)):
        scores.append(
            s.KV(
                label=t(lang, "read.m.long_sentence_share"),
                value=f"{round(float(r['long_sentence_share']) * 100)}%",
            )
        )
    terms = _term_rows(data.terms)
    phrases = _term_rows(data.phrases)
    if not scores and not terms:
        return s.ReadabilitySection(
            **_nm(lang, _why(rec, "readability") if not data.language else "language-unsupported")
        )
    kws = [
        s.KeywordRow(
            keyword=_c(k.keyword, 80),
            in_title=k.in_title,
            in_h1=k.in_h1,
            in_description=k.in_description,
            in_url=k.in_url,
            in_first_100_words=k.in_first_100_words,
            in_subheadings=k.in_subheadings,
            occurrences=k.occurrences,
            density_percent=round(k.density_percent, 2),
        )
        for k in data.keywords[:KEYWORD_CAP]
    ]
    return s.ReadabilitySection(
        status="measured",
        language=_c(data.language, 10) if data.language else None,
        scores=scores,
        terms=terms,
        phrases=phrases,
        keywords=kws,
        keywords_not_measured_text=None if kws else t(lang, "read.kw.none"),
        suggested_targets=[_c(x, 80) for x in data.suggested_targets[:10]],
        notes=[_label(lang, "read.note", n) for n in data.notes],
    )


def schema_suggest_section(rec: Audit, lang: str) -> s.SchemaSuggestSection:
    data = _load(rec, "schema_suggestions", SchemaSuggestions)
    if isinstance(data, str):
        return s.SchemaSuggestSection(**_nm(lang, data))
    if not data.items:
        return s.SchemaSuggestSection(
            **_nm(lang, "schema-nothing-to-suggest"),
            notes=[_label(lang, "schemasug.note", n) for n in data.notes],
        )
    items = [
        s.SchemaItem(
            url=_c(i.url, 300),
            type=_c(i.type, 60),
            reason=_c(i.reason, 300),
            json_ld=clean_text(i.json_ld)[:JSON_LD_CAP],
            complete=i.complete,
            missing_fields=[_c(x, 60) for x in i.missing_fields[:LIST_CAP]],
            rich_result_note=_c(i.rich_result_note, 300),
            warning=_c(i.warning, 300),
        )
        for i in data.items[:SCHEMA_ITEMS]
    ]
    return s.SchemaSuggestSection(
        status="measured",
        items=items,
        items_total=len(data.items),
        truncated=len(data.items) > SCHEMA_ITEMS,
        notes=[_label(lang, "schemasug.note", n) for n in data.notes],
    )


LOCAL_LIST_CAP = 20


def local_section(rec: Audit, lang: str) -> s.LocalSection:
    data = _load(rec, "local", LocalSeo)
    if isinstance(data, str):
        return s.LocalSection(**_nm(lang, _why(rec, "local") if data == "suite-no-data" else data))
    if not data.applicable:
        return s.LocalSection(**_nm(lang, _why(rec, "local")))
    facts = [
        s.KV(label=t(lang, "local.fact.pages"), value=str(data.pages_scanned)),
        s.KV(label=t(lang, "local.fact.tel"), value=_yn(lang, data.has_tel_link)),
        s.KV(label=t(lang, "local.fact.map"), value=_yn(lang, data.has_map_embed)),
    ]
    if data.business_types:
        facts.append(
            s.KV(
                label=t(lang, "local.fact.types"),
                value=", ".join(_c(x, 40) for x in data.business_types[:LOCAL_LIST_CAP]),
            )
        )
    if data.names:
        facts.append(
            s.KV(
                label=t(lang, "local.fact.names"),
                value=", ".join(_c(x, 80) for x in data.names[:5]),
            )
        )
    facts.append(
        s.KV(
            label=t(lang, "local.fact.signals"),
            value=", ".join(_label(lang, "local.signal", x) for x in data.signals) or "—",
        )
    )
    return s.LocalSection(
        status="measured",
        facts=facts,
        phones=[
            s.LocalPhoneRow(
                number=_c(p.number, 30),
                formats=", ".join(_c(f, 30) for f in p.formats),
                sources=", ".join(_label(lang, "local.source", x) for x in p.sources),
                pages=len(p.pages),
            )
            for p in data.phones[:LOCAL_LIST_CAP]
        ],
        addresses=[
            s.LocalAddressRow(
                text=_c(a.text, 200),
                source=_label(lang, "local.source", a.source),
                pages=len(a.pages),
            )
            for a in data.addresses[:LOCAL_LIST_CAP]
        ],
        issues=[
            s.LocalIssueRow(
                label=_label(lang, "local.issue", i.code),
                values=" | ".join(_c(v, 120) for v in i.values),
                pages=", ".join(_c(u, 200) for u in i.pages[:3]),
            )
            for i in data.inconsistencies[:LOCAL_LIST_CAP]
        ],
        notes=[_label(lang, "local.note", n) for n in data.notes],
        note=t(lang, "local.intro"),
    )
