"""mod_local: NAP extraction and consistency (belge 12 C4)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import rules
from seoprobe.features.content import FEATURE, local_seo
from seoprobe.features.content.local_seo import (
    LOCAL_RULE_IDS,
    LocalSeo,
    normalise_phone,
    phone_key,
    text_addresses,
    text_phones,
)
from tests import fakes
from tests.features.content.support import HOST, page


def _ld(tel: str, street: str = "Atatürk Cad. No:5", extra_type: str = "Dentist") -> str:
    data = {
        "@context": "https://schema.org",
        "@type": extra_type,
        "name": "Klinik",
        "telephone": tel,
        "address": {
            "@type": "PostalAddress",
            "streetAddress": street,
            "addressLocality": "Ankara",
        },
    }
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def _html(body: str, ld: str = "") -> str:
    return f"<!doctype html><html lang='tr'><head><title>t</title>{ld}</head><body>{body}</body></html>"


async def _run(pages: list[Page], entry_html: str = "") -> Any:
    # the source drove the module with an entry page at HOST/ whose HTML was ``entry_html``
    entry = page(f"{HOST}/", entry_html) if entry_html else Page(url=f"{HOST}/", final_url=f"{HOST}/", status=200)
    async with fakes.session() as s:
        res = await local_seo(entry, pages=pages, session=s)
    assert res.ok
    return res


def test_registered_with_files_and_no_id_clash() -> None:
    RULES = rules()
    assert "local_seo" in {op.name for op in FEATURE.operations}
    for rid in LOCAL_RULE_IDS:
        assert rid in RULES and RULES[rid].heuristic
    assert "local-schema-missing-nap" not in RULES  # schema-missing-key-properties owns it


def test_phone_normalisation_and_key() -> None:
    assert normalise_phone("0 (312) 555 12 34") == "+903125551234"
    assert normalise_phone("+90 312 555 12 34") == "+903125551234"
    assert normalise_phone("0090 312 555 12 34") == "+903125551234"
    assert normalise_phone("12") is None
    assert phone_key("+903125551234") == phone_key("0312 555 12 34") == "3125551234"


def test_text_patterns_need_well_formed_shapes() -> None:
    found = {n for n, _ in text_phones("Ara: 0312 555 12 34 veya +49 30 1234567. Tarih 2024 05 06 12")}
    assert "+903125551234" in found and any(x.startswith("+49") for x in found)
    assert text_phones("Sipariş no 5551234567 ve 2024-05-06") == []
    addrs = text_addresses("Merkez: Atatürk Cad. No:5 Çankaya Ankara\nHakkımızda yazısı")
    assert addrs and "Atatürk Cad" in addrs[0]
    assert text_addresses("Bu bir cadde değil, sadece Cad. kelimesi") == []  # needs a number


async def test_not_applicable_without_local_signals() -> None:
    res = await _run([page("/", _html("<p>Just a blog post about cooking.</p>"))])
    assert res.findings == []
    assert {n.check_id for n in res.not_checked} == {"local", *LOCAL_RULE_IDS}
    assert all(n.reason_code == "not-applicable" for n in res.not_checked)
    assert isinstance(res.data, LocalSeo) and not res.data.applicable


async def test_clean_local_business_has_no_findings() -> None:
    body = '<p>Telefon: <a href="tel:+903125551234">0312 555 12 34</a></p>'
    res = await _run([page("/", _html(body, _ld("+90 312 555 12 34")))])
    assert res.findings == []
    data = res.data
    assert data.applicable and data.has_tel_link and data.business_types == ["Dentist"]  # type: ignore[union-attr]
    assert [p.number for p in data.phones] == ["+903125551234"]  # type: ignore[union-attr]


async def test_different_spellings_of_one_number_are_not_an_inconsistency() -> None:
    p1 = page("/", _html("<p>Ara 0312 555 12 34</p>", _ld("+90 312 555 1234")))
    p2 = page("/iletisim", _html("<p>Tel +90 (312) 555-12-34</p>", _ld("0312-555-1234")))
    res = await _run([p1, p2])
    assert [f.id for f in res.findings] == ["local-no-phone-link"]
    assert len(res.data.phones) == 1 and len(res.data.phones[0].formats) >= 2  # type: ignore[union-attr]


async def test_schema_phone_not_on_page_and_differing_phones() -> None:
    p1 = page("/", _html('<a href="tel:+903125551234">x</a>', _ld("+90 312 999 88 77")))
    p2 = page("/b", _html('<a href="tel:+903125551234">x</a>', _ld("+90 312 555 12 34")))
    res = await _run([p1, p2])
    codes = [i.code for i in res.data.inconsistencies]  # type: ignore[union-attr]
    assert "schema-phone-not-on-page" in codes and "schema-phones-differ" in codes
    assert {f.id for f in res.findings} == {"local-nap-inconsistent"}
    assert all(f.confidence == "medium" for f in res.findings)


async def test_addresses_differ_across_pages() -> None:
    tel = '<a href="tel:+903125551234">x</a>'
    p1 = page("/", _html(tel, _ld("+90 312 555 12 34", "Atatürk Cad. No:5")))
    p2 = page("/b", _html(tel, _ld("+90 312 555 12 34", "Kızılay Sok. No:9")))
    res = await _run([p1, p2])
    assert [i.code for i in res.data.inconsistencies] == ["schema-addresses-differ"]  # type: ignore[union-attr]


async def test_no_phone_link_when_only_text() -> None:
    body = "<p>Arayın: 0312 555 12 34. Adres: Atatürk Cad. No:5 Çankaya</p>"
    res = await _run([page("/", _html(body))])
    assert [f.id for f in res.findings] == ["local-no-phone-link"]
    assert "address-from-text-is-heuristic" in res.data.notes  # type: ignore[union-attr]


async def test_map_embed_counts_only_with_phone_or_address() -> None:
    iframe = '<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>'
    only_map = await _run([page("/", _html(iframe))], entry_html=_html(iframe))
    assert not only_map.data.applicable  # type: ignore[union-attr]
    with_tel = _html(iframe + '<a href="tel:+903125551234">x</a>')
    res = await _run([page("/", with_tel)], entry_html=with_tel)
    assert res.data.applicable and res.data.has_map_embed  # type: ignore[union-attr]


async def test_hostile_huge_text_is_fast() -> None:
    body = "<p>" + ("0 (312) 5 " * 40000) + ("+ 1 2 3 " * 20000) + "</p>"
    t0 = time.perf_counter()
    res = await _run([page("/", _html(body))])
    assert time.perf_counter() - t0 < 3
    assert res.data is not None


async def test_jsonld_walk_is_bounded() -> None:
    nested: dict[str, object] = {"@type": "LocalBusiness", "name": "X"}
    for _ in range(50):
        nested = {"@graph": [nested]}
    ld = f'<script type="application/ld+json">{json.dumps(nested)}</script>'
    res = await _run([page("/", _html("<p>x</p>", ld))])
    assert res.data is not None  # no crash, no recursion error


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Visit us at 425 Madison Ave, New York, NY 10017.", "425 Madison Ave, New York, NY 10017"),
        ("Office: 123 Main St. Springfield", "123 Main St. Springfield"),
        ("10 Downing Street, London", "10 Downing Street, London"),
        ("We have 3 Great Ideas for you", None),
        (  # the address ends at the ZIP code, not 60 characters later
            "425 Madison Ave Suite 403, New York, NY 10017 office@example.com Hours: Mon",
            "425 Madison Ave Suite 403, New York, NY 10017",
        ),
        ("12345 Ocean Drive, Miami, FL 33139 Call us", "12345 Ocean Drive, Miami, FL 33139"),  # house no. is not a ZIP
        ("1 Main Street | Contact us", "1 Main Street"),
    ],
)
def test_english_street_abbreviations_with_or_without_a_dot(text: str, found: str | None) -> None:
    assert text_addresses(text) == ([found] if found else [])
