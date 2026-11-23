"""Report sections for off-site data: domain authority and the competitor comparison."""

from __future__ import annotations

from seoprobe.core.rules.catalog import t
from seoprobe.features.audit import Audit
from seoprobe.features.offsite import AuthoritySignal, CompetitorComparison, CompetitorRow

from .. import models as s
from .contract import _c, _load, _nm, _why, not_measured


def authority_section(rec: Audit, lang: str) -> s.AuthoritySection:
    data = _load(rec, "authority", AuthoritySignal)
    if isinstance(data, str):
        return s.AuthoritySection(**_nm(lang, _why(rec, "authority") if data == "suite-no-data" else data))
    items = [s.KV(label=t(lang, "auth.domain"), value=_c(data.domain, 120))]
    if data.page_rank_decimal is not None:
        items.append(s.KV(label=t(lang, "auth.pr"), value=f"{data.page_rank_decimal:.2f} / 10"))
    if data.rank is not None:
        items.append(s.KV(label=t(lang, "auth.rank"), value=f"{data.rank:,}".replace(",", ".")))
    return s.AuthoritySection(status="measured", items=items, note=t(lang, "auth.note"))


def _ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{round(v)} ms"


def _kb(v: float | None) -> str:
    return "—" if v is None else f"{round(v / 1024):,} KB".replace(",", ".")


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:g} %"


def _opt(v: object) -> str:
    return "—" if v is None else _c(v, 120)


_METRICS = (
    "score",
    "status",
    "title_len",
    "meta_len",
    "h1_count",
    "words",
    "schema",
    "images",
    "alt_pct",
    "canonical",
    "noindex",
    "ttfb",
    "html_kb",
    "links",
    "readability",
    "tech",
)


def competitors_section(rec: Audit, lang: str) -> s.CompetitorsSection:
    data = _load(rec, "competitors", CompetitorComparison)
    if isinstance(data, str):
        return s.CompetitorsSection(**not_measured(lang, _why(rec, "competitors") if data == "suite-no-data" else data))
    if not data.rows:
        return s.CompetitorsSection(**not_measured(lang, _why(rec, "competitors")))

    def yn(v: bool | None) -> str:
        return "—" if v is None else t(lang, "idx.yes" if v else "idx.no")

    def values(r: CompetitorRow) -> list[str]:
        canon = {
            "self": t(lang, "comp.canon.self"),
            "other": t(lang, "comp.canon.other"),
            "none": t(lang, "comp.canon.none"),
        }
        links = "—" if r.internal_links is None else f"{r.internal_links} / {r.external_links}"
        read = (
            "—"
            if r.readability_score is None
            else f"{r.readability_score:g} ({t(lang, 'comp.formula.' + str(r.readability_formula))})"
        )
        return [
            _opt(r.score),
            _opt(r.status),
            _opt(r.title_len),
            _opt(r.meta_len),
            _opt(r.h1_count),
            _opt(r.words),
            ", ".join(_c(x, 40) for x in r.schema_types) or "—",
            _opt(r.images),
            _pct(r.images_alt_pct),
            canon.get(r.canonical or "", "—"),
            yn(r.noindex),
            _ms(r.ttfb_ms),
            _kb(r.html_bytes),
            links,
            read,
            ", ".join(_c(x, 40) for x in r.tech) or "—",
        ]

    def column(r: CompetitorRow) -> s.CompetitorColumn:
        note = None
        if r.error:
            note = t(lang, f"comp.err.{r.error}") if r.error in _KNOWN_ERRORS else _c(r.error, 60)
        elif r.title_len is None:
            note = t(lang, "comp.err.no-data")
        return s.CompetitorColumn(
            host=_c(r.host or r.url, 120),
            url=_c(r.final_url or r.url),
            is_you=r.is_you,
            note=note,
            values=values(r),
        )

    columns = ([column(data.you)] if data.you else []) + [column(r) for r in data.rows]
    skipped = [s.KV(label=_c(x.url, 200), value=t(lang, f"comp.skip.{x.reason}")) for x in data.skipped]
    return s.CompetitorsSection(
        status="measured",
        metrics=[t(lang, f"comp.m.{m}") for m in _METRICS],
        columns=columns,
        skipped=skipped,
        note=t(lang, "comp.note"),
    )


_KNOWN_ERRORS = ("target-rejected", "timeout", "unreachable", "dns-failed", "not-html")
