"""
SPA detection, raw-vs-rendered comparison ("JS gap") and the ``render`` operation.

``is_spa`` is a faithful port of the upstream project ``scripts/render_page.py::_is_spa`` (MIT). The heuristic is deliberately conservative: an unnecessary render is expensive, so
SSR/SSG pages that keep their framework root markers but ship real text are NOT flagged.

``js_gap`` ports ``_gap_signals``, ``_only_after``, ``js_gap`` and ``js_gap_reasons`` of the same
script. Pure: two HTML strings in, a JSON-ready model out. The limits are this tool's
heuristics, not Google rules.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.page import Page

from .models import GapDiff, GapSignals, JsGap, RenderReport

# --------------------------------------------------------------------------- SPA detection

# A marker only counts while meaningful body text is sparse (< BUILDER_SPARSE_TEXT_MAX).
SPA_SHELL_PATTERNS: tuple[str, ...] = (
    '<div id="root"></div>',
    '<div id="__next">',
    '<div id="app"></div>',
    '<div id="__nuxt">',
    "data-svelte-h=",
    "<astro-island ",
    "you need to enable javascript",
    "please enable javascript",
)

# Builder markers are supporting evidence only: Wix, Webflow and Squarespace can all
# serve complete HTML, so two markers of the SAME builder plus sparse text are required.
BUILDER_FINGERPRINT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("wix-warmup-data", "static.parastorage.com", 'content="wix.com'),
    ("data-wf-page", "data-wf-site"),
    ('content="squarespace', "static1.squarespace.com"),
)

BUILDER_SPARSE_TEXT_MAX = 400
THIN_BODY_TEXT_MAX = 100  # example.com has ~125 chars of body text and must not trip this

_TAG_STRIP = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_NON_VISIBLE_STRIP = re.compile(
    r"<(script|style|template|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def visible_body_text(lower_html: str) -> str:
    """
    Whitespace-collapsed text of ``<body>`` without script/style/template/noscript.

    ``lower_html`` must already be lower-cased (as the source does); returns ``""``
    when there is no well-formed ``<body>...</body>``.
    """
    start = lower_html.find("<body")
    end = lower_html.rfind("</body>")
    if start == -1 or end <= start:
        return ""
    body = _NON_VISIBLE_STRIP.sub(" ", lower_html[start:end])
    return _WHITESPACE.sub(" ", _TAG_STRIP.sub(" ", body)).strip()


def is_spa(raw_html: str | None) -> bool:
    """True when ``raw_html`` looks like a sparse client-side hydration shell."""
    if not raw_html:
        return True
    lc = raw_html.lower()
    text = visible_body_text(lc)
    if len(text) < BUILDER_SPARSE_TEXT_MAX and any(p in lc for p in SPA_SHELL_PATTERNS):
        return True
    if len(text) < BUILDER_SPARSE_TEXT_MAX:
        for markers in BUILDER_FINGERPRINT_GROUPS:
            if sum(marker in lc for marker in markers) >= 2:
                return True
    # Very thin <body> suggests JS-rendered content even without a known shell.
    start = lc.find("<body")
    end = lc.rfind("</body>")
    return start != -1 and end > start and len(text) < THIN_BODY_TEXT_MAX


# --------------------------------------------------------------------------- JS gap

JS_GAP_WORDS_SHARE = 0.30  # share of rendered words that exist only after JS ...
JS_GAP_WORDS_MIN = 25  # ... and at least this many such words
JS_GAP_LINKS_SHARE = 0.30
JS_GAP_LINKS_MIN = 3
# Output bounds, so a hostile page cannot inflate the JSON.
MAX_ITEMS = 12
MAX_CANONICALS = 5
MAX_TEXT = 300
MAX_NAME = 80
REASON_TEXT = 80
REASON_ITEMS = 5
# Past this size the comparison is skipped (avoids stalling on a huge DOM).
MAX_HTML_CHARS = 5_000_000

_JSON_LD_MAX_NODES = 10_000
_JSON_LD_MAX_DEPTH = 40
_JSON_LD_MAX_BLOCKS = 50
_JSON_LD_MAX_BLOCK_BYTES = 256 * 1024

_WORD = re.compile(r"\w+")
_WS = re.compile(r"\s+")
_NON_VISIBLE_TAGS = ("script", "style", "noscript", "template", "head", "title")
_NON_LINK_HREF = ("#", "javascript:", "mailto:", "tel:", "data:")


def _clean(value: object, limit: int = MAX_TEXT) -> str:
    return _WS.sub(" ", str(value or "")).strip()[:limit]


def _schema_types(data: object) -> list[str]:
    """Bounded, non-recursive collection of ``@type`` values."""
    types: set[str] = set()
    stack: list[tuple[object, int]] = [(data, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > _JSON_LD_MAX_NODES or depth > _JSON_LD_MAX_DEPTH:
            break
        if isinstance(value, dict):
            t = value.get("@type")
            if isinstance(t, str):
                types.add(t)
            elif isinstance(t, list):
                types.update(i for i in t if isinstance(i, str))
            stack.extend((i, depth + 1) for i in value.values())
        elif isinstance(value, list):
            stack.extend((i, depth + 1) for i in value)
    return sorted(types)[:100]


def _json_ld_types(soup: BeautifulSoup) -> dict[str, None]:
    out: dict[str, None] = {}
    scripts = [s for s in soup.find_all("script") if str(s.get("type", "")).strip().lower() == "application/ld+json"]
    for script in scripts[:_JSON_LD_MAX_BLOCKS]:
        raw = str((script.string if script.string is not None else script.get_text()) or "").strip()
        if len(raw.encode("utf-8")) > _JSON_LD_MAX_BLOCK_BYTES:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            continue
        for name in _schema_types(parsed):
            cleaned = _clean(name, MAX_NAME)
            if cleaned:
                out[cleaned] = None
    return out


def _signals(html: str) -> tuple[GapSignals, Counter[str], Counter[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    schema_types = _json_ld_types(soup)

    title: str | None = None
    for tag in soup.find_all("title"):
        if tag.find_parent("svg") is None:  # <svg><title> is an accessible name
            title = _clean(tag.get_text(" ")) or None
            break

    canonicals: dict[str, None] = {}
    for tag in soup.find_all("link"):
        rel: Any = tag.get("rel") or []
        rel_list = rel.split() if isinstance(rel, str) else list(rel)
        href = _clean(tag.get("href"))
        if href and any(str(r).lower() == "canonical" for r in rel_list):
            canonicals[href] = None

    robots: dict[str, None] = {}
    for tag in soup.find_all("meta"):
        if str(tag.get("name") or "").strip().lower() != "robots":
            continue
        for directive in str(tag.get("content") or "").lower().split(","):
            d = _clean(directive, MAX_NAME)
            if d:
                robots[d] = None

    for tag in soup(list(_NON_VISIBLE_TAGS)):
        tag.decompose()

    h1: dict[str, None] = {}
    for tag in soup.find_all("h1"):
        text = _clean(tag.get_text(" ", strip=True))
        if text:
            h1[text] = None

    hrefs: Counter[str] = Counter()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if href and not href.lower().startswith(_NON_LINK_HREF):
            hrefs[href] += 1

    words: Counter[str] = Counter(_WORD.findall(soup.get_text(" ", strip=True).lower()))
    sig = GapSignals(
        words=sum(words.values()),
        links=sum(hrefs.values()),
        schema_types=list(schema_types)[:MAX_ITEMS],
        h1=list(h1)[:MAX_ITEMS],
        title=title,
        canonical=next(iter(canonicals), None),
        canonical_all=list(canonicals)[:MAX_CANONICALS],
        meta_robots=", ".join(list(robots)[:MAX_ITEMS]) or None,
    )
    return sig, words, hrefs


def _only_after(raw: Counter[str], rendered: Counter[str]) -> tuple[int, float]:
    total = sum(rendered.values())
    added = sum((rendered - raw).values())
    return added, (round(added / total, 3) if total else 0.0)


def js_gap(raw_html: str | None, rendered_html: str | None, *, raw_truncated: bool = False) -> JsGap | None:
    """
    Compare the HTML as served with the DOM after JavaScript ran.

    ``None`` when either version is missing (no render happened) or when either is
    larger than ``MAX_HTML_CHARS``. An empty string is a real, empty document.
    """
    if raw_html is None or rendered_html is None:
        return None
    if len(raw_html) > MAX_HTML_CHARS or len(rendered_html) > MAX_HTML_CHARS:
        return None
    raw, raw_words, raw_links = _signals(raw_html)
    rend, rend_words, rend_links = _signals(rendered_html)
    w_add, w_share = _only_after(raw_words, rend_words)
    l_add, l_share = _only_after(raw_links, rend_links)
    diff = GapDiff(
        words_only_rendered=w_add,
        words_only_rendered_share=w_share,
        links_only_rendered=l_add,
        links_only_rendered_share=l_share,
        schema_types_only_rendered=[n for n in rend.schema_types if n not in raw.schema_types],
        h1_only_rendered=[t for t in rend.h1 if t not in raw.h1],
        title_changed=raw.title != rend.title,
        canonical_changed=set(raw.canonical_all) != set(rend.canonical_all),
        meta_robots_changed=set((raw.meta_robots or "").split(", ")) != set((rend.meta_robots or "").split(", ")),
    )
    gap = JsGap(raw=raw, rendered=rend, diff=diff, raw_truncated=bool(raw_truncated))
    gap.reasons = js_gap_reasons(gap)
    gap.significant = bool(gap.reasons)
    return gap


def _quoted(value: object) -> str:
    if isinstance(value, list):
        value = " | ".join(str(i) for i in value)
    text = str(value or "")
    if not text:
        return "none"
    if len(text) > REASON_TEXT:
        text = text[:REASON_TEXT] + "..."
    return f"'{text}'"


def js_gap_reasons(gap: JsGap | None) -> list[str]:
    """Plain-language reasons a gap matters; empty when none do. Fixed wording (EN)."""
    if gap is None:
        return []
    raw, rend, d = gap.raw, gap.rendered, gap.diff
    reasons: list[str] = []
    if d.words_only_rendered_share >= JS_GAP_WORDS_SHARE and d.words_only_rendered >= JS_GAP_WORDS_MIN:
        reasons.append(
            f"{round(d.words_only_rendered_share * 100)}% of visible words appear only after "
            f"JavaScript runs ({raw.words} raw, {rend.words} rendered)"
        )
    if d.links_only_rendered_share >= JS_GAP_LINKS_SHARE and d.links_only_rendered >= JS_GAP_LINKS_MIN:
        reasons.append(
            f"{d.links_only_rendered} of {rend.links} links appear only after JavaScript runs "
            f"({raw.links} in the raw HTML)"
        )
    if d.schema_types_only_rendered:
        reasons.append(
            "structured data appears only after JavaScript: "
            + ", ".join(_quoted(n) for n in d.schema_types_only_rendered[:REASON_ITEMS])
        )
    if d.h1_only_rendered:
        reasons.append("the H1 changes after JavaScript" if raw.h1 else "the H1 appears only after JavaScript")
    for changed, label, a, b in (
        (d.title_changed, "title", raw.title, rend.title),
        (d.canonical_changed, "canonical URL", raw.canonical_all, rend.canonical_all),
        (d.meta_robots_changed, "meta robots value", raw.meta_robots, rend.meta_robots),
    ):
        if changed:
            reasons.append(
                f"the {label} is not the same before and after JavaScript (raw: {_quoted(a)}, rendered: {_quoted(b)})"
            )
    return reasons


def _gap_dict(raw: str, rendered: str) -> dict[str, Any] | None:
    """``js_gap`` as a JSON dict with a ``gap`` flag (= ``significant``)."""
    g = js_gap(raw, rendered)
    if g is None:
        return None
    d = g.to_dict()
    d["gap"] = bool(g.significant)
    return d


# --------------------------------------------------------------------------- operation


@operation(
    "render",
    output=RenderReport,
    requires=Requirements(network=True, browser=True, extras=["browser"]),
    timeout_s=60.0,
)
async def render(ctx: Context, target: str | Page, *, force: bool = False) -> Outcome[RenderReport]:
    """
    Render the page in the browser when it looks like a SPA shell (or ``force``) and compare
    the raw HTML with the rendered DOM.
    """
    page = await ctx.page(target)
    if not page.html_ok:
        return Outcome(data=RenderReport(url=page.url, is_spa=False, rendered=False))
    spa = is_spa(page.html)
    if not spa and not force:
        return Outcome(data=RenderReport(url=page.url, is_spa=spa, rendered=False))
    renderer = await ctx.browser()
    res = await renderer.render(page.final_url or page.url)
    if res.error or not res.html:
        return Outcome(
            data=RenderReport(
                url=page.url,
                is_spa=spa,
                rendered=False,
                error=(res.error or "render-failed").split(":", 1)[0],
            ),
            not_checked=[ctx.not_checked("js-dependent-content", "render-failed", url=page.url)],
        )
    gap = _gap_dict(page.html or "", res.html)
    return Outcome(
        data=RenderReport(
            url=page.url,
            is_spa=spa,
            rendered=True,
            js_gap=gap,
            reasons=list(gap.get("reasons", [])) if gap else [],
            html=res.html,
        )
    )
