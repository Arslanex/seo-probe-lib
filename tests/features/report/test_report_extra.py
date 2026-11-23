"""Report sections for local SEO, suggested sitemap and authority."""

from __future__ import annotations

from xml.sax.saxutils import escape

from seoprobe.core.models import AuditMode, NotChecked
from seoprobe.features.audit import Audit
from seoprobe.features.content import AddressEntry, LocalSeo, NapInconsistency, PhoneEntry
from seoprobe.features.offsite import AuthoritySignal
from seoprobe.features.report import build
from seoprobe.features.report.build import build_report
from seoprobe.features.report.quality import check_report, missing_keys
from seoprobe.features.site import ExcludedPage, SitemapGenerated
from tests import fakes
from tests.features.report.sample import make_sample_record

XSS = "<script>alert(1)</script>"


def _xml(urls: list[str]) -> str:
    """The suggested-sitemap XML in the site feature's format."""
    blocks = "".join(f"  <url>\n    <loc>{escape(u)}</loc>\n  </url>\n" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + blocks + "</urlset>\n"
    )


def _sitemap() -> SitemapGenerated:
    # what the site feature generates for "/", "/<xss>" and a 404 page
    urls = ["https://example.com/", f"https://example.com/{XSS}"]
    return SitemapGenerated(
        xml=_xml(urls),
        urls_total=2,
        urls=urls,
        excluded=[ExcludedPage(url="https://example.com/n", reason="status-4xx")],
        excluded_total=1,
        pages_in_sample=3,
        in_sitemap_not_indexable=[ExcludedPage(url=XSS, reason="noindex")],
        in_sitemap_not_indexable_total=1,
        missing_from_sitemap=[XSS],
        missing_total=1,
    )


def _extras() -> dict[str, object]:
    return {
        "local": LocalSeo(
            applicable=True,
            signals=["schema-local-business", "tel-link"],
            business_types=["Dentist"],
            names=[XSS],
            phones=[
                PhoneEntry(
                    number="+903125551234",
                    formats=[XSS],
                    pages=["https://e.com/"],
                    sources=["text"],
                )
            ],
            addresses=[AddressEntry(text=XSS, source="text", pages=["https://e.com/"])],
            inconsistencies=[
                NapInconsistency(
                    kind="phone",
                    code="schema-phones-differ",
                    values=[XSS, "+903125550000"],
                    pages=[XSS],
                )
            ],
            has_tel_link=True,
            pages_scanned=2,
            notes=["address-from-text-is-heuristic"],
        ).model_dump(),
        "sitemap_generated": _sitemap().model_dump(),
        "authority": AuthoritySignal(domain=XSS, page_rank_decimal=4.5, page_rank_integer=4, rank=1234567).model_dump(),
    }


def _record(extras: dict[str, object] | None, mode: AuditMode | None = None) -> Audit:
    rec = make_sample_record("gated", mode=mode)
    rec.extras = dict(extras or {})
    return rec


def test_catalog_parity() -> None:
    assert missing_keys() == set()


def test_sections_measured_and_clean() -> None:
    for lang in ("tr", "en"):
        report = build_report(_record(_extras()), lang)
        for name in ("local", "sitemap_gen", "authority"):
            assert getattr(report, name).status == "measured", name
        quality = check_report(report, lang=lang)
        assert not [c for c in quality.checks if c.status == "FAIL"], quality
        assert XSS not in report.sitemap_gen.xml_preview  # escaped inside the XML
        assert "<urlset" in report.sitemap_gen.xml_preview  # the XML itself; there is no download link
        assert report.sitemap_gen.counts_toward_overall is False


def test_sections_without_data_say_why() -> None:
    rec = _record(None)
    rec.not_checked += [
        NotChecked(check_id="local", reason_code="not-applicable"),
        NotChecked(check_id="sitemap_generated", reason_code="site-mode-only"),
        NotChecked(check_id="authority", reason_code="no-api-key"),
    ]
    report = build_report(rec, "en")
    assert report.local.not_measured_reason == "not-applicable"
    assert report.sitemap_gen.not_measured_reason == "site-mode-only"
    assert report.authority.not_measured_reason == "no-api-key"
    assert "Config.openpagerank_key" in (report.authority.not_measured_text or "")
    quality = check_report(report, lang="en")
    assert not [c for c in quality.checks if c.status == "FAIL"], quality


def test_preview_is_capped_and_bad_data_is_not_measured() -> None:
    big = SitemapGenerated(xml=_xml([f"https://example.com/p{i}" for i in range(250)]), urls_total=250)
    rec = _record({"sitemap_generated": big.model_dump(), "local": {"phones": "nope"}})
    report = build_report(rec, "en")
    assert report.sitemap_gen.urls_shown == 200
    assert report.sitemap_gen.xml_preview.count("<url>") == 200
    assert report.local.not_measured_reason == "suite-bad-data"


async def test_report_json_has_new_sections() -> None:
    async with fakes.session() as session:
        res = await build(_record(_extras()), lang="en", session=session)
    body = res.to_dict()["data"]
    for key in ("local", "sitemap_gen", "authority"):
        assert body[key]["status"] == "measured"
    assert body["schema_version"] == "1.6.0"
