from __future__ import annotations

from seoprobe.core.parse.urls import (
    in_scope,
    is_crawlable_page,
    is_low_value,
    normalize_url,
    template_key,
    url_key,
)


def test_normalize() -> None:
    assert normalize_url("HTTPS://Example.COM:443/a//b/?utm_source=x&q=1#frag") == "https://example.com/a/b/?q=1"
    assert normalize_url("/x", base="https://e.com/y/") == "https://e.com/x"
    for bad in ("javascript:void(0)", "mailto:a@b.c", "tel:1", "#top", "", "ftp://x.com/"):
        assert normalize_url(bad) is None


def test_normalize_returns_none_for_malformed_links_instead_of_raising() -> None:
    for bad in ("http://e.com:99999/", "https://e.com:abc/", "http://[::1/", "http://" + "a" * 64 + ".com/"):
        assert normalize_url(bad) is None
    assert normalize_url("http://e.com:99999/", base="https://e.com/") is None


def test_url_key_collapses_www_scheme_and_trailing_slash() -> None:
    assert url_key("http://www.e.com/a/") == url_key("https://e.com/a")


def test_scope_and_assets() -> None:
    assert in_scope("https://www.e.com/x", "e.com") and not in_scope("https://blog.e.com/x", "e.com")
    assert not is_crawlable_page("https://e.com/a.pdf") and is_crawlable_page("https://e.com/a")


def test_template_keys() -> None:
    assert template_key("https://e.com/") == "/"
    assert template_key("https://e.com/blog/my-first-post") == "/blog/*"
    assert template_key("https://e.com/about") == "/about"
    assert template_key("https://e.com/my-long-slug-page") == "/*"
    assert template_key("https://e.com/urun/123?color=red&size=m") == "/urun/*?color,size"
    assert is_low_value("https://e.com/blog/page/2") and not is_low_value("https://e.com/blog/x")


def test_cdn_cgi_paths_are_infrastructure_not_pages() -> None:
    from seoprobe.core.parse.urls import is_infrastructure_path

    assert is_infrastructure_path("https://e.com/cdn-cgi/l/email-protection")
    assert not is_crawlable_page("https://e.com/cdn-cgi/l/email-protection")
    assert not is_infrastructure_path("https://e.com/blog/cdn-cgi")
