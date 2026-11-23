"""
Sections assembled from the others: the one-look summary and the "what this score does not
measure" box (only items this audit really left unmeasured).
"""

from __future__ import annotations

from seoprobe.core.rules.catalog import t
from seoprobe.features.audit import Audit
from seoprobe.features.content import ReadabilityData

from .. import models as s
from .contract import _load
from .offsite import competitors_section
from .search import rankings_section


def _ms_text(v: float | int | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{round(v)} ms"


def glance_section(rec: Audit, lang: str, tech: s.TechSection) -> s.GlanceSection:
    items: list[s.KV] = []
    sc = rec.scores
    if sc is not None:
        items.append(
            s.KV(
                label=t(lang, "glance.score"),
                value=f"{sc.overall:.0f} / 100" if sc.overall is not None else t(lang, "glance.insufficient"),
            )
        )
    perf = next((p for p in rec.perf if not p.error), None)
    if perf is not None:
        items.append(s.KV(label=t(lang, "glance.lcp"), value=_ms_text(perf.lcp_ms)))
        items.append(s.KV(label=t(lang, "glance.ttfb"), value=_ms_text(perf.ttfb_ms)))
        items.append(
            s.KV(
                label=t(lang, "glance.weight"),
                value=f"{round(perf.total_bytes / 1024):,} KB".replace(",", "."),
            )
        )
        items.append(s.KV(label=t(lang, "glance.requests"), value=str(perf.total_requests)))
    if rec.indexability:
        ok = sum(1 for r in rec.indexability if r.indexable)
        items.append(s.KV(label=t(lang, "glance.indexable"), value=f"{ok} / {len(rec.indexability)}"))
    if rec.links is not None:
        lk = rec.links
        items.append(s.KV(label=t(lang, "glance.links"), value=f"{lk.internal_total} / {lk.external_total}"))
        items.append(s.KV(label=t(lang, "glance.broken"), value=str(lk.broken)))
    if tech.status == "measured":
        names = [i.name for g in tech.groups[:4] for i in g.items[:1]]
        if names:
            items.append(s.KV(label=t(lang, "glance.tech"), value=", ".join(names)))
    counts = {"CRITICAL": 0, "HIGH": 0}
    for f in rec.findings:
        if f.severity.value in counts:
            counts[f.severity.value] += 1
    items.append(s.KV(label=t(lang, "glance.issues"), value=f"{counts['CRITICAL']} / {counts['HIGH']}"))
    return s.GlanceSection(status="measured", items=items)


def _measured_scope_items(rec: Audit) -> set[int]:
    """Scope items 2 (rankings), 4 (keyword targeting) and 6 (rivals), when their data exists."""
    done: set[int] = set()
    if rankings_section(rec, "en").status == "measured":
        done.add(2)
    readability = _load(rec, "readability", ReadabilityData)
    if not isinstance(readability, str) and readability.keywords:
        done.add(4)
    if competitors_section(rec, "en").status == "measured":
        done.add(6)
    return done


def scope_note(lang: str, rec: Audit | None = None) -> s.ScopeNote:
    """Items that the audit *did* measure (indexing, rankings, keywords, field data, rivals) go."""
    gsc_ok = rec is not None and rec.gsc is not None and rec.gsc.status == "ok"
    field_ok = rec is not None and rec.field is not None and bool(rec.field.metrics)
    skip = ({1} if gsc_ok else set()) | ({5} if field_ok else set())
    if rec is not None:
        skip |= _measured_scope_items(rec)
    return s.ScopeNote(
        title=t(lang, "scope.title"),
        intro=t(lang, "scope.intro"),
        items=[t(lang, f"scope.i{i}") for i in range(1, 7) if i not in skip],
        next_step=t(lang, "scope.next.connected" if gsc_ok else "scope.next"),
        model_note=t(lang, "scope.model"),
    )
