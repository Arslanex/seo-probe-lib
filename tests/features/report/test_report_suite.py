"""Report sections for the analysis-module blocks."""

from __future__ import annotations

from seoprobe.core.models import NotChecked
from seoprobe.features.audit import Audit
from seoprobe.features.content import (
    KeywordCheck,
    Previews,
    ReadabilityData,
    SchemaSuggestion,
    SchemaSuggestions,
    SerpPreview,
    SocialCard,
    SocialImage,
)
from seoprobe.features.crawl import HreflangEntry, HreflangPageRow, HreflangReport
from seoprobe.features.render import AboveFold, ScreenshotFile, Screenshots
from seoprobe.features.report.build import build_report
from seoprobe.features.report.quality import check_report, missing_keys
from seoprobe.features.site import (
    AgenticCheck,
    AgenticReadiness,
    Probe,
    RedirectVariant,
    SiteSetup,
    TechItem,
    TechStack,
)
from tests.features.report.sample import make_sample_record

XSS = "<script>alert(1)</script>"
SUITE = (
    "techstack",
    "site_setup",
    "previews",
    "screenshots",
    "readability",
    "hreflang",
    "schema_suggestions",
    "agentic",
)


def _extras() -> dict[str, object]:
    return {
        "techstack": TechStack(
            items=[
                TechItem(name="WordPress", category="cms", version="6.5", evidence=XSS, confidence="high"),
                TechItem(name="nginx", category="server", evidence="server header", confidence="medium"),
            ]
        ).model_dump(),
        "site_setup": SiteSetup(
            redirects=[
                RedirectVariant(
                    name="http",
                    variant_url="http://example.com/",
                    status=301,
                    final_url="https://example.com/",
                    final_status=200,
                    hops=1,
                    permanent=True,
                )
            ],
            soft_404={
                "probe_url": "https://example.com/x",
                "status": 404,
                "final_status": 404,
                "detected": False,
            },
            custom_404=True,
            headers={"content_encoding": "br", "server": XSS},
            hsts={
                "present": True,
                "max_age": 31536000,
                "include_subdomains": True,
                "preload": False,
            },
            files={"security_txt": Probe(url="https://example.com/.well-known/security.txt", status=404)},
            requests_made=7,
        ).model_dump(),
        "previews": Previews(
            serp=[
                SerpPreview(
                    device="desktop",
                    display_url="example.com",
                    title=XSS,
                    title_shown=XSS,
                    title_truncated=True,
                    title_px=700,
                    description="",
                    description_shown="",
                    description_truncated=False,
                    description_missing=True,
                )
            ],
            social=SocialCard(
                og={"title": XSS},
                image=SocialImage(
                    url="https://example.com/og.png",
                    reachable=True,
                    width=1200,
                    height=630,
                    ratio=1.9,
                ),
            ),
            notes=["pixel-width-is-approximate"],
        ).model_dump(),
        "screenshots": Screenshots(
            files=[
                ScreenshotFile(
                    device="desktop", name="desktop.jpg", width=1366, height=768, bytes=90_000, data_base64="/9j/AA=="
                )
            ],
            above_fold={"desktop": AboveFold(h1_present=True, h1_visible=True, text_chars=300)},
        ).model_dump(),
        "readability": ReadabilityData(
            language="en",
            readability={
                "formula": "flesch",
                "score": 62.1,
                "label": "easy",
                "words": 400,
                "long_sentence_share": 0.1,
            },
            terms=[{"term": XSS, "count": 9}],
            keywords=[
                KeywordCheck(
                    keyword="seo",
                    in_title=True,
                    in_h1=False,
                    in_description=False,
                    in_url=False,
                    in_first_100_words=True,
                    in_subheadings=False,
                    occurrences=4,
                    density_percent=1.2,
                )
            ],
        ).model_dump(),
        "hreflang": HreflangReport(
            applicable=True,
            languages=["en", "tr"],
            pages=[
                HreflangPageRow(
                    url="https://example.com/",
                    html_lang="en",
                    entries=[HreflangEntry(lang="tr", href="https://example.com/tr/")],
                    issues=["hreflang-no-return-link"],
                )
            ],
        ).model_dump(),
        "schema_suggestions": SchemaSuggestions(
            items=[
                SchemaSuggestion(
                    url="https://example.com/",
                    type="Organization",
                    reason="home page",
                    json_ld='{"@type": "Organization", "name": "<doldurun: ad></script>"}',
                    complete=False,
                    missing_fields=["name"],
                )
            ]
        ).model_dump(),
        "agentic": AgenticReadiness(
            checks=[
                AgenticCheck(id="llms-txt", status="na", detail="absent-optional"),
                AgenticCheck(id="named-controls", status="pass", detail="all-named"),
            ],
            fraction="1/1",
        ).model_dump(),
    }


def _record(extras: dict[str, object] | None) -> Audit:
    rec = make_sample_record("gated")
    rec.extras = dict(extras or {})
    return rec


def test_catalog_parity() -> None:
    assert missing_keys() == set()


def test_sections_are_measured_and_the_report_is_clean() -> None:
    rec = _record(_extras())
    for lang in ("tr", "en"):
        report = build_report(rec, lang)
        for name in SUITE:
            assert getattr(report, name).status == "measured", name
        quality = check_report(report, lang=lang)
        assert not [c for c in quality.checks if c.status == "FAIL"], quality
        assert report.glance.status == "measured"
        shot = report.screenshots.shots[0]
        assert shot.name == "desktop.jpg" and shot.data_base64 == "/9j/AA=="  # the image itself, no app link


def test_sections_without_data_say_why_and_never_show_empty_tables() -> None:
    report = build_report(_record(None), "en")
    for name in SUITE:
        sec = getattr(report, name)
        assert sec.status == "not_measured" and sec.not_measured_text, name
    quality = check_report(report, lang="en")
    assert not [c for c in quality.checks if c.status == "FAIL"], quality


def test_bad_stored_data_is_not_measured_not_a_crash() -> None:
    report = build_report(_record({"techstack": {"items": "nope"}}), "en")
    assert report.techstack.status == "not_measured"
    assert report.techstack.not_measured_reason == "suite-bad-data"


def test_hreflang_not_applicable_and_screenshot_reason_from_notchecked() -> None:
    rec = _record({"hreflang": HreflangReport(applicable=False).model_dump()})
    rec.not_checked.append(NotChecked(check_id="screenshots", reason_code="render-unavailable"))
    report = build_report(rec, "en")
    assert report.hreflang.not_measured_reason == "hreflang-not-applicable"
    assert report.screenshots.not_measured_reason == "render-unavailable"
