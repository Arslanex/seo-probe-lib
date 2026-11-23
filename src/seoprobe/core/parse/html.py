"""
HTML -> ``ParsedPage``: every fact the page checks (and downstream consumers) need.

Ported from the upstream project ``scripts/page_checks.py`` and ``scripts/parse_html.py`` (MIT). The extraction logic is a faithful port; the structure is not: checks run on the
serialisable ``ParsedPage`` fields and never touch BeautifulSoup. Parsing never raises: a
document that cannot be parsed yields a ``ParsedPage`` with ``parse_error`` set.

Hostile input limits (documented deviations from the source, which has none here):
links 20 000, images 5 000, headings 2 000, JSON-LD blocks 50 and 256 KiB per block, main
text 100 000 characters. ``*_truncated`` flags say when a limit was hit.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from functools import cache
from typing import Any
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

_HTML_PARSER = "lxml"
# trafilatura logs an ERROR for every empty page. Quiet it only when the host application has not
# configured that logger itself (its own level, set before or after this import, wins).
if logging.getLogger("trafilatura").level == logging.NOTSET:
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

EVIDENCE_MAX_CHARS = 200
MAX_LINKS = 20_000
MAX_IMAGES = 5_000
MAX_HEADINGS = 2_000
JSON_LD_MAX_BLOCKS = 50
JSON_LD_MAX_BLOCK_BYTES = 256 * 1024
MAIN_TEXT_MAX_CHARS = 100_000
GENERIC_ANCHOR_MAX_CHARS = 200
HOST_MAX_CHARS = 253
UNSPACED_SCRIPT_RATIO = 0.3

GENERIC_ANCHORS = frozenset(
    {
        "click here",
        "read more",
        "learn more",
        "here",
        "more",
        "link",
        "this page",
        "details",
        "view",
        "website",
        "continue",
        "go",
    }
)
DEPRECATED_TAGS = (
    "acronym",
    "applet",
    "basefont",
    "big",
    "blink",
    "center",
    "dir",
    "font",
    "frame",
    "frameset",
    "isindex",
    "marquee",
    "nobr",
    "noframes",
    "spacer",
    "strike",
    "tt",
)
_CHROME_TAGS = ("nav", "header", "footer", "aside")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "complementary", "menubar")
_CHROME_MARKERS = (
    "navbar",
    "topbar",
    "top-bar",
    "site-header",
    "site-footer",
    "main-nav",
    "footer-nav",
    "mega-menu",
    "breadcrumb",
    "cookie-banner",
    "sidebar-nav",
)
_LAZY_SRC_ATTRS = (
    "data-perfmatters-src",
    "data-ewww-src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-full-url",
)
_MIXED_CONTENT_TAGS = ("img", "script", "iframe", "source", "video", "audio", "embed")
_SKIPPED_LINK_SCHEMES = ("#", "javascript:", "data:", "mailto:", "tel:", "sms:")
_TRACKING_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "ref"})

_PLACEHOLDER_RE = re.compile(r"\blorem ipsum\b|\bdolor sit amet\b|\bconsectetur adipiscing\b", re.I)
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_NON_ASCII_BYTES_RE = re.compile(rb"[\x80-\xff]+")
_URL_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_UTF8_ALIASES = {"utf8": "utf-8", "utf-8": "utf-8", "unicode-1-1-utf-8": "utf-8"}
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
# Han, Hiragana, Katakana, Thai, Lao, Myanmar, Khmer: no spaces between words.
_UNSPACED_SCRIPT_RE = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿฀-໿က-႟ក-៿]")
_UNSPACED_LANGS = ("zh", "ja", "th", "lo", "km", "my")


# --------------------------------------------------------------------------- models


@dataclass
class LinkInfo:
    href: str
    resolved: str  # absolute http(s) URL, "" when the link is not a countable web link
    text: str
    rel: list[str]
    zone: str  # "chrome" | "main"
    named: bool  # has an accessible name
    generic: bool  # generic anchor text outside site chrome (only for countable links)


@dataclass
class ImageInfo:
    src: str
    has_alt_attr: bool
    alt: str
    decorative: bool
    has_width: bool
    has_height: bool
    lazy: bool


@dataclass
class JsonLdBlock:
    index: int
    data: Any = None
    error: str | None = None
    size: int = 0


@dataclass
class ParsedPage:
    url: str
    base_url: str = ""
    parse_error: str | None = None
    lang: str = ""
    has_doctype: bool = False
    doctype_head: str = ""
    title: str = ""
    title_tag_count: int = 0
    meta_description: str = ""
    meta_tags: list[tuple[str, str, str]] = field(default_factory=list)  # (name, property, content)
    meta_refresh: str = ""
    meta_charset: str = ""
    has_viewport: bool = False
    canonical: str = ""
    canonical_link_count: int = 0
    og: dict[str, str] = field(default_factory=dict)
    og_url: str = ""
    hreflang: list[tuple[str, str]] = field(default_factory=list)
    h1: list[str] = field(default_factory=list)
    outline: list[tuple[int, str]] = field(default_factory=list)
    heading_level_skips: int = 0
    template_heading_level_skips: int = 0
    links: list[LinkInfo] = field(default_factory=list)
    links_truncated: bool = False
    images: list[ImageInfo] = field(default_factory=list)
    images_truncated: bool = False
    first_image_lazy: bool = False
    first_image_src: str = ""
    mixed_content: list[str] = field(default_factory=list)
    frames_count: int = 0
    deprecated_tags: list[str] = field(default_factory=list)
    placeholder_snippet: str = ""
    jsonld: list[JsonLdBlock] = field(default_factory=list)
    jsonld_total: int = 0
    schema_types: list[str] = field(default_factory=list)
    visible_text: str = ""
    word_count: int = 0
    main_text: str = ""
    dom: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers


def trim(value: object, limit: int = EVIDENCE_MAX_CHARS) -> str:
    """One line of untrusted text: control characters removed, cut to ``limit``."""
    text = re.sub(r"\s+", " ", _CONTROL_RE.sub(" ", str(value))).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def attr(el: Any, name: str) -> str:
    """Attribute as a stripped string, whether bs4 holds it as a string or a list."""
    value = el.get(name)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return str(value).strip()


def _rels(el: Any) -> list[str]:
    return attr(el, "rel").lower().split()


def _text(el: Any) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def join_url(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except ValueError:
        return ""


def is_generic_anchor(anchor: str) -> bool:
    """Generic link text once surrounding punctuation is dropped (linear scan, no regex)."""
    if len(anchor) > GENERIC_ANCHOR_MAX_CHARS:
        return False
    start, end = 0, len(anchor)
    while start < end and not anchor[start].isalnum():
        start += 1
    while end > start and not anchor[end - 1].isalnum():
        end -= 1
    return anchor[start:end].lower() in GENERIC_ANCHORS


def words_measurable(text: str, lang: str = "") -> bool:
    """Whether a whitespace word count means anything (no unspaced scripts: zh/ja/th/lo/km/my)."""
    if (lang or "").lower().split("-")[0] in _UNSPACED_LANGS:
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    return not letters or len(_UNSPACED_SCRIPT_RE.findall(text)) / letters < UNSPACED_SCRIPT_RATIO


def _one_escape_spelling(part: str) -> str:
    """One spelling for percent-encoding (RFC 3986 s.6.2.2): %c3%a9 == %C3%A9 == literal e-acute."""

    def spell(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _URL_UNRESERVED else match.group(0).upper()

    part = _PERCENT_ESCAPE_RE.sub(spell, part)
    if part.isascii():
        return part
    raw = part.encode("utf-8", "surrogatepass")
    return _NON_ASCII_BYTES_RE.sub(lambda m: b"%" + m.group(0).hex("%").upper().encode("ascii"), raw).decode("ascii")


def _idna_host(host: str) -> str:
    """Host in the ASCII form browsers send (UTS 46 style: NFKC, lower case, Punycode)."""
    host = host.lower()
    if host.isascii() or len(host) > HOST_MAX_CHARS:
        return host
    try:
        mapped = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", host).lower())
        labels = [
            label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
            for label in mapped.replace("。", ".").split(".")
        ]
    except (UnicodeError, ValueError):
        return host
    return ".".join(labels)


def canonicalize_url(url: str) -> str | None:
    """
    Comparison form of a URL: IDNA lower-case host, no fragment, no tracking parameters,
    one spelling for percent-encoding. None for anything that is not an http(s) URL.
    """
    if not url or any(c.isspace() for c in url):
        return None
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = _idna_host(parsed.hostname).rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    netloc = f"{host}:{port}" if port and port != default_port else host
    path = _one_escape_spelling(re.sub(r"/{2,}", "/", parsed.path or "/"))
    kept = []
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key = unquote(pair.split("=", 1)[0]).lower()
        if key.startswith("utm_") or key in _TRACKING_KEYS:
            continue
        kept.append(_one_escape_spelling(pair))
    return urlunparse((scheme, netloc, path, _one_escape_spelling(parsed.params), "&".join(kept), ""))


def url_match_key(url: str) -> str:
    """Key for deciding that two URL forms name the same page: trailing slash ignored."""
    canonical = canonicalize_url(url)
    if canonical is None:
        return url
    parsed = urlparse(canonical)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))


def norm_charset(value: str) -> str:
    v = (value or "").strip().lower()
    return _UTF8_ALIASES.get(v, v)


def charset_of(content_type: str) -> str:
    m = re.search(r"charset\s*=\s*['\"]?([^;,'\"\s]+)", content_type or "", re.I)
    return norm_charset(m.group(1)) if m else ""


# --------------------------------------------------------------------------- extraction


def _document_base(soup: Any, page_url: str) -> str:
    """The first <base href> when it resolves to an http(s) URL, else the page URL."""
    for base in soup.find_all("base"):
        if base.get("href") is None:
            continue
        resolved = join_url(page_url, attr(base, "href"))
        try:
            parsed = urlparse(resolved)
            usable = bool(attr(base, "href")) and parsed.scheme in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            usable = False
        return resolved if usable else page_url
    return page_url


def _is_decorative(img: Any) -> bool:
    node = img
    while node is not None and getattr(node, "attrs", None) is not None:
        if attr(node, "aria-hidden").lower() == "true":
            return True
        if attr(node, "role").lower() in ("presentation", "none"):
            return True
        node = node.parent
    return False


def _in_chrome_zone(tag: Any) -> bool:
    curr = tag.parent
    while isinstance(curr, Tag):
        if (curr.name or "").lower() in _CHROME_TAGS or attr(curr, "role").lower() in _CHROME_ROLES:
            return True
        marker = f"{attr(curr, 'class')} {attr(curr, 'id')}".lower()
        if any(tok in marker for tok in _CHROME_MARKERS):
            return True
        curr = curr.parent
    return False


def _image_src(img: Any) -> str:
    """The real source of an image, looking past lazy-loading placeholders."""
    for name in (*_LAZY_SRC_ATTRS, "src"):
        value = attr(img, name)
        if value and not value.startswith("data:"):
            return value
    for name in ("data-srcset", "srcset"):
        for candidate in attr(img, name).split(","):
            url = candidate.strip().split(" ")[0]
            if url and not url.startswith(("data:", "blob:")):
                return url
    return ""


def _visible_text(html: str) -> str:
    """Body text without scripts, styles and site chrome (nav, footer, site header)."""
    soup = BeautifulSoup(html, _HTML_PARSER)
    for tag in soup(["head", "script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    for tag in soup.find_all("header"):
        if not tag.decomposed and tag.find_parent(["article", "main"]) is None:
            tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _dom_stats(soup: Any) -> dict[str, Any]:
    root = soup.find("html") or soup
    nodes = max_depth = max_children = 0
    widest = ""
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        el, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        children = [c for c in el.children if isinstance(c, Tag)]
        if len(children) > max_children:
            max_children = len(children)
            widest = el.name or ""
        stack.extend((c, depth + 1) for c in children)
    return {
        "nodes": nodes,
        "max_depth": max_depth,
        "max_children": max_children,
        "widest_element": widest,
    }


def _headings(soup: Any) -> tuple[list[str], int, int, list[tuple[int, str]]]:
    """H1 texts, level skips in the main content, skips in template regions, outline."""
    h1: list[str] = []
    outline: list[tuple[int, str]] = []
    content_skips = template_skips = 0
    main_root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
    prev_level: int | None = None
    prev_template_level: int | None = None
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if attr(h, "aria-hidden").lower() == "true":
            continue
        text = _text(h)
        if not text:
            continue
        level = int(h.name[1])
        if len(outline) < MAX_HEADINGS:
            outline.append((level, trim(text, 160)))
        in_template = h.find_parent(list(_CHROME_TAGS)) is not None
        in_main = main_root is not None and any(p is main_root for p in h.parents)
        if main_root is not None and not in_main:
            if prev_template_level is not None and level > prev_template_level + 1:
                template_skips += 1
            prev_template_level = level
        else:
            if prev_level is not None and level > prev_level + 1:
                if in_template:
                    template_skips += 1
                else:
                    content_skips += 1
            prev_level = level
        if level == 1:
            h1.append(text)
    return h1, content_skips, template_skips, outline


def _schema_types(data: Any, out: list[str], depth: int = 0) -> None:
    if depth > 12 or len(out) > 200:
        return
    if isinstance(data, list):
        for item in data[:200]:
            _schema_types(item, out, depth + 1)
    elif isinstance(data, dict):
        t = data.get("@type")
        for v in t if isinstance(t, list) else [t]:
            if isinstance(v, str) and v not in out:
                out.append(v)
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            if key in data:
                _schema_types(data[key], out, depth + 1)


@cache
def _trafilatura() -> Any:
    """
    Imported once, on first use (it is slow to import). ``catch_warnings`` changes process-wide
    state and is not thread-safe, so it runs only for this one import, never per parse.
    """
    with warnings.catch_warnings():  # its dependencies warn about optional SOCKS support on import
        warnings.simplefilter("ignore")
        import trafilatura
    return trafilatura


def _extract_main_text(html: str, fallback: str) -> str:
    try:
        # fast=True: no fallback extractors. From trafilatura 2.4 the fallback takes the whole page
        # (a shared 2000-word menu included) when the article is short, which made every page of a
        # site look like a duplicate of the others; without it 2.3 and 2.4 agree, and it is faster.
        text = _trafilatura().extract(html, include_comments=False, include_tables=False, fast=True)
    except Exception:  # noqa: BLE001 - extraction must never break a parse
        text = None
    return (text or fallback)[:MAIN_TEXT_MAX_CHARS]


def parse_html(html: str, page_url: str, *, extract_main_text: bool = True) -> ParsedPage:
    """Parse one document. Never raises."""
    page = ParsedPage(url=page_url, base_url=page_url)
    try:
        _parse_into(page, html, page_url, extract_main_text)
    except RecursionError:
        page.parse_error = "document nested too deeply to parse"
    except Exception as exc:  # noqa: BLE001 - hostile input must not produce a traceback
        page.parse_error = f"{type(exc).__name__}: {exc}"[:200]
    return page


def _parse_into(page: ParsedPage, html: str, page_url: str, extract_main_text: bool) -> None:
    soup = BeautifulSoup(html, _HTML_PARSER)
    base_url = _document_base(soup, page_url)
    page.base_url = base_url
    is_https = page_url.lower().startswith("https://")

    html_tag = soup.find("html")
    page.lang = attr(html_tag, "lang") if isinstance(html_tag, Tag) else ""

    visible_text = _visible_text(html)
    page.visible_text = visible_text
    page.word_count = len(visible_text.split())

    # --- title / meta ----------------------------------------------------
    title_tags = [t for t in soup.find_all("title") if t.find_parent("svg") is None]
    page.title_tag_count = len(title_tags)
    page.title = _text(title_tags[0]) if title_tags else ""

    for meta in soup.find_all("meta"):
        content = attr(meta, "content")
        if content:
            page.meta_tags.append((attr(meta, "name").lower(), attr(meta, "property").lower(), content))
    page.meta_description = next((c for n, _p, c in page.meta_tags if n == "description"), "")
    page.meta_refresh = next(
        (attr(m, "content") for m in soup.find_all("meta") if attr(m, "http-equiv").lower() == "refresh"),
        "",
    )
    for m in soup.find_all("meta"):
        if attr(m, "charset"):
            page.meta_charset = norm_charset(attr(m, "charset"))
            break
        if attr(m, "http-equiv").lower() == "content-type":
            page.meta_charset = charset_of(attr(m, "content"))
            if page.meta_charset:
                break
    page.has_viewport = any(attr(m, "name").lower() == "viewport" for m in soup.find_all("meta"))
    for name, prop, content in page.meta_tags:
        key = prop or name
        if key.startswith("og:") and key not in page.og:
            page.og[key] = content
    if page.og.get("og:url"):
        page.og_url = join_url(base_url, page.og["og:url"])

    # --- canonical / hreflang -------------------------------------------
    canonical_links = [link for link in soup.find_all("link") if "canonical" in _rels(link)]
    page.canonical_link_count = len(canonical_links)
    page.canonical = next(
        (u for u in (join_url(base_url, attr(link, "href")) for link in canonical_links if attr(link, "href")) if u),
        "",
    )
    page.hreflang = [
        (attr(link, "hreflang"), join_url(base_url, attr(link, "href")))
        for link in soup.find_all("link")
        if "alternate" in _rels(link) and attr(link, "hreflang") and attr(link, "href")
    ]

    # --- headings --------------------------------------------------------
    (
        page.h1,
        page.heading_level_skips,
        page.template_heading_level_skips,
        page.outline,
    ) = _headings(soup)

    # --- document basics -------------------------------------------------
    head_text = re.sub(r"^(?:<!--.*?-->\s*)+", "", html.lstrip().lstrip("\ufeff").lstrip(), flags=re.S)
    page.has_doctype = head_text[:15].lower().startswith("<!doctype html")
    page.doctype_head = head_text[:40]
    page.frames_count = len(soup.find_all(["frameset", "frame"]))
    page.deprecated_tags = sorted({t.name for t in soup.find_all(list(DEPRECATED_TAGS))})
    if ph := _PLACEHOLDER_RE.search(visible_text):
        start = max(0, ph.start() - 40)
        page.placeholder_snippet = visible_text[start : ph.end() + 60]

    # --- images ----------------------------------------------------------
    for img in soup.find_all("img"):
        if attr(img, "aria-hidden").lower() == "true":
            continue
        src = _image_src(img)
        if not src:
            continue
        if len(page.images) >= MAX_IMAGES:
            page.images_truncated = True
            break
        page.images.append(
            ImageInfo(
                src=src,
                has_alt_attr=img.get("alt") is not None,
                alt=attr(img, "alt"),
                decorative=_is_decorative(img),
                has_width=bool(attr(img, "width")),
                has_height=bool(attr(img, "height")),
                lazy=attr(img, "loading").lower() == "lazy",
            )
        )
    scope = soup.find("main") or soup.body or soup
    first_img = scope.find("img")
    page.first_image_lazy = first_img is not None and attr(first_img, "loading").lower() == "lazy"
    if first_img is not None:
        page.first_image_src = _image_src(first_img)

    # --- links -----------------------------------------------------------
    for a in soup.find_all("a", href=True):
        href = attr(a, "href")
        anchor = _text(a)
        named = bool(
            anchor
            or attr(a, "aria-label")
            or attr(a, "aria-labelledby")
            or attr(a, "title")
            or any(attr(img, "alt") for img in a.find_all("img"))
            or any(svg.find("title") is not None for svg in a.find_all("svg"))
        )
        resolved = ""
        if href and not href.lower().startswith(_SKIPPED_LINK_SCHEMES):
            full = join_url(base_url, href)
            try:
                p = urlparse(full)
                if p.scheme.lower() in ("http", "https") and p.hostname:
                    resolved = full
            except ValueError:
                pass
        chrome = _in_chrome_zone(a)
        if len(page.links) >= MAX_LINKS:
            page.links_truncated = True
            break
        page.links.append(
            LinkInfo(
                href=trim(href, 300),
                resolved=trim(resolved, 500) if resolved else "",
                text=trim(anchor, 200),
                rel=_rels(a),
                zone="chrome" if chrome else "main",
                named=named,
                generic=bool(resolved) and is_generic_anchor(anchor) and not chrome,
            )
        )
    if is_https:
        for el in soup.find_all(list(_MIXED_CONTENT_TAGS)):
            if attr(el, "src").lower().startswith("http://"):
                page.mixed_content.append(attr(el, "src"))
        for link in soup.find_all("link", href=True):
            rels = _rels(link)
            if ("stylesheet" in rels or "preload" in rels) and attr(link, "href").lower().startswith("http://"):
                page.mixed_content.append(attr(link, "href"))

    # --- DOM / JSON-LD ----------------------------------------------------
    page.dom = _dom_stats(soup)
    types: list[str] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        page.jsonld_total += 1
        if len(page.jsonld) >= JSON_LD_MAX_BLOCKS:
            continue
        block = JsonLdBlock(index=page.jsonld_total, size=len(raw))
        if len(raw) > JSON_LD_MAX_BLOCK_BYTES:
            block.error = "block too large to parse"
        else:
            try:
                block.data = json.loads(raw)
                _schema_types(block.data, types)
            except RecursionError:
                block.error = "nested too deeply to parse"
            except ValueError as exc:
                block.error = trim(str(exc), 120)
        page.jsonld.append(block)
    page.schema_types = types[:50]

    page.main_text = _extract_main_text(html, visible_text) if extract_main_text else ""
