"""
Deterministic single-page checks (47 rules) and the ``onpage`` operation.

Port of the upstream project ``page_checks.run_checks``.

The check logic, thresholds and evidence strings follow the source (MIT; the source
was itself ported from web-obs, MIT). Differences, all deliberate:

* input is ``FetchResult`` + ``ParsedPage`` (no BeautifulSoup in this module);
* findings come from the catalog (``make_finding``), so severity/messages/effort live in one place;
* language gate: the two relevance checks use the source's English
  stopword list, so they run only for English text and are ``NotChecked('language-unsupported')``
  otherwise;
* word-based checks on unspaced scripts are ``NotChecked('words-not-measurable')``;
* a page that is ``noindex`` lowers ``missing-meta-description`` to Info.

Thresholds are display heuristics, never search engine rules.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import SEVERITY_ORDER, CheckOutput, FetchResult, Finding, NotChecked, Requirements, Severity
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage, charset_of, trim, url_match_key, words_measurable
from seoprobe.core.rules.catalog import make_finding

from .models import OnPageReport

# The 47 rule ids, in the source table order (definitions live in catalog.json).
PAGE_RULE_IDS: tuple[str, ...] = (
    "missing-title",
    "title-too-long",
    "title-too-short",
    "missing-meta-description",
    "meta-description-too-long",
    "duplicate-meta-tags",
    "missing-h1",
    "multiple-h1",
    "heading-level-skip",
    "missing-canonical",
    "canonical-points-elsewhere",
    "og-url-differs-from-canonical",
    "noindex",
    "noindex-header",
    "nofollow",
    "meta-refresh-redirect",
    "hreflang-missing-self",
    "missing-doctype",
    "missing-charset",
    "charset-mismatch",
    "missing-lang",
    "missing-viewport",
    "uses-frames",
    "deprecated-html-tags",
    "placeholder-text",
    "images-missing-alt",
    "images-missing-dimensions",
    "first-image-lazy-loaded",
    "links-without-text",
    "generic-anchor-text",
    "links-to-http",
    "mixed-content",
    "url-too-long",
    "url-has-parameters",
    "url-uppercase",
    "url-underscores",
    "url-non-ascii",
    "url-double-slash",
    "html-over-2mb",
    "deep-dom",
    "wide-dom-node",
    "missing-open-graph",
    "missing-og-image",
    "invalid-jsonld",
    "title-not-reflected-in-content",
    "description-not-reflected-in-content",
    "thin-content",
)

TITLE_MIN_CHARS = 30
TITLE_MAX_CHARS = 60
META_DESCRIPTION_MIN_CHARS = 120  # a fact only; no finding id exists for it
META_DESCRIPTION_MAX_CHARS = 160
URL_MAX_CHARS = 100
HTML_MAX_BYTES = 2_000_000
THIN_CONTENT_MIN_WORDS = 200
DOM_MAX_DEPTH = 32
DOM_MAX_CHILDREN = 60
TITLE_RELEVANCE_MIN = 0.3
DESCRIPTION_RELEVANCE_MIN = 0.2
RELEVANCE_MIN_WORDS = 50
ROBOTS_OTHER_AGENTS_MAX = 10

THRESHOLDS: dict[str, float | int] = {
    "title_min_chars": TITLE_MIN_CHARS,
    "title_max_chars": TITLE_MAX_CHARS,
    "meta_description_min_chars": META_DESCRIPTION_MIN_CHARS,
    "meta_description_max_chars": META_DESCRIPTION_MAX_CHARS,
    "url_max_chars": URL_MAX_CHARS,
    "html_max_bytes": HTML_MAX_BYTES,
    "thin_content_min_words": THIN_CONTENT_MIN_WORDS,
    "dom_max_depth": DOM_MAX_DEPTH,
    "dom_max_children": DOM_MAX_CHILDREN,
    "title_relevance_min": TITLE_RELEVANCE_MIN,
    "description_relevance_min": DESCRIPTION_RELEVANCE_MIN,
    "relevance_min_words": RELEVANCE_MIN_WORDS,
}

STOPWORDS = frozenset(
    """
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further get got had has have having he her here hers
herself him himself his how i if in into is it its itself just let me more most my myself no nor not now of off on once
only or other our ours ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up us very was we were what when where which while who whom
why will with would you your yours yourself yourselves new one two use using used like make made way may might must
need via per yet etc www http https com
""".split()
)

_ROBOTS_VALUED_DIRECTIVES = (
    "max-snippet",
    "max-image-preview",
    "max-video-preview",
    "unavailable_after",
)
_ROBOTS_GENERAL_AGENTS = ("", "googlebot")
_ROBOTS_META_NAMES = ("robots", "googlebot")
_CRAWLER_NAME_RE = re.compile(r"[a-z][a-z0-9_.-]*")
_WORD_RE = re.compile(r"[^\W\d_][\w'-]*", re.UNICODE)
_TRACKING_PARAM_RE = re.compile(r"^(utm_|fbclid$|gclid$|msclkid$|mc_)", re.I)

# English-looking text: share of tokens that are English stopwords. Used only to decide whether
# the English-only relevance checks may run on a page without a usable lang attribute.
_ENGLISH_STOPWORD_SHARE = 0.15
_ENGLISH_MIN_TOKENS = 30


# --------------------------------------------------------------------------- helpers


def tokenize(text: str) -> list[str]:
    text = (text or "").replace("’", "'").replace("‘", "'")
    return [w.lower().strip("'-") for w in _WORD_RE.findall(text) if w.strip("'-")]


def significant_terms(text: str) -> list[str]:
    return [t for t in tokenize(text) if len(t) >= 3 and t not in STOPWORDS]


def _relevance(source: str, body_terms: set[str]) -> tuple[float | None, list[str]]:
    terms = set(significant_terms(source))
    if not terms:
        return None, []
    return round(len(terms & body_terms) / len(terms), 2), sorted(terms - body_terms)


def _sample(values: list[str], each: int = 60) -> str:
    """Up to five distinct examples (the same image used twice is shown once)."""
    values = list(dict.fromkeys(values))
    shown = ", ".join(trim(v, each) for v in values[:5])
    return shown + (f" (+{len(values) - 5} more)" if len(values) > 5 else "")


def _robots_rules(value: str) -> list[tuple[str, str]]:
    """(user agent, directive) pairs of one robots meta / X-Robots-Tag value (source grammar)."""
    rules: list[tuple[str, str]] = []
    agent = ""
    for part in (value or "").lower().split(","):
        name, sep, rest = (piece.strip() for piece in part.partition(":"))
        if sep and name not in _ROBOTS_VALUED_DIRECTIVES and _CRAWLER_NAME_RE.fullmatch(name):
            agent = name
            name, sep, rest = (piece.strip() for piece in rest.partition(":"))
        if not sep:
            rules.extend((agent, word) for word in name.split())
    return rules


def _robots_directives(value: str) -> set[str]:
    return {d for agent, d in _robots_rules(value) if agent in _ROBOTS_GENERAL_AGENTS}


def is_english_text(text: str, lang: str | None) -> bool:
    """
    Whether the English-only relevance checks may run.

    A declared language decides: ``en`` / ``en-*`` is supported, anything else is not. Without a
    usable lang attribute the text itself decides, by the share of English stopwords.
    """
    primary = (lang or "").strip().lower().split("-")[0].split("_")[0]
    if primary:
        return primary == "en"
    tokens = tokenize(text)
    if len(tokens) < _ENGLISH_MIN_TOKENS:
        return False
    hits = sum(1 for t in tokens if t in STOPWORDS)
    return hits / len(tokens) >= _ENGLISH_STOPWORD_SHARE


# --------------------------------------------------------------------------- main entry


def run_page_checks(fetch: FetchResult, parsed: ParsedPage, *, lang: str | None = None) -> CheckOutput:
    """Run every page check. Never raises: an unexpected failure becomes ``rule-error``."""
    try:
        return _run(fetch, parsed, lang)
    except Exception as exc:  # noqa: BLE001 - one bad rule must not kill the audit
        return CheckOutput(
            not_checked=[
                NotChecked(
                    check_id="page-checks",
                    reason_code="rule-error",
                    url=fetch.final_url or fetch.url,
                )
            ],
            facts={"rule_error": f"{type(exc).__name__}: {exc}"[:200]},
        )


def _run(fetch: FetchResult, parsed: ParsedPage, lang_hint: str | None) -> CheckOutput:
    page_url = fetch.final_url or fetch.url
    headers_available = fetch.status is not None
    hdrs = {str(k).lower(): str(v) for k, v in fetch.headers.items()}
    is_https = page_url.lower().startswith("https://")
    html_bytes = fetch.html_bytes or (len((fetch.html or "").encode("utf-8", "replace")))

    findings: list[Finding] = []
    not_checked: list[NotChecked] = []

    def add(check_id: str, evidence: object, count: int | None = None, **kw: Any) -> None:
        findings.append(make_finding(check_id, url=page_url, evidence=trim(evidence), count=count, **kw))

    def skip(check_id: str, reason: str) -> None:
        not_checked.append(NotChecked(check_id=check_id, reason_code=reason, url=page_url))

    have_doc = parsed.parse_error is None
    page_lang = parsed.lang
    visible_text = parsed.visible_text
    word_count = parsed.word_count
    measurable = words_measurable(visible_text, page_lang)

    # --- robots first (needed for the noindex-aware severity of missing-meta-description) --------
    robots_metas = [(name, content) for name, _prop, content in parsed.meta_tags if name in _ROBOTS_META_NAMES]
    meta_directives: set[str] = set()
    for _name, content in robots_metas:
        meta_directives |= _robots_directives(content)
    meta_robots = "; ".join(f"{name}: {content}" for name, content in robots_metas)
    x_robots_values = [str(v) for k, v in fetch.headers.items() if str(k).lower() == "x-robots-tag"]
    x_robots = ", ".join(x_robots_values).strip()
    header_rules = [rule for value in x_robots_values for rule in _robots_rules(value)]
    header_directives = {d for agent, d in header_rules if agent in _ROBOTS_GENERAL_AGENTS}
    other_agent_rules = list(
        dict.fromkeys(f"{agent}: {d}" for agent, d in header_rules if agent not in _ROBOTS_GENERAL_AGENTS)
    )
    is_noindex = bool((meta_directives | header_directives) & {"noindex", "none"})

    # --- title / description ----------------------------------------------------------------------
    title = parsed.title
    description = parsed.meta_description
    if have_doc:
        if not title:
            add(
                "missing-title",
                "no <title> element" if not parsed.title_tag_count else "the <title> element is empty",
            )
        else:
            if len(title) > TITLE_MAX_CHARS:
                add("title-too-long", f"{len(title)} characters: {title}")
            if len(title) < TITLE_MIN_CHARS:
                if words_measurable(title, page_lang):
                    add("title-too-short", f"{len(title)} characters: {title}")
                else:
                    skip("title-too-short", "words-not-measurable")

        if not description:
            kw: dict[str, Any] = {}
            if is_noindex:
                kw = {
                    "severity": Severity.INFO,
                    "severity_reason": "page is noindex, so its snippet is not shown in results",
                }
            add("missing-meta-description", 'no <meta name="description"> with content', **kw)
        elif len(description) > META_DESCRIPTION_MAX_CHARS:
            add("meta-description-too-long", f"{len(description)} characters: {description}")

        duplicates: list[str] = []
        if parsed.title_tag_count > 1:
            duplicates.append(f"title x{parsed.title_tag_count}")
        for wanted, index in (
            (("description", "robots", "viewport", "title"), 0),
            (("og:title", "og:description", "og:url", "og:image"), 1),
        ):
            for key in wanted:
                n = sum(1 for tag in parsed.meta_tags if tag[index] == key)
                if n > 1:
                    duplicates.append(f"meta {key} x{n}")
        if parsed.canonical_link_count > 1:
            duplicates.append(f"canonical x{parsed.canonical_link_count}")
        if duplicates:
            add("duplicate-meta-tags", ", ".join(duplicates), len(duplicates))

        # --- headings -----------------------------------------------------------------------------
        h1 = parsed.h1
        if not h1:
            add("missing-h1", "no H1 element with text")
        elif len(h1) > 1:
            add("multiple-h1", f"{len(h1)} H1 headings: {_sample(h1)}", len(h1))
        if parsed.heading_level_skips:
            add(
                "heading-level-skip",
                f"{parsed.heading_level_skips} skipped heading level(s) in the main content",
                parsed.heading_level_skips,
            )
    else:
        h1 = []
        for cid in (
            "missing-title",
            "title-too-long",
            "title-too-short",
            "missing-meta-description",
            "meta-description-too-long",
            "duplicate-meta-tags",
            "missing-h1",
            "multiple-h1",
            "heading-level-skip",
        ):
            skip(cid, "parse-failed")

    # --- canonical, og:url ----------------------------------------------------------------------
    self_key = url_match_key(page_url)
    canonical = parsed.canonical
    canonical_note = None
    if not have_doc:
        canonical_status = "unknown"
        skip("missing-canonical", "parse-failed")
        skip("canonical-points-elsewhere", "parse-failed")
        skip("og-url-differs-from-canonical", "parse-failed")
    else:
        if not canonical:
            canonical_status = "missing"
            add("missing-canonical", 'no <link rel="canonical"> with an href')
        elif url_match_key(canonical) == self_key:
            canonical_status = "self"
            if canonical != page_url and canonical.rstrip("/") == page_url.rstrip("/"):
                canonical_note = "differs from the page URL only by a trailing slash, treated as self-referencing"
        else:
            canonical_status = "other"
            add(
                "canonical-points-elsewhere",
                f"canonical {trim(canonical, 60)} vs page {trim(page_url, 60)} "
                "(a trailing slash difference alone is tolerated)",
            )
        og_url = parsed.og_url
        if og_url and canonical and url_match_key(og_url) != url_match_key(canonical):
            add(
                "og-url-differs-from-canonical",
                f"og:url {trim(og_url, 60)} vs canonical {trim(canonical, 60)} "
                "(a trailing slash difference alone is tolerated)",
            )

    # --- robots, meta refresh, hreflang -------------------------------------------------------
    if have_doc and (meta_directives & {"noindex", "none"}):
        add("noindex", f"meta {meta_robots}")
    if header_directives & {"noindex", "none"}:
        add("noindex-header", f"X-Robots-Tag: {x_robots}")
    elif not headers_available:
        skip("noindex-header", "headers-unavailable")
    nofollow_sources = []
    if have_doc and (meta_directives & {"nofollow", "none"}):
        nofollow_sources.append(f"meta {meta_robots}")
    if header_directives & {"nofollow", "none"}:
        nofollow_sources.append(f"X-Robots-Tag: {x_robots}")
    if nofollow_sources:
        add("nofollow", "; ".join(nofollow_sources))

    if have_doc:
        if parsed.meta_refresh and "url=" in parsed.meta_refresh.lower():
            add("meta-refresh-redirect", f'content="{parsed.meta_refresh}"')
        hreflang = parsed.hreflang
        if hreflang and not any(url_match_key(url) == self_key for _lang, url in hreflang):
            add(
                "hreflang-missing-self",
                f"{len(hreflang)} hreflang link(s) ({_sample([c for c, _u in hreflang], 12)}), "
                f"none points to {trim(page_url, 80)}",
            )
    else:
        hreflang = []
        skip("meta-refresh-redirect", "parse-failed")
        skip("hreflang-missing-self", "parse-failed")

    # --- document basics ------------------------------------------------------------------------
    header_charset = charset_of(hdrs.get("content-type", ""))
    if have_doc:
        if not parsed.has_doctype:
            add(
                "missing-doctype",
                f"document starts with: {parsed.doctype_head}" if parsed.doctype_head else "empty document",
            )
        if not parsed.meta_charset and not header_charset:
            add(
                "missing-charset",
                "no meta charset and no charset in the Content-Type header"
                if headers_available
                else "no meta charset; response headers were not supplied, so the Content-Type header was not checked",
            )
        if parsed.meta_charset and header_charset and parsed.meta_charset != header_charset:
            add(
                "charset-mismatch",
                f"meta charset {parsed.meta_charset}, Content-Type header charset {header_charset}",
            )
        elif not headers_available:
            skip("charset-mismatch", "headers-unavailable")
        if not parsed.lang:
            add("missing-lang", "<html> has no lang attribute")
        if not parsed.has_viewport:
            add("missing-viewport", 'no <meta name="viewport">')
        if parsed.frames_count:
            add(
                "uses-frames",
                f"{parsed.frames_count} <frameset> or <frame> element(s)",
                parsed.frames_count,
            )
        if parsed.deprecated_tags:
            add(
                "deprecated-html-tags",
                ", ".join(f"<{n}>" for n in parsed.deprecated_tags),
                len(parsed.deprecated_tags),
            )
        if parsed.placeholder_snippet:
            add("placeholder-text", parsed.placeholder_snippet)
    else:
        for cid in (
            "missing-doctype",
            "missing-charset",
            "charset-mismatch",
            "missing-lang",
            "missing-viewport",
            "uses-frames",
            "deprecated-html-tags",
            "placeholder-text",
        ):
            skip(cid, "parse-failed")

    # --- images / links / mixed content ----------------------------------------------------------
    images_total = len(parsed.images)
    missing_alt = [i.src for i in parsed.images if not i.has_alt_attr and not i.decorative]
    images_empty_alt = sum(1 for i in parsed.images if i.has_alt_attr and not i.alt and not i.decorative)
    missing_dimensions = [i.src for i in parsed.images if not i.has_width or not i.has_height]
    links_total = sum(1 for lk in parsed.links if lk.resolved)
    links_without_text = [lk.href or "(empty href)" for lk in parsed.links if not lk.named]
    generic_anchors = [f'"{lk.text}" to {lk.resolved}' for lk in parsed.links if lk.generic]
    insecure_links = [
        lk.resolved
        for lk in parsed.links
        if is_https and lk.resolved and urlparse(lk.resolved).scheme.lower() == "http"
    ]
    mixed = parsed.mixed_content if is_https else []
    if have_doc:
        if missing_alt:
            add(
                "images-missing-alt",
                f"{len(missing_alt)} of {images_total} images: {_sample(missing_alt)}",
                len(missing_alt),
            )
        if missing_dimensions:
            add(
                "images-missing-dimensions",
                f"{len(missing_dimensions)} of {images_total} images: {_sample(missing_dimensions)}",
                len(missing_dimensions),
            )
        if parsed.first_image_lazy:
            add(
                "first-image-lazy-loaded",
                f'loading="lazy" on {parsed.first_image_src or "the first <img>"}',
            )
        if links_without_text:
            add(
                "links-without-text",
                f"{len(links_without_text)} link(s): {_sample(links_without_text)}",
                len(links_without_text),
            )
        if generic_anchors:
            add(
                "generic-anchor-text",
                f"{len(generic_anchors)} link(s): {_sample(generic_anchors)}",
                len(generic_anchors),
            )
        if insecure_links:
            add(
                "links-to-http",
                f"{len(insecure_links)} link(s): {_sample(insecure_links)}",
                len(insecure_links),
            )
        if mixed:
            add("mixed-content", f"{len(mixed)} resource(s): {_sample(mixed)}", len(mixed))
    else:
        for cid in (
            "images-missing-alt",
            "images-missing-dimensions",
            "first-image-lazy-loaded",
            "links-without-text",
            "generic-anchor-text",
            "links-to-http",
            "mixed-content",
        ):
            skip(cid, "parse-failed")

    # --- URL hygiene (needs no document) ---------------------------------------------------------
    parsed_url = urlparse(page_url)
    path = parsed_url.path or "/"
    params = [p.split("=", 1)[0] for p in parsed_url.query.split("&") if p]
    content_params = [p for p in params if not _TRACKING_PARAM_RE.match(p)]
    if len(page_url) > URL_MAX_CHARS:
        add("url-too-long", f"{len(page_url)} characters: {page_url}")
    if content_params:
        add(
            "url-has-parameters",
            f"parameters: {_sample(content_params, 30)} (tracking parameters are ignored)",
            len(content_params),
        )
    unescaped_path = re.sub(r"%[0-9A-Fa-f]{2}", "", path)
    if unescaped_path != unescaped_path.lower():
        add("url-uppercase", f"path {path}")
    if "_" in path:
        add("url-underscores", f"path {path}")
    if any(ord(ch) > 127 for ch in page_url):
        add("url-non-ascii", page_url)
    if "//" in path:
        add("url-double-slash", f"path {path}")

    # --- size / DOM -------------------------------------------------------------------------------
    if html_bytes > HTML_MAX_BYTES:
        add("html-over-2mb", f"{html_bytes} bytes of HTML")
    dom = parsed.dom or {"nodes": 0, "max_depth": 0, "max_children": 0, "widest_element": ""}
    if have_doc:
        if dom["max_depth"] > DOM_MAX_DEPTH:
            add("deep-dom", f"{dom['max_depth']} levels deep")
        if dom["max_children"] > DOM_MAX_CHILDREN:
            add(
                "wide-dom-node",
                f"<{dom['widest_element']}> has {dom['max_children']} direct children",
            )
    else:
        skip("deep-dom", "parse-failed")
        skip("wide-dom-node", "parse-failed")

    # --- social / JSON-LD ---------------------------------------------------------------------------
    og = parsed.og
    jsonld_errors = [f"block {b.index}: {b.error}" for b in parsed.jsonld if b.error]
    if have_doc:
        if not og.get("og:title") and not og.get("og:image"):
            add("missing-open-graph", "no og:title and no og:image")
        elif not og.get("og:image"):
            add("missing-og-image", "og:title is present, og:image is not")
        if jsonld_errors:
            add(
                "invalid-jsonld",
                f"{len(jsonld_errors)} of {parsed.jsonld_total} JSON-LD block(s): {'; '.join(jsonld_errors)}",
                len(jsonld_errors),
            )
    else:
        for cid in ("missing-open-graph", "missing-og-image", "invalid-jsonld"):
            skip(cid, "parse-failed")

    # --- content -------------------------------------------------------------------------------------
    body_terms = set(significant_terms(visible_text))
    title_relevance, title_missing = _relevance(title, body_terms)
    description_relevance, description_missing = _relevance(description, body_terms)
    english = is_english_text(visible_text, lang_hint or page_lang)
    if not have_doc:
        for cid in (
            "thin-content",
            "title-not-reflected-in-content",
            "description-not-reflected-in-content",
        ):
            skip(cid, "parse-failed")
    elif not measurable:
        for cid in (
            "thin-content",
            "title-not-reflected-in-content",
            "description-not-reflected-in-content",
        ):
            skip(cid, "words-not-measurable")
    else:
        if word_count < THIN_CONTENT_MIN_WORDS:
            add("thin-content", f"{word_count} words of visible body text")
        if not english:
            skip("title-not-reflected-in-content", "language-unsupported")
            skip("description-not-reflected-in-content", "language-unsupported")
        elif word_count >= RELEVANCE_MIN_WORDS:
            if title_relevance is not None and title_relevance < TITLE_RELEVANCE_MIN:
                add(
                    "title-not-reflected-in-content",
                    f"{title_relevance:.0%} of title terms found in the body; missing: {', '.join(title_missing)}",
                )
            if description_relevance is not None and description_relevance < DESCRIPTION_RELEVANCE_MIN:
                add(
                    "description-not-reflected-in-content",
                    f"{description_relevance:.0%} of description terms found in the body; "
                    f"missing: {', '.join(description_missing)}",
                )

    facts: dict[str, Any] = {
        "title": trim(title),
        "title_length": len(title),
        "title_count": parsed.title_tag_count,
        "meta_description": trim(description),
        "meta_description_length": len(description),
        "h1": [trim(t, 120) for t in h1[:10]],
        "h1_count": len(h1),
        "heading_level_skips": parsed.heading_level_skips,
        "template_heading_level_skips": parsed.template_heading_level_skips,
        "canonical": trim(canonical) or None,
        "canonical_status": canonical_status,
        "canonical_note": canonical_note,
        "meta_robots": trim(meta_robots) or None,
        "x_robots_tag": trim(x_robots) or None,
        "x_robots_other_agents": [trim(r, 100) for r in other_agent_rules[:ROBOTS_OTHER_AGENTS_MAX]],
        "hreflang_count": len(hreflang),
        "lang": trim(parsed.lang, 35) or None,
        "has_doctype": parsed.has_doctype,
        "meta_charset": trim(parsed.meta_charset, 40) or None,
        "header_charset": trim(header_charset, 40) or None,
        "has_viewport": parsed.has_viewport,
        "images": {
            "total": images_total,
            "missing_alt": len(missing_alt),
            "empty_alt": images_empty_alt,
            "missing_dimensions": len(missing_dimensions),
            "first_image_lazy": parsed.first_image_lazy,
        },
        "links": {
            "total": links_total,
            "without_text": len(links_without_text),
            "generic_anchor": len(generic_anchors),
            "http_on_https": len(insecure_links),
        },
        "mixed_content": len(mixed),
        "url": {"length": len(page_url), "query_params": [trim(p, 40) for p in params[:20]]},
        "html_bytes": html_bytes,
        "dom": dom,
        "open_graph": {trim(k, 40): trim(v) for k, v in list(og.items())[:20]},
        "jsonld_blocks": parsed.jsonld_total,
        "jsonld_invalid": len(jsonld_errors),
        "schema_types": list(parsed.schema_types),
        "word_count": word_count if measurable else None,
        "words_measured": measurable,
        "title_relevance": title_relevance if measurable and english else None,
        "description_relevance": description_relevance if measurable and english else None,
        "relevance_language_supported": english,
        "is_noindex": is_noindex,
        "headers_available": headers_available,
        "parse_error": parsed.parse_error,
        "thresholds": dict(THRESHOLDS),
    }
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f.severity))
    facts["checks_run"] = 47 - len(not_checked)
    return CheckOutput(findings=findings, not_checked=not_checked, facts=facts)


# --------------------------------------------------------------------------- operation


@operation("onpage", output=OnPageReport, requires=Requirements(network=True), timeout_s=30)
async def onpage(ctx: Context, target: str | Page, *, lang: str | None = None) -> Outcome[OnPageReport]:
    """Run the 47 deterministic single-page checks."""
    page = await ctx.page(target)
    parsed = page.parsed
    if parsed is None:
        return Outcome(data=OnPageReport(url=page.url, checked=False))
    out = run_page_checks(page, parsed, lang=lang)
    return Outcome(
        data=OnPageReport(
            url=page.url,
            checked=True,
            title=parsed.title or None,
            word_count=parsed.word_count,
            facts=out.facts,
        ),
        findings=[f.model_copy(update={"url": f.url or page.url}) for f in out.findings],
        not_checked=[n.model_copy(update={"url": n.url or page.url}) for n in out.not_checked],
    )
