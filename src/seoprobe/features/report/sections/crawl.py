"""Report sections built from the crawl: links, indexability, visibility signals and hreflang."""

from __future__ import annotations

from seoprobe.core.rules.catalog import has_text, t
from seoprobe.features.audit import Audit
from seoprobe.features.crawl import HreflangReport, LinkRecord, classify_link

from .. import models as s
from .contract import _c, _code, _day, _label, _load, _nm, _vis_error, _why, not_measured

# Row caps: the JSON report stays a sensible size; the API serves everything that was stored.
LINK_ROWS = 200


BROKEN_ROWS = 200


REDIRECT_ROWS = 200


DOMAINS = 15


PAGE_ROWS = 200


INDEX_ROWS = 200


ORPHAN_ROWS = 100


LIST_CAP = 30


HREFLANG_ROWS = 100


def _link_state(lk: LinkRecord) -> str:
    """Same verdicts as the analysis (a site that blocks bots, e.g. LinkedIn's 999, is not "broken")."""
    verdict = classify_link(lk.target_status, lk.error)
    if verdict == "broken":
        return "broken"
    if verdict == "blocked":
        return "blocked"
    if verdict in ("rejected", "unchecked"):
        return "unchecked"
    if lk.redirect_hops > 0 or (lk.target_status is not None and 300 <= lk.target_status < 400):
        return "redirect"
    return "ok" if lk.checked else "unchecked"


def _link_error_text(lang: str, err: str | None) -> str | None:
    if not err:
        return None
    code = _code(err) or "unknown"
    key = f"link.err.{code}"
    return t(lang, key) if has_text(key) else t(lang, "link.err.unknown")


def _link_row(lk: LinkRecord, lang: str) -> s.LinkRow:
    return s.LinkRow(
        source_url=_c(lk.source_url, 2048),
        target_url=_c(lk.target_url, 2048),
        anchor=_c(lk.anchor, 120),
        kind=lk.kind,
        rel=[_c(r, 20) for r in lk.rel[:5]],
        zone=lk.zone,
        target_status=lk.target_status,
        final_url=_c(lk.final_url, 2048) if lk.final_url else None,
        redirect_hops=lk.redirect_hops,
        error=_code(lk.error),
        error_text=_link_error_text(lang, lk.error),
        state=_link_state(lk),  # type: ignore[arg-type]
    )


def links_section(rec: Audit, lang: str) -> s.LinksSection:
    ls = rec.links
    if ls is None:
        return s.LinksSection(**not_measured(lang, "analysis-not-run"))
    rows = [_link_row(lk, lang) for lk in ls.links]
    broken_all = [r for r in rows if r.state == "broken"]
    redirect_all = [r for r in rows if r.state == "redirect"]
    pages_all = sorted(ls.pages, key=lambda p: (p.depth is None, p.depth or 0, p.url))
    pages = [
        s.LinkPageRow(
            url=_c(p.url, 2048),
            inlinks=p.inlinks,
            outlinks_internal=p.outlinks_internal,
            outlinks_external=p.outlinks_external,
            depth=p.depth,
            in_sitemap=p.in_sitemap,
        )
        for p in pages_all[:PAGE_ROWS]
    ]
    domains = [
        s.DomainCount(domain=_c(d.get("domain", ""), 253), count=int(d.get("count", 0)))
        for d in sorted(ls.external_domains, key=lambda d: -int(d.get("count", 0)))[:DOMAINS]
        if d.get("domain")
    ]
    summary = {
        "internal_total": ls.internal_total,
        "external_total": ls.external_total,
        "internal_unique": ls.internal_unique,
        "external_unique": ls.external_unique,
        "checked": ls.checked,
        "skipped": ls.skipped,
        "broken": ls.broken,
        "redirecting": ls.redirecting,
        "nofollow": ls.nofollow,
        "max_depth": ls.max_depth,
        "orphans": len(ls.orphans),
    }
    total = max(len(rows), ls.internal_total + ls.external_total)
    shown = rows[:LINK_ROWS]
    return s.LinksSection(
        status="measured",
        summary=summary,
        broken=broken_all[:BROKEN_ROWS],
        redirecting=redirect_all[:REDIRECT_ROWS],
        external_domains=domains,
        pages=pages,
        pages_total=len(ls.pages),
        orphans=[_c(u, 2048) for u in ls.orphans[:ORPHAN_ROWS]],
        links=shown,
        links_total=total,
        truncated=(
            ls.truncated
            or total > len(shown)
            or len(broken_all) > BROKEN_ROWS
            or len(redirect_all) > REDIRECT_ROWS
            or len(ls.pages) > PAGE_ROWS
        ),
    )


def _reason_text(lang: str, code: str) -> str:
    key = f"idx.reason.{code}"
    return t(lang, key) if has_text(key) else _c(code, 60)


def indexability_section(rec: Audit, lang: str) -> s.IndexabilitySection:
    if not rec.indexability:
        return s.IndexabilitySection(**not_measured(lang, "analysis-not-run"))
    rows = [
        s.IndexRow(
            url=_c(r.url, 2048),
            status=r.status,
            indexable=r.indexable,
            reasons=[_c(x, 60) for x in r.reasons[:8]],
            reason_texts=[_reason_text(lang, x) for x in r.reasons[:8]],
            canonical=_c(r.canonical, 2048) if r.canonical else None,
            in_sitemap=r.in_sitemap,
            inlinks=r.inlinks,
            depth=r.depth,
        )
        for r in sorted(rec.indexability, key=lambda r: (r.indexable, r.url))[:INDEX_ROWS]
    ]
    blocked = sum(1 for r in rec.indexability if not r.indexable)
    return s.IndexabilitySection(
        status="measured",
        rows=rows,
        rows_total=len(rec.indexability),
        indexable_count=len(rec.indexability) - blocked,
        blocked_count=blocked,
        note=t(lang, "idx.note"),
        truncated=len(rec.indexability) > INDEX_ROWS,
    )


def visibility_section(rec: Audit, lang: str) -> s.VisibilitySection:
    v = rec.visibility
    if v is None:
        return s.VisibilitySection(**not_measured(lang, "visibility-unavailable"))
    items: list[s.VisItem] = []

    r = v.rdap or {}
    if r.get("error") or not (r.get("created") or r.get("age_days") is not None):
        items.append(
            s.VisItem(
                id="rdap",
                label=t(lang, "vis.rdap"),
                available=False,
                error_text=_vis_error(lang, r.get("error") or "unknown"),
            )
        )
    else:
        detail = t(lang, "vis.registrar", name=_c(r["registrar"], 80)) if r.get("registrar") else None
        items.append(
            s.VisItem(
                id="rdap",
                label=t(lang, "vis.rdap"),
                value=t(
                    lang,
                    "vis.created",
                    date=_day(r.get("created") or "—"),
                    days=r.get("age_days", "—"),
                ),
                detail=detail,
            )
        )

    w = v.wayback or {}
    # a partial answer (the dates without the URL sample, or without the last capture) is shown
    if not (w.get("first_capture") or w.get("captures")):
        items.append(
            s.VisItem(
                id="wayback",
                label=t(lang, "vis.wayback"),
                available=False,
                error_text=_vis_error(lang, w.get("error") or "no-captures"),
            )
        )
    else:
        items.append(
            s.VisItem(
                id="wayback",
                label=t(lang, "vis.wayback"),
                value=t(
                    lang,
                    "vis.wb.first",
                    first=_day(w.get("first_capture") or "—"),
                    last=_day(w.get("last_capture") or "—"),
                ),
                detail=t(lang, "vis.wb.count", n=w["captures"]) if w.get("captures") is not None else None,
            )
        )

    cc = v.common_crawl or {}
    if cc.get("error") or cc.get("present") is None:
        items.append(
            s.VisItem(
                id="common_crawl",
                label=t(lang, "vis.common_crawl"),
                available=False,
                error_text=_vis_error(lang, cc.get("error") or "unknown"),
            )
        )
    else:
        coll = _c(cc.get("collection") or "—", 40)
        items.append(
            s.VisItem(
                id="common_crawl",
                label=t(lang, "vis.common_crawl"),
                value=(
                    t(lang, "vis.cc.present", collection=coll, pages=cc.get("pages_seen", "—"))
                    if cc["present"]
                    else t(lang, "vis.cc.absent", collection=coll)
                ),
            )
        )

    if rec.gsc is not None and rec.gsc.status == "ok":
        items.append(
            s.VisItem(
                id="search_console",
                label=t(lang, "vis.search_console"),
                value=t(lang, "vis.gsc.on"),
                detail=t(lang, "vis.gsc.see"),
            )
        )
    else:
        items.append(
            s.VisItem(
                id="search_console",
                label=t(lang, "vis.search_console"),
                value=t(lang, "vis.gsc.off"),
                detail=t(lang, "vis.gsc.how"),
                available=False,
            )
        )
    notes = []
    for code in v.notes[:12]:
        if code == "gsc-not-connected":  # shown by the Search Console row, from the real state
            continue
        key = f"vis.note.{_c(code, 60)}"
        notes.append(t(lang, key) if has_text(key) else t(lang, "vis.note.unknown", code=_c(code, 60)))
    return s.VisibilitySection(
        status="measured",
        domain=_c(v.domain, 253),
        items=items,
        notes=notes,
        disclaimer=t(lang, "vis.disclaimer"),
    )


def hreflang_section(rec: Audit, lang: str) -> s.HreflangSection:
    data = _load(rec, "hreflang", HreflangReport)
    if isinstance(data, str):
        return s.HreflangSection(**_nm(lang, _why(rec, "hreflang") if data == "suite-no-data" else data))
    if not data.applicable or not data.pages:
        return s.HreflangSection(
            **_nm(lang, "hreflang-not-applicable"),
            notes=[_label(lang, "hreflang.note", n) for n in data.notes],
        )
    rows = [
        s.HreflangRow(
            url=_c(p.url, 300),
            html_lang=_c(p.html_lang, 20),
            canonical=_c(p.canonical, 300) if p.canonical else None,
            entries=[
                s.KV(label=_c(e.lang, 20) + ("" if e.valid else " ⚠"), value=_c(e.href, 300))
                for e in p.entries[:LIST_CAP]
            ],
            issues=[_label(lang, "hreflang.issue", i) for i in p.issues],
        )
        for p in data.pages[:HREFLANG_ROWS]
    ]
    return s.HreflangSection(
        status="measured",
        languages=[_c(x, 20) for x in data.languages[:LIST_CAP]],
        has_x_default=data.has_x_default,
        rows=rows,
        rows_total=len(data.pages),
        unchecked=data.unchecked,
        notes=[_label(lang, "hreflang.note", n) for n in data.notes],
    )
