"""
Internal link suggestions: for pages that receive few internal links, the sampled pages on the
closest topic that could link to them, with anchor text candidates.

* Targets are the given URLs, else every sampled page whose inlinks (internal links from other
  sampled pages, the link graph's counting) are at most ``max_inlinks``. The first page is taken
  as the entry page for "internal" (as in ``CrawlReport.pages``).
* Similarity is the cosine of TF-IDF vectors (unigrams and bigrams) over the main text
  (``ParsedPage.main_text``, falling back to ``visible_text``), folded like the duplicate detector
  and without the stopwords of the page language (``seoprobe.core`` ``data/stopwords.json``; a page
  without a known language uses every list). Pages with fewer than ``min_words`` tokens are not
  compared.
* A source that already links to the target, or is the target, is never suggested. Ties are
  broken by URL, so the same pages give the same suggestions.
* Anchor candidates: the target's title and first H1, then the strongest shared terms (bigrams
  first); every shared term occurs in the source text.

Pure computation, no network. The similarity threshold is a heuristic, not a search engine rule.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.urls import url_key
from seoprobe.core.rules.catalog import make_finding

from .duplicates import fold, words
from .links.graph import _collect
from .models import LinkSuggestion, LinkSuggestions, LinkSummary, LinkTarget

RULE_ID = "internal-link-opportunity"


@lru_cache(maxsize=1)
def defaults() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("suggest-links.json").read_text("utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _stopwords() -> dict[str, frozenset[str]]:
    raw = json.loads(files("seoprobe.core").joinpath("data/stopwords.json").read_text("utf-8"))
    out = {lang: frozenset(fold(w) for w in ws) for lang, ws in raw.items() if not lang.startswith("_")}
    out[""] = frozenset().union(*out.values())
    return out


def stopwords_for(lang: str) -> frozenset[str]:
    sw = _stopwords()
    primary = lang.strip().lower().replace("_", "-").split("-")[0]
    return sw.get(primary, sw[""])


def terms(text: str, lang: str, *, min_token_length: int) -> tuple[Counter[str], int]:
    """(unigram + bigram counts, number of kept tokens). A bigram joins two adjacent kept tokens."""
    stop = stopwords_for(lang)
    counts: Counter[str] = Counter()
    kept = 0
    prev: str | None = None
    for tok in words(text):
        if len(tok) < min_token_length or tok.isdigit() or tok in stop:
            prev = None
            continue
        kept += 1
        counts[tok] += 1
        if prev is not None:
            counts[f"{prev} {tok}"] += 1
        prev = tok
    return counts, kept


def _vectors(docs: dict[str, Counter[str]], max_terms: int) -> dict[str, dict[str, float]]:
    """L2-normalised TF-IDF vectors (smoothed idf), each cut to its ``max_terms`` strongest terms."""
    n = len(docs)
    df: Counter[str] = Counter()
    for c in docs.values():
        df.update(c.keys())
    out: dict[str, dict[str, float]] = {}
    for url, c in docs.items():
        total = sum(c.values()) or 1
        w = {t: (k / total) * (math.log((1 + n) / (1 + df[t])) + 1.0) for t, k in c.items()}
        top = sorted(w.items(), key=lambda kv: (-kv[1], kv[0]))[:max_terms]
        norm = math.sqrt(sum(v * v for _, v in top)) or 1.0
        out[url] = {t: v / norm for t, v in top}
    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b[t] for t, v in a.items() if t in b)


@dataclass
class _Graph:
    inlinks: dict[str, int]  # page url -> distinct sampled sources linking to it
    links_to: dict[str, set[str]]  # page url -> url keys it links to (internal)


def _graph(pages: list[Page]) -> _Graph:
    """Inlinks and outgoing internal link keys with the link graph's normalisation."""
    entry_host = (urlparse(pages[0].effective_url).hostname or "") if pages else ""
    col = _collect(pages, entry_host)
    key_to_page: dict[str, str] = {}
    for p in pages:
        for k in (url_key(p.url), url_key(p.effective_url)):
            key_to_page.setdefault(k, p.url)
    sources: dict[str, set[str]] = {p.url: set() for p in pages}
    links_to: dict[str, set[str]] = {p.url: set() for p in pages}
    for raw in col.raws:
        if raw.kind != "internal" or raw.self_link:
            continue
        links_to[raw.source].add(raw.key)
        tgt = key_to_page.get(raw.key)
        if tgt is not None and tgt != raw.source:
            sources[tgt].add(raw.source)
    return _Graph({u: len(s) for u, s in sources.items()}, links_to)


def _anchors(target: Page, shared: list[str], *, limit: int, max_len: int) -> list[str]:
    out: list[str] = []
    parsed = target.parsed
    heads = [parsed.title, parsed.h1[0] if parsed.h1 else ""] if parsed is not None else []
    ranked = [t for t in shared if " " in t] + [t for t in shared if " " not in t]
    for cand in [*heads, *ranked]:
        cand = " ".join(cand.split())[:max_len]
        if cand and cand.casefold() not in {c.casefold() for c in out}:
            out.append(cand)
        if len(out) >= limit:
            break
    return out


def build_suggestions(
    pages: list[Page],
    *,
    targets: list[str] | None,
    links: LinkSummary | None,
    params: dict[str, Any],
) -> tuple[list[LinkTarget], int, list[str]]:
    """(target rows, pages compared, unknown targets). CPU-bound; run it in a thread."""
    graph = _graph(pages)
    if links is not None:
        given = {s.url: s.inlinks for s in links.pages}
        given_by_key = {url_key(s.url): s.inlinks for s in links.pages}
        inlinks = {p.url: given.get(p.url, given_by_key.get(url_key(p.url), graph.inlinks[p.url])) for p in pages}
    else:
        inlinks = graph.inlinks

    healthy: dict[str, Page] = {}
    docs: dict[str, Counter[str]] = {}
    for p in pages:
        parsed = p.parsed
        if parsed is None or p.error or p.status is None or p.status >= 400:
            continue
        key = url_key(p.effective_url)
        if key in healthy:  # two sampled URLs that ended on the same page
            continue
        healthy[key] = p
        text = parsed.main_text or parsed.visible_text
        counts, kept = terms(text, parsed.lang, min_token_length=params["min_token_length"])
        if kept >= params["min_words"]:
            docs[p.url] = counts
    vectors = _vectors(docs, params["max_terms_per_page"])
    by_key: dict[str, Page] = {}
    for p in pages:
        for k in (url_key(p.url), url_key(p.effective_url)):
            by_key.setdefault(k, p)

    unknown: list[str] = []
    chosen: list[Page] = []
    if targets is not None:
        for t in targets:
            hit = by_key.get(url_key(t))
            if hit is None:
                unknown.append(t)
            elif all(c.url != hit.url for c in chosen):
                chosen.append(hit)
    else:
        chosen = [p for p in healthy.values() if inlinks[p.url] <= params["max_inlinks"]]

    rows: list[LinkTarget] = []
    for tp in chosen:
        row = LinkTarget(url=tp.url, inlinks=inlinks[tp.url])
        rows.append(row)
        tv = vectors.get(tp.url)
        if tv is None:
            continue
        own = {url_key(tp.url), url_key(tp.effective_url)}
        scored: list[tuple[float, str]] = []
        for src, sv in vectors.items():
            if src == tp.url or graph.links_to.get(src, set()) & own:
                continue
            sim = _cosine(sv, tv)
            if sim >= params["min_similarity"]:
                scored.append((sim, src))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for sim, src in scored[: params["max_suggestions"]]:
            sv = vectors[src]
            shared = sorted((t for t in sv if t in tv), key=lambda t: (-(sv[t] * tv[t]), t))
            shared = shared[: params["shared_terms"]]
            row.suggestions.append(
                LinkSuggestion(
                    source_url=src,
                    similarity=round(sim, params["similarity_digits"]),
                    anchor_candidates=_anchors(
                        tp, shared, limit=params["max_anchor_candidates"], max_len=params["max_anchor_length"]
                    ),
                    shared_terms=shared,
                )
            )
    return rows, len(docs), unknown


@operation("suggest_links", output=LinkSuggestions, requires=Requirements(network=False), timeout_s=120.0)
async def suggest_links(
    ctx: Context,
    pages: list[Page],
    *,
    targets: list[str] | None = None,
    links: LinkSummary | None = None,
    max_inlinks: int | None = None,
    max_suggestions: int | None = None,
    min_similarity: float | None = None,
) -> Outcome[LinkSuggestions]:
    """Topic-close source pages and anchor candidates for pages with few internal links (no network)."""
    params = dict(defaults())
    for name, value in (
        ("max_inlinks", max_inlinks),
        ("max_suggestions", max_suggestions),
        ("min_similarity", min_similarity),
    ):
        if value is not None:
            params[name] = value
    cap = ctx.config.max_pages_cap
    used = pages[:cap]
    targets_skipped = 0
    if targets is not None and len(targets) > ctx.config.max_batch_urls:
        targets_skipped = len(targets) - ctx.config.max_batch_urls
        targets = targets[: ctx.config.max_batch_urls]
    rows, compared, unknown = await asyncio.to_thread(
        build_suggestions, used, targets=targets, links=links, params=params
    )
    findings: list[Finding] = []
    for row in rows:
        if not row.suggestions:
            continue
        best = row.suggestions[0]
        findings.append(
            make_finding(
                RULE_ID,
                url=row.url,
                count=len(row.suggestions),
                evidence=(
                    f"{row.inlinks} inlinks (max {params['max_inlinks']}); best source {best.source_url}"
                    f" similarity {best.similarity} (min {params['min_similarity']})"
                )[:200],
            )
        )
    data = LinkSuggestions(
        targets=rows,
        thresholds={k: params[k] for k in ("max_inlinks", "max_suggestions", "min_similarity", "min_words")},
        pages_considered=compared,
        pages_skipped=len(pages) - len(used),
        targets_skipped=targets_skipped,
        unknown_targets=unknown,
    )
    return Outcome(data=data, findings=findings)
