"""The SEO-relevant state of one page as JSON, and the severity-ranked difference of two of them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import SEVERITY_ORDER, Finding, Requirements, Severity
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage
from seoprobe.core.parse.urls import in_scope
from seoprobe.features.site import header_directives, meta_noindex

from .models import FieldChange, HreflangPair, Snapshot, SnapshotDiff

_NOINDEX = {"noindex", "none"}
_SIMPLE_FIELDS = ("final_url", "canonical", "title", "meta_description", "h1", "hreflang", "schema_types")


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("thresholds.json").read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def change_rules() -> dict[str, dict[str, str]]:
    """Change kind -> {"rule", "severity"} from ``diff-severity.json``."""
    data: dict[str, Any] = json.loads(files(__package__).joinpath("diff-severity.json").read_text(encoding="utf-8"))
    return dict(data["changes"])


def content_hash(text: str) -> str:
    """sha256 of the visible text, lower-cased with whitespace collapsed."""
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()


def _internal_links(parsed: ParsedPage, url: str) -> int:
    entry = urlparse(url).hostname or ""
    return sum(1 for link in parsed.links if link.resolved and in_scope(link.resolved, entry))


def build_snapshot(page: Page) -> Snapshot:
    """The snapshot of an already fetched page (parses it when needed; CPU only)."""
    parsed = page.parsed
    x_robots = page.headers.get("x-robots-tag", "")
    snap = Snapshot(
        url=page.url,
        final_url=page.effective_url,
        status=page.status,
        error=page.error,
        x_robots_tag=x_robots,
        taken_at=page.fetched_at or datetime.now(UTC),
    )
    snap.noindex = bool(header_directives(x_robots) & _NOINDEX) or meta_noindex(page)
    if parsed is None:
        return snap
    snap.title = parsed.title
    snap.meta_description = parsed.meta_description
    snap.canonical = parsed.canonical
    snap.robots_meta = [c.strip() for n, _p, c in parsed.meta_tags if n.lower() in ("robots", "googlebot")]
    snap.h1 = list(parsed.h1)
    snap.hreflang = [HreflangPair(lang=lang, href=href) for lang, href in sorted(set(parsed.hreflang))]
    snap.schema_types = sorted(set(parsed.schema_types))
    snap.content_hash = content_hash(parsed.visible_text)
    snap.word_count = parsed.word_count
    snap.internal_links = _internal_links(parsed, page.effective_url)
    return snap


@operation("snapshot", output=Snapshot, requires=Requirements(network=True), timeout_s=60.0)
async def snapshot(ctx: Context, url: str | Page) -> Outcome[Snapshot]:
    """
    Status, final URL, title, description, canonical, robots directives, H1s, hreflang, schema
    types, a content hash, word count and internal link count of one page.
    """
    page = await ctx.page(url)
    return Outcome(data=await asyncio.to_thread(build_snapshot, page))


# ------------------------------------------------------------------ diff


def _pct_change(old: int, new: int) -> float:
    if old == 0:
        return 0.0 if new == 0 else 100.0
    return (new - old) * 100.0 / old


def compare(
    old: Snapshot, new: Snapshot, *, word_count_drop_pct: float, internal_links_change_pct: float
) -> list[FieldChange]:
    """Field changes between two snapshots, most severe first (stable within a severity)."""
    rules = change_rules()
    out: list[FieldChange] = []

    def add(kind: str, field: str, a: Any, b: Any) -> None:
        r = rules[kind]
        out.append(FieldChange(field=field, old=a, new=b, severity=Severity(r["severity"]), rule=r["rule"]))

    if old.noindex != new.noindex:
        add("noindex_added" if new.noindex else "noindex_removed", "noindex", old.noindex, new.noindex)
    if old.status != new.status:
        error = (new.status is None or new.status >= 400) and not (old.status is None or old.status >= 400)
        add("status_error" if error else "status", "status", old.status, new.status)
    for field in _SIMPLE_FIELDS:
        a, b = getattr(old, field), getattr(new, field)
        if a != b:
            if field == "hreflang":
                a, b = [p.model_dump() for p in a], [p.model_dump() for p in b]
            add(field, field, a, b)
    if (old.robots_meta, old.x_robots_tag) != (new.robots_meta, new.x_robots_tag):
        add(
            "robots",
            "robots",
            {"meta": old.robots_meta, "header": old.x_robots_tag},
            {"meta": new.robots_meta, "header": new.x_robots_tag},
        )
    if -_pct_change(old.word_count, new.word_count) >= word_count_drop_pct:
        add("word_count", "word_count", old.word_count, new.word_count)
    if abs(_pct_change(old.internal_links, new.internal_links)) >= internal_links_change_pct:
        add("internal_links", "internal_links", old.internal_links, new.internal_links)
    if old.content_hash != new.content_hash:
        add("content_hash", "content_hash", old.content_hash, new.content_hash)
    rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    return sorted(out, key=lambda c: rank[c.severity])


def _short(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


@operation("diff", output=SnapshotDiff, requires=Requirements(network=False), timeout_s=10.0)
async def diff(
    ctx: Context,
    old: Snapshot,
    new: Snapshot,
    *,
    word_count_drop_pct: float | None = None,
    internal_links_change_pct: float | None = None,
) -> Outcome[SnapshotDiff]:
    """The changes between two snapshots of a page, each with a severity (no network access)."""
    d = _defaults()["diff"]
    thresholds = {
        "word_count_drop_pct": float(d["word_count_drop_pct"] if word_count_drop_pct is None else word_count_drop_pct),
        "internal_links_change_pct": float(
            d["internal_links_change_pct"] if internal_links_change_pct is None else internal_links_change_pct
        ),
    }
    a, b = Snapshot.model_validate(old), Snapshot.model_validate(new)
    changes = compare(a, b, **thresholds)
    findings: list[Finding] = []
    for c in changes:
        evidence = f"{c.field}: {_short(c.old)} -> {_short(c.new)}"
        if c.field == "word_count":
            evidence += f" (drop threshold {thresholds['word_count_drop_pct']:g}%)"
        elif c.field == "internal_links":
            evidence += f" (change threshold {thresholds['internal_links_change_pct']:g}%)"
        findings.append(ctx.finding(c.rule, url=b.url, evidence=evidence))
    data = SnapshotDiff(
        url=b.url,
        old_taken_at=a.taken_at,
        new_taken_at=b.taken_at,
        changed=bool(changes),
        changes=changes,
        max_severity=changes[0].severity if changes else None,
        thresholds=thresholds,
    )
    return Outcome(data=data, findings=findings)
