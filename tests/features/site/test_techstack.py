"""Technology detection: signatures per category, versions, false positives, hostile input."""

from __future__ import annotations

import json
import re
import time
from importlib.resources import files

import pytest

from seoprobe.features.site import TechStack, techstack
from seoprobe.features.site.techstack import detect, load_technologies
from tests import fakes


def names(stack):  # type: ignore[no-untyped-def]
    return {i.name for i in stack.items}


def test_database_size_and_regexes_compile() -> None:
    techs = load_technologies()
    assert len(techs) >= 80
    assert {t.category for t in techs} >= {
        "cms", "framework", "js-library", "ui", "server", "cdn", "hosting",
        "analytics", "tag-manager", "ecommerce", "font", "security", "ads", "other",
    }  # fmt: skip
    raw = json.loads(files("seoprobe.features.site").joinpath("techstack.json").read_text("utf-8"))
    for t in raw["technologies"]:
        for p in t["patterns"]:
            re.compile(p["regex"])
            assert len(p["regex"]) < 200  # short, bounded patterns only


@pytest.mark.parametrize(
    ("headers", "html", "expected"),
    [
        (
            {},
            '<meta name="generator" content="WordPress 6.4.2"><script src="/wp-includes/js/x.js"></script>',
            "WordPress",
        ),
        ({}, '<script src="https://cdn.shopify.com/s/files/x.js"></script>', "Shopify"),
        ({"server": "cloudflare", "cf-ray": "abc"}, "", "Cloudflare"),
        ({"server": "nginx/1.25.3"}, "", "nginx"),
        ({"x-powered-by": "Next.js"}, '<script src="/_next/static/a.js"></script>', "Next.js"),
        ({"x-vercel-id": "iad1::x"}, "", "Vercel"),
        ({"x-wpe-request-id": "1"}, "", "WP Engine"),  # prefix header key
        (
            {},
            '<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>',
            "Google Analytics 4",
        ),
        (
            {},
            '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABCD12"></script>',
            "Google Tag Manager",
        ),
        (
            {},
            '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>',
            "Meta Pixel",
        ),
        (
            {},
            '<link href="https://fonts.googleapis.com/css?family=Roboto" rel="stylesheet">',
            "Google Fonts",
        ),
        ({}, '<script src="https://www.google.com/recaptcha/api.js"></script>', "reCAPTCHA"),
        (
            {},
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
            "Cloudflare Turnstile",
        ),
        ({}, '<script src="https://js.stripe.com/v3/"></script>', "Stripe"),
        ({}, '<script src="https://widget.intercom.io/widget/abc"></script>', "Intercom"),
        ({}, '<script src="https://client.crisp.chat/l.js"></script>', "Crisp"),
        ({}, '<script src="https://cdn.myikas.com/x.js"></script>', "ikas"),
        ({}, '<script src="/static/jquery-3.6.0.min.js"></script>', "jQuery"),
        ({}, '<link rel="stylesheet" href="/css/bootstrap.min.css">', "Bootstrap"),
        ({}, '<script id="__NEXT_DATA__" type="application/json">{}</script>', "Next.js"),
        ({}, '<div ng-version="17.1.0"></div>', "Angular"),
        ({"set-cookie": "laravel_session=abc; path=/\nXSRF-TOKEN=x"}, "", "Laravel"),
        ({"x-powered-by": "PHP/8.2.1"}, "", "PHP"),
        ({"server": "Microsoft-IIS/10.0"}, "", "Microsoft IIS"),
        ({}, '<script src="https://static.hotjar.com/c/hotjar-123.js"></script>', "Hotjar"),
        ({}, '<script src="https://plausible.io/js/script.js"></script>', "Plausible"),
        ({}, '<script src="https://www.clarity.ms/tag/abc"></script>', "Microsoft Clarity"),
        ({}, '<meta name="generator" content="MediaWiki 1.48.0-wmf.5">', "MediaWiki"),
        ({}, '<div class="mw-parser-output"><p>Article</p></div>', "MediaWiki"),  # no generator meta
    ],
)
def test_detects(headers: dict[str, str], html: str, expected: str) -> None:
    metas = [(m.group(1).lower(), m.group(2)) for m in re.finditer(r'<meta name="([^"]+)" content="([^"]*)"', html)]
    assert expected in names(detect(headers, html, metas))


def test_versions_are_captured() -> None:
    s = detect(
        {"server": "nginx/1.25.3"},
        '<meta name="generator" content="WordPress 6.4.2"><script src="/jquery-3.6.0.min.js"></script>',
        [("generator", "WordPress 6.4.2")],
    )
    by = {i.name: i for i in s.items}
    assert by["nginx"].version == "1.25.3"
    assert by["WordPress"].version == "6.4.2"
    mw = detect({}, "", [("generator", "MediaWiki 1.48.0-wmf.5")])
    assert {i.name: i.version for i in mw.items} == {"MediaWiki": "1.48.0"}
    assert by["jQuery"].version == "3.6.0"
    assert by["WordPress"].confidence == "high"
    assert by["WordPress"].website == "https://wordpress.org"
    assert "cms" in s.by_category and "WordPress" in s.by_category["cms"]


def test_plain_text_mentions_are_not_detections() -> None:
    html = (
        "<html><body><p>We migrated from jQuery to React, Bootstrap and WordPress; "
        "Cloudflare and Shopify were considered. Stripe too.</p></body></html>"
    )
    assert detect({}, html, []).items == []


def test_header_value_must_match() -> None:
    assert "Cloudflare" not in names(detect({"server": "my-cloudflare-fan"}, "", []))
    assert detect({"server": "gws"}, "", []).items == []


def test_hostile_huge_html_is_fast() -> None:
    html = ('<div class="a">' + "<script src=" * 2000 + "x" * 50) * 5000  # several MB, malformed
    t0 = time.monotonic()
    detect({"server": "nginx"}, html, [])
    assert time.monotonic() - t0 < 5
    html2 = '<script src="' + "a" * 2_000_000
    t0 = time.monotonic()
    detect({}, html2, [])
    assert time.monotonic() - t0 < 5


def test_multiple_cookies_header() -> None:
    s = detect({"set-cookie": "__cf_bm=1; Secure\nPHPSESSID=abc"}, "", [])
    assert {"Cloudflare", "PHP"} <= names(s)


async def test_techstack_operation_reads_headers_html_and_meta() -> None:
    url = "https://example.com/"
    html = '<html><head><meta name="generator" content="WordPress 6.4.2"></head><body></body></html>'
    async with fakes.session({url: fakes.html_page(html, headers={"server": "nginx/1.25.3"})}) as s:
        res = await techstack(url, session=s)
    assert res.ok and isinstance(res.data, TechStack) and res.findings == []
    by = {i.name: i for i in res.data.items}
    assert by["WordPress"].version == "6.4.2"
    mw = detect({}, "", [("generator", "MediaWiki 1.48.0-wmf.5")])
    assert {i.name: i.version for i in mw.items} == {"MediaWiki": "1.48.0"} and by["nginx"].version == "1.25.3"
    assert s.http.requested == [url]  # type: ignore[attr-defined]  # no extra requests
    assert '"operation":"techstack"' in res.to_json()


async def test_techstack_operation_fails_without_a_response() -> None:
    async with fakes.session({}, errors={"https://example.com/": "unreachable"}) as s:
        res = await techstack("https://example.com/", session=s)
    assert not res.ok
