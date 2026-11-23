"""SERP truncation, image header parsing and social card findings (offline)."""

from __future__ import annotations

import base64
import importlib
import struct
import zlib
from typing import Any

from seoprobe.core.result import Result
from seoprobe.features.content import Previews, previews
from tests import fakes
from tests.features.content.support import page

# the operation shadows the module name on the package: load the module itself
mp = importlib.import_module("seoprobe.features.content.previews")

WEBP_1X1 = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


def png(w: int, h: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    com = b"\xff\xfe" + struct.pack(">H", 8) + b"abcdef"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + com + sof + b"\xff\xd9"


def webp_vp8l(w: int, h: int) -> bytes:
    bits = (w - 1) | ((h - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 4
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8x(w: int, h: int) -> bytes:
    payload = b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_image_headers() -> None:
    assert mp.image_info(png(1200, 630)) == {"format": "png", "width": 1200, "height": 630}
    assert mp.image_info(jpeg(800, 418)) == {"format": "jpeg", "width": 800, "height": 418}
    assert mp.image_info(WEBP_1X1) == {"format": "webp", "width": 1, "height": 1}
    assert mp.image_info(webp_vp8l(1200, 630)) == {"format": "webp", "width": 1200, "height": 630}
    assert mp.image_info(webp_vp8x(1920, 1005)) == {"format": "webp", "width": 1920, "height": 1005}
    assert mp.image_info(b"GIF89a" + struct.pack("<HH", 30, 20)) == {
        "format": "gif",
        "width": 30,
        "height": 20,
    }
    assert mp.image_info(b"<html>") is None
    assert mp.image_info(b"") is None
    assert mp.image_info(b"\x89PNG\r\n\x1a\n") is None  # truncated header
    assert mp.image_info(b"\xff\xd8\xff\xe0\x00") is None  # truncated jpeg


def test_title_truncation_is_pixel_based_not_character_based() -> None:
    short = "Coffee beans | Shop"
    assert mp.truncate_px(short, 580) == (short, False)
    long_t = "Best Specialty Coffee Beans and Brewing Equipment Online Store for Home Baristas"
    shown, cut = mp.truncate_px(long_t, 580)
    assert cut and shown.endswith("…") and 45 <= len(shown) <= 62
    assert long_t.startswith(shown[:-1].rstrip())
    # 30 wide capitals are wider than 30 narrow letters
    assert mp.text_px("W" * 30) > mp.text_px("i" * 30) * 2
    assert mp.truncate_px("W" * 40, 580)[1] and not mp.truncate_px("i" * 80, 580)[1]


def test_description_truncation_and_missing() -> None:
    d = "word " * 60
    shown, cut = mp.truncate_chars(d, 160)
    assert cut and len(shown) <= 160 and shown.endswith("…")
    assert mp.truncate_chars("short", 160) == ("short", False)
    serp = mp.build_serp("https://www.x.test/a/b", "T", "")
    assert [s.device for s in serp] == ["desktop", "mobile"] and serp[0].description_missing
    serp2 = mp.build_serp("https://x.test/", "T", "x" * 140)
    assert not serp2[0].description_truncated and serp2[1].description_truncated  # 160 vs 120


def test_display_url_breadcrumb() -> None:
    assert mp.display_url("https://www.example.com/blog/my-post") == "example.com › blog › my-post"
    assert mp.display_url("https://example.com/") == "example.com"
    assert mp.display_url("https://example.com/a/b/c/d").endswith("› …")


def _page(og_image: str | None, twitter: bool = False) -> str:
    og = f"<meta property='og:image' content='{og_image}'>" if og_image else ""
    tw = "<meta name='twitter:card' content='summary_large_image'>" if twitter else ""
    return (
        f"<html><head><title>Hello</title><meta property='og:title' content='Hello'>{og}{tw}</head><body></body></html>"
    )


URL = "https://x.test/page"


async def _call(html: str, files: dict[str, tuple[int, dict[str, str], bytes]]) -> tuple[Result[Previews], list[str]]:
    async with fakes.session(files) as s:
        res = await previews(page(URL, html), session=s)
        return res, s.http.requested  # type: ignore[attr-defined]


async def _run(html: str, files: dict[str, tuple[int, dict[str, str], bytes]]) -> tuple[Any, set[str]]:
    res, _requested = await _call(html, files)
    assert res.ok
    return res, {f.id for f in res.findings}


async def test_good_card_has_no_image_findings() -> None:
    files = {"https://x.test/og.png": (200, {"content-type": "image/png"}, png(1200, 630))}
    res, ids = await _run(_page("https://x.test/og.png", twitter=True), files)
    assert ids == set()
    img = res.data.social.image  # type: ignore[union-attr]
    assert img.reachable and (img.width, img.height, img.ratio) == (1200, 630, 1.9)
    assert res.data.serp  # type: ignore[union-attr]


async def test_small_wrong_ratio_and_missing_twitter() -> None:
    files = {"https://x.test/og.jpg": (200, {"content-type": "image/jpeg"}, jpeg(600, 600))}
    _res, ids = await _run(_page("https://x.test/og.jpg"), files)
    assert ids == {"og-image-too-small", "og-image-wrong-ratio", "twitter-card-missing"}


async def test_unreachable_and_not_an_image() -> None:
    _r, ids = await _run(_page("https://x.test/gone.png", True), {})
    assert ids == {"og-image-unreachable"}
    html_files = {"https://x.test/fake.png": (200, {"content-type": "image/png"}, b"<html>")}
    res, ids2 = await _run(_page("https://x.test/fake.png", True), html_files)
    assert ids2 == {"og-image-unreachable"} and res.data.social.image.error == "not-an-image"  # type: ignore[union-attr]


async def test_relative_url_is_flagged_and_still_inspected() -> None:
    files = {"https://x.test/og.png": (200, {"content-type": "image/png"}, png(1200, 630))}
    res, ids = await _run(_page("/og.png", True), files)
    assert ids == {"og-image-relative-url"}
    assert res.data.social.image.reachable  # type: ignore[union-attr]


async def test_no_og_image_adds_no_image_findings() -> None:
    res, requested = await _call(_page(None, True), {})
    assert not res.findings and requested == []  # existing rules cover the missing tag
    assert "og:image" in res.data.social.missing_og  # type: ignore[union-attr]


async def test_image_fetch_is_at_most_one_request_and_size_flagged() -> None:
    big = png(1200, 630) + b"\x00" * 3_000_000
    files = {"https://x.test/og.png": (200, {"content-type": "image/png"}, big)}
    res, requested = await _call(_page("https://x.test/og.png", True), files)
    assert requested == ["https://x.test/og.png"]
    img = res.data.social.image  # type: ignore[union-attr]
    assert img.too_heavy and img.bytes_is_lower_bound


def test_rules_registered_with_both_languages() -> None:
    from seoprobe.core.rules import catalog

    for rid in mp.RULE_IDS:
        d = catalog.get(rid)
        assert d.category.value == "onpage"
        for key in (
            d.message_key,
            d.recommendation_key,
            d.failure_check_key,
            d.leading_indicator_key,
        ):
            assert catalog.message(key, "en") != key and catalog.message(key, "tr") != key
