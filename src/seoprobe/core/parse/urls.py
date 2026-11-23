"""URL normalisation, scope and template keys."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|msclkid$|mc_|_ga$|ref$)", re.I)
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "data:", "#")
_LOW_VALUE_SEGMENTS = {
    "page",
    "tag",
    "tags",
    "category",
    "categories",
    "search",
    "feed",
    "wp-json",
    "author",
    "cart",
    "login",
    "wp-admin",
}
_ASSET_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|pdf|zip|gz|mp4|mp3|woff2?|ttf|xml|json|txt)$", re.I)


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Canonical form used for de-duplication; None when the URL is not crawlable."""
    url = (url or "").strip()
    if not url or url.lower().startswith(_SKIP_SCHEMES):
        return None
    try:  # page-derived: a bad port, IPv6 bracket or IDNA label must not escape as an exception
        if base:
            url = urljoin(base, url)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        host = p.hostname.encode("idna").decode("ascii").lower()
        port = p.port
    except (ValueError, UnicodeError):
        return None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    return urlunparse((p.scheme, netloc, path, "", query, ""))


def url_key(url: str) -> str:
    """Scheme/www-insensitive key: two spellings of the same page collide."""
    n = normalize_url(url) or url
    p = urlparse(n)
    host = p.hostname or ""
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def in_scope(url: str, entry_host: str) -> bool:
    """Same host, or its www/apex counterpart."""
    h = (urlparse(url).hostname or "").lower()
    e = entry_host.lower()

    def strip(x: str) -> str:
        return x[4:] if x.startswith("www.") else x

    return strip(h) == strip(e)


def is_infrastructure_path(url: str) -> bool:
    """
    CDN/proxy plumbing that is not a page: Cloudflare's /cdn-cgi/ (its email-protection link
    404s when fetched directly because JavaScript rewrites it in the browser).
    """
    return urlparse(url).path.startswith("/cdn-cgi/")


def is_crawlable_page(url: str) -> bool:
    return not _ASSET_EXT.search(urlparse(url).path) and not is_infrastructure_path(url)


def _slug_like(seg: str) -> bool:
    return seg.count("-") >= 2 or any(ch.isdigit() for ch in seg)


def template_key(url: str) -> str:
    """Group pages by URL shape: ``/blog/my-post`` -> ``/blog/*``."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        key = "/"
    elif len(segs) == 1:
        key = "/*" if _slug_like(segs[0]) else f"/{segs[0]}"
    else:
        key = f"/{segs[0]}/*" if not _slug_like(segs[0]) else "/*/*"
    params = sorted({k for k, _ in parse_qsl(p.query)})
    return key + (f"?{','.join(params)}" if params else "")


def is_low_value(url: str) -> bool:
    p = urlparse(url)
    if any(s.lower() in _LOW_VALUE_SEGMENTS for s in p.path.split("/") if s):
        return True
    return any(k.lower() in ("page", "paged", "s", "q", "search", "sort", "filter") for k, _ in parse_qsl(p.query))


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of a URL."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
