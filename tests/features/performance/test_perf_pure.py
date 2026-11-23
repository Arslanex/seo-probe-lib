"""Pure helpers of lab.py: domains, formats, image rules, resource summary."""

from __future__ import annotations

import pytest

from seoprobe.features.performance.lab import (
    KIB,
    image_category,
    image_issues,
    map_resource_type,
    registrable_domain,
    served_format,
    summarize_resources,
    transfer_bytes,
)

WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8 "
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 4


@pytest.mark.parametrize(
    "host,expected",
    [
        ("example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("a.b.c.example.org", "example.org"),
        ("shop.example.co.uk", "example.co.uk"),
        ("www.firma.com.tr", "firma.com.tr"),
        ("app.localhost", "app.localhost"),
        ("cdn.localhost", "cdn.localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("", ""),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_served_format_trusts_the_bytes_over_the_content_type() -> None:
    assert served_format(WEBP, "image/jpeg") == "webp"  # a .jpg URL serving WebP
    assert served_format(PNG, None) == "png"
    assert served_format(JPEG, "image/png") == "jpeg"
    assert served_format(AVIF, None) == "avif"
    assert served_format(b"", "image/svg+xml; charset=utf-8") == "svg"
    assert served_format(b"<svg xmlns=...", None) == "svg"
    assert served_format(b"\x00\x01", "text/html") == "unknown"


@pytest.mark.parametrize(
    "w,above,expected",
    [
        (100, False, "thumbnail"),
        (150, True, "thumbnail"),
        (800, True, "hero"),
        (300, True, "content"),
        (900, False, "content"),
    ],
)
def test_image_category(w: float, above: bool, expected: str) -> None:
    assert image_category(w, above, 1000) == expected


def test_content_image_size_boundaries() -> None:
    assert image_issues("webp", 200 * KIB, "content", None, None, 1) == []
    assert image_issues("webp", 200 * KIB + 1, "content", None, None, 1) == ["image-too-large"]
    assert image_issues("webp", 500 * KIB, "content", None, None, 1) == ["image-too-large"]
    assert image_issues("webp", 500 * KIB + 1, "content", None, None, 1) == ["image-critically-large"]


def test_thumbnail_and_hero_thresholds() -> None:
    assert image_issues("avif", 100 * KIB, "thumbnail", None, None, 1) == []
    assert image_issues("avif", 150 * KIB, "thumbnail", None, None, 1) == ["image-too-large"]
    assert image_issues("avif", 201 * KIB, "thumbnail", None, None, 1) == ["image-critically-large"]
    assert image_issues("avif", 300 * KIB, "hero", None, None, 1) == []
    assert image_issues("avif", 400 * KIB, "hero", None, None, 1) == ["image-too-large"]
    assert image_issues("avif", 701 * KIB, "hero", None, None, 1) == ["image-critically-large"]


def test_modern_formats_are_never_flagged_as_legacy() -> None:
    assert "image-legacy-format" not in image_issues("webp", 900 * KIB, "hero", None, None, 1)
    assert "image-legacy-format" not in image_issues("avif", 900 * KIB, "hero", None, None, 1)
    assert "image-legacy-format" in image_issues("jpeg", 101 * KIB, "content", None, None, 1)
    assert "image-legacy-format" in image_issues("png", 101 * KIB, "content", None, None, 1)
    assert "image-legacy-format" not in image_issues("png", 100 * KIB, "content", None, None, 1)
    assert "image-legacy-format" not in image_issues("gif", 500 * KIB, "content", None, None, 1)


def test_svg_and_ico_are_exempt_from_every_size_rule() -> None:
    assert image_issues("svg", 5_000_000, "hero", 4000, 100, 1) == []
    assert image_issues("ico", 5_000_000, "thumbnail", 4000, 16, 1) == []


def test_unknown_bytes_trigger_nothing() -> None:
    assert image_issues(None, None, "content", 4000, 100, 1) == []


def test_oversized_for_display_respects_device_pixel_ratio_and_a_byte_floor() -> None:
    assert "image-oversized-for-display" in image_issues("webp", 60 * KIB, "content", 1000, 400, 1)
    assert "image-oversized-for-display" not in image_issues("webp", 60 * KIB, "content", 800, 400, 1)  # exactly 2x
    assert "image-oversized-for-display" not in image_issues("webp", 60 * KIB, "content", 1000, 400, 3)  # retina
    assert "image-oversized-for-display" in image_issues("webp", 60 * KIB, "content", 2500, 400, 3)
    assert "image-oversized-for-display" not in image_issues("webp", 10 * KIB, "content", 1000, 100, 1)  # tiny file


def test_map_resource_type() -> None:
    assert [map_resource_type(x) for x in ("script", "fetch", "xhr", "texttrack", "image")] == [
        "script",
        "xhr",
        "xhr",
        "other",
        "image",
    ]


def test_transfer_bytes_prefers_content_length() -> None:
    assert transfer_bytes({"headers": {"content-length": "1234"}, "body_len": 9999}) == 1234
    assert transfer_bytes({"headers": {}, "body_len": 77}) == 77
    assert transfer_bytes({"headers": {"content-length": "abc"}, "body_len": 5}) == 5


def _e(
    url: str,
    typ: str,
    body: int,
    *,
    hdr: dict[str, str] | None = None,
    main: bool = False,
    status: int = 200,
    fetch_ms: int = 0,
) -> dict:
    return {
        "url": url,
        "resolved_url": url,
        "resource_type": typ,
        "status": status,
        "headers": hdr or {},
        "body_len": body,
        "head": b"",
        "is_main": main,
        "fetch_ms": fetch_ms,
    }


def test_summarize_resources_totals_third_party_compression_and_cache() -> None:
    entries = [
        _e(
            "https://www.site.com/",
            "document",
            5000,
            main=True,
            fetch_ms=123,
            hdr={"content-encoding": "gzip", "content-length": "1500"},
        ),
        _e("https://www.site.com/a.js", "script", 50_000),  # uncompressed, no cache
        _e("https://www.site.com/b.css", "stylesheet", 900),  # small: not listed
        _e(
            "https://www.site.com/c.js",
            "script",
            40_000,
            hdr={"content-encoding": "br", "cache-control": "public, max-age=31536000"},
        ),
        _e("https://www.site.com/d.png", "image", 3000, hdr={"cache-control": "max-age=3600"}),
        _e("https://cdn.tracker.net/t.js", "script", 70_000),  # third party: never listed
        _e("https://cdn.tracker.net/p.gif", "image", 100),
        _e(
            "https://static.site.com/f.woff2",
            "font",
            20_000,
            hdr={"expires": "Wed, 01 Jan 2031 00:00:00 GMT"},
        ),
    ]
    r = summarize_resources(entries, "www.site.com")
    assert r["total_requests"] == 8
    assert r["total_bytes"] == 1500 + 50_000 + 900 + 40_000 + 3000 + 70_000 + 100 + 20_000
    assert r["html_bytes"] == 1500 and r["ttfb_ms"] == 123
    assert r["by_type"]["script"] == {"count": 3, "bytes": 160_000}
    assert r["third_party"] == [{"host": "cdn.tracker.net", "requests": 2, "bytes": 70_100}]
    assert r["uncompressed_text"] == ["https://www.site.com/a.js"]
    assert "https://www.site.com/a.js" in r["no_cache_static"] and "https://www.site.com/d.png" in r["no_cache_static"]
    assert "https://www.site.com/c.js" not in r["no_cache_static"]  # long max-age
    assert "https://static.site.com/f.woff2" not in r["no_cache_static"]  # Expires present
    assert "https://cdn.tracker.net/t.js" not in r["no_cache_static"]


def test_summarize_resources_error_status_and_immutable() -> None:
    entries = [
        _e("https://s.com/x.js", "script", 5000, status=404),
        _e(
            "https://s.com/y.js",
            "script",
            5000,
            hdr={"cache-control": "public, max-age=60, immutable"},
        ),
        _e("https://s.com/z.js", "script", 5000, hdr={"cache-control": "no-store"}),
    ]
    r = summarize_resources(entries, "s.com")
    assert r["no_cache_static"] == ["https://s.com/z.js"]
