"""
Report sections for the site-level module blocks: technology, site setup, screenshots, agentic
readiness and the suggested sitemap.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from seoprobe.core.parse.text import clean_text
from seoprobe.core.rules.catalog import t
from seoprobe.features.audit import Audit
from seoprobe.features.render import Screenshots
from seoprobe.features.site import AgenticReadiness, SitemapGenerated, SiteSetup, TechStack

from .. import models as s
from .contract import _c, _codes, _label, _load, _nm, _why, _yn

LIST_CAP = 30


SCREENSHOT_FILES = ("desktop.jpg", "mobile.jpg")


def techstack_section(rec: Audit, lang: str) -> s.TechSection:
    data = _load(rec, "techstack", TechStack)
    if isinstance(data, str):
        return s.TechSection(**_nm(lang, data))
    if not data.items:
        return s.TechSection(**_nm(lang, "tech-none-detected"))
    groups: dict[str, list[s.TechRow]] = {}
    for it in data.items:
        groups.setdefault(it.category, []).append(
            s.TechRow(
                name=_c(it.name, 80),
                version=_c(it.version, 40) if it.version else None,
                confidence=it.confidence,
                evidence=_c(it.evidence, 160),
                website=_c(it.website, 200) if it.website else None,
            )
        )
    order = [c for c in CATEGORY_ORDER if c in groups] + sorted(set(groups) - set(CATEGORY_ORDER))
    return s.TechSection(
        status="measured",
        groups=[s.TechGroup(category=c, label=_label(lang, "tech.cat", c), items=groups[c][:LIST_CAP]) for c in order],
        total=len(data.items),
        note=t(lang, "tech.note"),
    )


CATEGORY_ORDER = (
    "cms",
    "ecommerce",
    "framework",
    "js-library",
    "ui",
    "server",
    "hosting",
    "cdn",
    "analytics",
    "tag-manager",
    "ads",
    "security",
    "font",
    "other",
)


def _soft404(lang: str, d: dict[str, Any]) -> tuple[Literal["ok", "problem", "unknown"], str]:
    if not d:
        return "unknown", t(lang, "setup.soft404.unknown")
    if d.get("error") or d.get("detected") is None:
        return "unknown", t(lang, "setup.soft404.unknown")
    if d.get("detected") is True:
        return "problem", t(lang, "setup.soft404.problem", status=_c(d.get("final_status"), 10))
    return "ok", t(lang, "setup.soft404.ok", status=_c(d.get("final_status"), 10))


def site_setup_section(rec: Audit, lang: str) -> s.SiteSetupSection:
    data = _load(rec, "site_setup", SiteSetup)
    if isinstance(data, str):
        return s.SiteSetupSection(**_nm(lang, data))
    state, state_text = _soft404(lang, data.soft_404)
    c404 = (
        t(lang, "setup.custom404.unknown")
        if data.custom_404 is None
        else t(lang, "setup.custom404.yes" if data.custom_404 else "setup.custom404.no")
    )
    redirects = [
        s.RedirectRow(
            name=r.name,
            label=_label(lang, "setup.variant", r.name),
            variant_url=_c(r.variant_url, 300),
            status=r.status,
            final_url=_c(r.final_url, 300) if r.final_url else None,
            hops=r.hops,
            permanent=r.permanent,
            note=_label(lang, "reason", r.error.split(":")[0]) if r.error else None,
        )
        for r in data.redirects[:LIST_CAP]
    ]
    headers = [s.KV(label=_c(k, 60), value=_c(v, 200)) for k, v in data.headers.items() if v not in (None, "")][
        :LIST_CAP
    ]
    hsts = data.hsts or {}
    if hsts.get("present"):
        parts = [f"max-age={_c(hsts.get('max_age') if hsts.get('max_age') is not None else '?', 12)}"]
        if hsts.get("include_subdomains"):
            parts.append("includeSubDomains")
        if hsts.get("preload"):
            parts.append("preload")
        hsts_text = ", ".join(parts)
    else:
        hsts_text = t(lang, "setup.hsts.none")
    files = [
        s.FileRow(
            id=_c(k, 40),
            label=_label(lang, "setup.file", k),
            url=_c(p.url, 300),
            present=None if p.error else p.present,
            status=p.status,
            note=_c(p.detail, 160) if p.detail else (_c(p.error, 80) if p.error else None),
        )
        for k, p in list(data.files.items())[:LIST_CAP]
    ]
    return s.SiteSetupSection(
        status="measured",
        redirects=redirects,
        soft_404=state,
        soft_404_text=state_text,
        custom_404_text=c404,
        headers=headers,
        hsts_text=hsts_text,
        http_version_text=(
            t(lang, "setup.http.unknown") if data.http_version == "unknown" else _c(data.http_version, 20)
        ),
        files=files,
        skipped=[
            s.KV(label=_label(lang, "setup.variant", k), value=_label(lang, "reason", v))
            for k, v in data.skipped.items()
        ][:LIST_CAP],
        requests_made=data.requests_made,
    )


def screenshots_section(rec: Audit, lang: str) -> s.ScreenshotsSection:
    data = _load(rec, "screenshots", Screenshots)
    if isinstance(data, str):
        return s.ScreenshotsSection(**_nm(lang, _why(rec, "screenshots") if data == "suite-no-data" else data))
    shots = [
        s.ShotRow(
            device=f.device,
            device_label=_label(lang, "dev", f.device),
            name=f.name,
            width=f.width,
            height=f.height,
            kb=round(f.bytes / 1024, 1),
            data_base64=f.data_base64,
        )
        for f in data.files
        if f.name in SCREENSHOT_FILES
    ]
    if not shots:
        reason = "screenshots-failed" if data.errors else "suite-no-data"
        return s.ScreenshotsSection(**_nm(lang, reason))
    fold = [
        s.FoldRow(
            device=d,
            device_label=_label(lang, "dev", d),
            h1_present=a.h1_present,
            h1_visible=a.h1_visible,
            cta_visible=a.cta_visible,
            text_chars=a.text_chars,
            overflow=a.horizontal_overflow,
            overlay_pct=round(a.overlay_share * 100, 1),
        )
        for d, a in data.above_fold.items()
    ]
    return s.ScreenshotsSection(
        status="measured",
        shots=shots,
        above_fold=fold,
        errors=[s.KV(label=_label(lang, "dev", k), value=_c(v, 120)) for k, v in data.errors.items()],
    )


def agentic_section(rec: Audit, lang: str) -> s.AgenticSection:
    data = _load(rec, "agentic", AgenticReadiness)
    if isinstance(data, str):
        return s.AgenticSection(**_nm(lang, data))
    if not data.checks:
        return s.AgenticSection(**_nm(lang, "suite-no-data"))
    rows = [
        s.AgenticRow(
            id=c.id,
            label=_label(lang, "agentic.check", c.id),
            status=c.status,
            detail=_codes(lang, "agentic.detail", c.detail) if c.detail else "",
        )
        for c in data.checks
    ]
    return s.AgenticSection(
        status="measured",
        rows=rows,
        fraction=_c(data.fraction, 12),
        note=t(lang, "agentic.note"),
        notes=[_label(lang, "agentic.llms", n) for n in data.llms_txt_notes],
    )


SITEMAP_LIST_CAP = 20
XML_PREVIEW_URLS = 200


_BLOCK = re.compile(r"  <url>.*?</url>\n", re.S)


def _preview(xml: str) -> str:
    blocks = _BLOCK.findall(xml)
    head = '<?xml version="1.0" encoding="UTF-8"?>\n'
    m = re.search(r"<urlset[^>]*>\n", xml)
    open_tag = m.group(0) if m else "<urlset>\n"
    return clean_text(head + open_tag + "".join(blocks[:XML_PREVIEW_URLS]) + "</urlset>\n")


def sitemap_gen_section(rec: Audit, lang: str) -> s.SitemapGenSection:
    data = _load(rec, "sitemap_generated", SitemapGenerated)
    if isinstance(data, str):
        return s.SitemapGenSection(**_nm(lang, _why(rec, "sitemap_generated") if data == "suite-no-data" else data))
    facts = [
        s.KV(label=t(lang, "smg.fact.urls"), value=str(data.urls_total)),
        s.KV(label=t(lang, "smg.fact.lastmod"), value=str(data.with_lastmod)),
        s.KV(label=t(lang, "smg.fact.sample"), value=str(data.pages_in_sample)),
        s.KV(label=t(lang, "smg.fact.existing"), value=_yn(lang, data.existing_sitemap)),
    ]
    return s.SitemapGenSection(
        status="measured",
        facts=facts,
        xml_preview=_preview(data.xml),
        urls_total=data.urls_total,
        urls_shown=min(data.urls_total, XML_PREVIEW_URLS),
        missing=[_c(u, 300) for u in data.missing_from_sitemap[:SITEMAP_LIST_CAP]],
        missing_total=data.missing_total,
        not_indexable=[
            s.KV(label=_c(x.url, 300), value=_label(lang, "smg.reason", x.reason))
            for x in data.in_sitemap_not_indexable[:SITEMAP_LIST_CAP]
        ],
        not_indexable_total=data.in_sitemap_not_indexable_total,
        excluded=[
            s.KV(label=_c(x.url, 300), value=_label(lang, "smg.reason", x.reason))
            for x in data.excluded[:SITEMAP_LIST_CAP]
        ],
        excluded_total=data.excluded_total,
        note=t(lang, "smg.note"),
        truncated=data.truncated,
    )
