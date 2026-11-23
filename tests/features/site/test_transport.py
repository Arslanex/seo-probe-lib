"""Transport checks: rules registry, header grading, DER/TLS judgement, redirects (offline)."""

from __future__ import annotations

import ssl
import time
from typing import Any

import pytest

from seoprobe.core.models import FetchResult, Hop, Severity
from seoprobe.core.net.client import RawResponse
from seoprobe.core.rules import catalog
from seoprobe.features.site import FEATURE, TransportReport, transport
from seoprobe.features.site.transport import (
    HEADER_RULE_IDS,
    analyze_headers,
    check_tls,
    parse_der_certificate,
    run_transport_checks,
    sibling_host,
    split_set_cookie,
)
from tests import fakes

# the rules the source declared in rules_site.py (transport, robots, AI crawler access, sitemap)
SITE_RULE_IDS = tuple(
    rid
    for rid in FEATURE.catalog.rules
    if rid.startswith(
        (
            "tls-",
            "http-",
            "www-",
            "hsts-",
            "csp-",
            "nosniff-",
            "clickjacking-",
            "referrer-",
            "permissions-",
            "cookie-",
            "robots-",
            "ai-",
            "llms-txt-missing",
            "sitemap-",
        )
    )
)

NOW = 1_800_000_000.0  # fixed "current time" for certificate maths


# ------------------------------------------------------------------ registry completeness


def test_every_site_rule_has_all_texts_in_en_and_tr() -> None:
    assert len(SITE_RULE_IDS) == 44
    messages = FEATURE.catalog.messages
    for rid in SITE_RULE_IDS:
        d = catalog.rules()[rid]
        assert d.scope == "site"
        for key in (
            d.message_key,
            d.recommendation_key,
            d.failure_check_key,
            d.leading_indicator_key,
        ):
            for lang in ("en", "tr"):
                assert messages[lang].get(key), (rid, key, lang)


def test_gate_ids_and_source_severities() -> None:
    """Gate ids; severities are the upstream project ones (handshake/not-trusted High)."""
    r = catalog.rules()
    assert r["tls-cert-expired"].severity == Severity.CRITICAL
    assert r["tls-cert-hostname-mismatch"].severity == Severity.CRITICAL
    assert r["tls-cert-not-yet-valid"].severity == Severity.CRITICAL
    assert r["tls-handshake-failed"].severity == Severity.HIGH
    assert r["tls-cert-not-trusted"].severity == Severity.HIGH
    assert r["http-no-https-redirect"].severity == Severity.HIGH
    assert r["hsts-missing"].severity == Severity.MEDIUM and r["hsts-missing"].heuristic


# ------------------------------------------------------------------ header grading

GOOD = {
    "strict-transport-security": "max-age=63072000; includeSubDomains; preload",
    "content-security-policy": "default-src 'none'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=()",
}


def _ids(headers: dict[str, str], cookies: list[str] | None = None, https: bool = True) -> set[str]:
    from seoprobe.features.site.transport import _header_findings

    h = analyze_headers(headers, cookies, https=https)
    h["https"] = https
    return {f.id for f in _header_findings(h, "https://example.com/")}


def test_good_headers_have_no_findings() -> None:
    assert _ids(GOOD) == set()


def test_missing_headers_report_the_source_ids() -> None:
    assert _ids({}) == {
        "hsts-missing",
        "csp-missing",
        "nosniff-missing",
        "clickjacking-protection-missing",
        "referrer-policy-missing",
        "permissions-policy-missing",
    }


def test_hsts_variants() -> None:
    assert "hsts-disabled" in _ids({**GOOD, "strict-transport-security": "max-age=0"})
    assert "hsts-disabled" in _ids({**GOOD, "strict-transport-security": "includeSubDomains"})
    assert "hsts-weak" in _ids({**GOOD, "strict-transport-security": "max-age=3600"})
    assert "hsts-weak" in _ids({**GOOD, "strict-transport-security": "max-age=63072000"})  # no includeSubDomains
    assert "hsts-missing" not in _ids({}, https=False)  # HSTS is only expected on HTTPS


def test_csp_variants() -> None:
    ro = {k: v for k, v in GOOD.items() if k != "content-security-policy"}
    ro["content-security-policy-report-only"] = "default-src 'self'"
    assert "csp-report-only" in _ids(ro)
    weak = {
        **GOOD,
        "content-security-policy": "script-src 'unsafe-inline' *; frame-ancestors 'none'",
    }
    assert "csp-weak" in _ids(weak)


def test_clickjacking_xfo_or_frame_ancestors() -> None:
    no_fa = {
        **GOOD,
        "content-security-policy": "default-src 'none'; base-uri 'none'",
        "x-frame-options": "DENY",
    }
    assert "clickjacking-protection-missing" not in _ids(no_fa)
    assert "clickjacking-protection-missing" in _ids({**no_fa, "x-frame-options": "ALLOW-FROM x"})


def test_referrer_policy_unsafe_and_last_value_wins() -> None:
    assert "referrer-policy-unsafe" in _ids({**GOOD, "referrer-policy": "unsafe-url"})
    assert "referrer-policy-unsafe" not in _ids({**GOOD, "referrer-policy": "unsafe-url, no-referrer"})


def test_cookie_flags_never_leak_values() -> None:
    h = analyze_headers(GOOD, ["sid=SECRETVALUE; Path=/", "ok=1; Secure; HttpOnly; SameSite=Lax"], https=True)
    assert "SECRETVALUE" not in str(h)
    ids = _ids(GOOD, ["sid=SECRETVALUE; Path=/"])
    assert {"cookie-missing-secure", "cookie-missing-httponly", "cookie-missing-samesite"} <= ids
    assert _ids(GOOD, ["ok=1; Secure; HttpOnly; SameSite=Lax"]) == set()


def test_split_set_cookie_keeps_expires_dates() -> None:
    parts = split_set_cookie("a=1; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/, b=2; Secure")
    assert len(parts) == 2 and parts[1].startswith("b=2")


def test_sibling_host() -> None:
    assert sibling_host("example.com") == "www.example.com"
    assert sibling_host("www.example.com") == "example.com"
    assert sibling_host("localhost") is None
    assert sibling_host("1.2.3.4") is None


# ------------------------------------------------------------------ hand-made DER certificate


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def _name(cn: str) -> bytes:
    atv = _der(0x30, _der(0x06, b"\x55\x04\x03") + _der(0x0C, cn.encode()))
    return _der(0x30, _der(0x31, atv))


def _utc(ts: float) -> bytes:
    return _der(0x17, time.strftime("%y%m%d%H%M%SZ", time.gmtime(ts)).encode())


def make_cert(cn: str, sans: list[str], not_before: float, not_after: float) -> bytes:
    san_body = b"".join(_der(0x82, s.encode()) for s in sans)
    san_ext = _der(0x30, _der(0x06, b"\x55\x1d\x11") + _der(0x04, _der(0x30, san_body)))
    exts = _der(0xA3, _der(0x30, san_ext))
    tbs = _der(
        0x30,
        _der(0xA0, _der(0x02, b"\x02"))
        + _der(0x02, b"\x01")
        + _der(0x30, _der(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x0b"))
        + _name("Test CA")
        + _der(0x30, _utc(not_before) + _utc(not_after))
        + _name(cn)
        + _der(
            0x30,
            _der(0x30, _der(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01")) + _der(0x03, b"\x00"),
        )
        + exts,
    )
    return _der(0x30, tbs + _der(0x30, _der(0x06, b"\x2a")) + _der(0x03, b"\x00"))


DAY = 86400.0


def test_parse_der_certificate_roundtrip() -> None:
    der = make_cert("example.com", ["example.com", "*.example.com"], NOW - DAY, NOW + 30 * DAY)
    c = parse_der_certificate(der)
    assert c["subject"]["common_name"] == "example.com"
    assert c["issuer"]["common_name"] == "Test CA"
    assert c["san"] == ["example.com", "*.example.com"]
    assert c["not_after"] - c["not_before"] == 31 * DAY


@pytest.mark.parametrize("bad", [b"", b"\x30", b"\x30\x84\xff\xff\xff\xff", b"\x30\x03\x01\x01", b"A" * 300_000])
def test_parse_der_rejects_garbage_without_crashing(bad: bytes) -> None:
    with pytest.raises(ValueError):
        parse_der_certificate(bad)


def test_parse_der_truncated_real_cert_is_rejected() -> None:
    der = make_cert("example.com", ["example.com"], NOW - DAY, NOW + DAY)
    for cut in range(1, len(der) - 1, 7):
        try:
            parse_der_certificate(der[:cut])
        except ValueError:
            pass  # a ValueError is the only acceptable failure mode


# ------------------------------------------------------------------ check_tls (injected handshake)


def _verified(not_after: str, sans: list[str]) -> dict[str, Any]:
    return {
        "cert": {
            "subject": ((("commonName", "example.com"),),),
            "issuer": ((("organizationName", "CA"),),),
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": not_after,
            "subjectAltName": tuple(("DNS", s) for s in sans),
        },
        "der": None,
        "protocol": "TLSv1.3",
        "cipher": "TLS_AES_256_GCM_SHA384",
        "cipher_bits": 256,
    }


def _scripted(verified: Any, unverified: Any = None):
    calls: list[tuple[str, int, str, bool]] = []

    async def connect(ip: str, port: int, host: str, timeout: float, verify: bool) -> dict[str, Any]:
        calls.append((ip, port, host, verify))
        out = verified if verify else unverified
        if isinstance(out, Exception):
            raise out
        return out  # type: ignore[no-any-return]

    return connect, calls


def _cert_error(code: int, msg: str) -> ssl.SSLCertVerificationError:
    e = ssl.SSLCertVerificationError(1, msg)
    e.verify_code = code
    e.verify_message = msg
    return e


def _lookup(*ips: str):
    async def lookup(host: str, port: int) -> list[str]:
        return list(ips)

    return lookup


def _tls_ids(tls: dict[str, Any], redirects: dict[str, Any] | None = None) -> set[str]:
    from seoprobe.features.site.transport import _tls_findings

    return {f.id for f in _tls_findings(tls, redirects or {}, "https://example.com/")}


async def test_valid_certificate_no_findings_and_pinned_ip() -> None:
    notafter = time.strftime("%b %e %H:%M:%S %Y GMT", time.gmtime(NOW + 200 * DAY))
    connect, calls = _scripted(_verified(notafter, ["example.com", "www.example.com"]))
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert tls["verified"] and tls["covers_host"] and tls["covers_sibling"] and tls["days_until_expiry"] > 190
    assert _tls_ids(tls) == set()
    assert calls == [("93.184.216.34", 443, "example.com", True)]  # connected to the IP, SNI = host


async def test_expired_certificate_is_critical_and_read_unverified() -> None:
    der = make_cert("example.com", ["example.com"], NOW - 400 * DAY, NOW - 10 * DAY)
    connect, calls = _scripted(
        _cert_error(10, "certificate has expired"),
        {"cert": None, "der": der, "protocol": "TLSv1.3", "cipher": "x", "cipher_bits": 1},
    )
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert tls["expired"] is True and tls["verified"] is False
    assert [c[3] for c in calls] == [True, False]
    ids = _tls_ids(tls)
    assert "tls-cert-expired" in ids and "tls-cert-not-trusted" not in ids


async def test_hostname_mismatch() -> None:
    der = make_cert("other.example", ["other.example"], NOW - DAY, NOW + 90 * DAY)
    connect, _ = _scripted(
        _cert_error(62, "Hostname mismatch"),
        {"cert": None, "der": der, "protocol": "TLSv1.2", "cipher": "x", "cipher_bits": 1},
    )
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert tls["covers_host"] is False
    assert _tls_ids(tls) == {"tls-cert-hostname-mismatch"}


async def test_untrusted_chain() -> None:
    der = make_cert("example.com", ["example.com"], NOW - DAY, NOW + 90 * DAY)
    connect, _ = _scripted(
        _cert_error(20, "unable to get local issuer certificate"),
        {"cert": None, "der": der, "protocol": "TLSv1.3", "cipher": "x", "cipher_bits": 1},
    )
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert _tls_ids(tls) == {"tls-cert-not-trusted"}


async def test_not_yet_valid() -> None:
    der = make_cert("example.com", ["example.com"], NOW + 5 * DAY, NOW + 90 * DAY)
    connect, _ = _scripted(
        _cert_error(9, "certificate is not yet valid"),
        {"cert": None, "der": der, "protocol": "TLSv1.3", "cipher": "x", "cipher_bits": 1},
    )
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert "tls-cert-not-yet-valid" in _tls_ids(tls)


async def test_expiring_soon_boundary() -> None:
    for days, expect in ((5, True), (14, True), (15, False)):
        na = time.strftime("%b %e %H:%M:%S %Y GMT", time.gmtime(NOW + days * DAY + 60))
        connect, _ = _scripted(_verified(na, ["example.com"]))
        tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
        assert ("tls-cert-expiring-soon" in _tls_ids(tls)) is expect, days


async def test_handshake_failure() -> None:
    connect, _ = _scripted(ssl.SSLError("handshake failure"))
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert _tls_ids(tls) == {"tls-handshake-failed"}


async def test_connection_refused_is_a_handshake_failure_not_a_crash() -> None:
    connect, _ = _scripted(ConnectionRefusedError("refused"))
    tls = await check_tls("https://example.com/", connect=connect, lookup=_lookup("93.184.216.34"), now=NOW)
    assert _tls_ids(tls) == {"tls-handshake-failed"}


@pytest.mark.parametrize(
    "url,ips",
    [
        ("https://attacker.example/", ("10.0.0.7",)),
        ("https://attacker.example/", ("93.184.216.34", "10.0.0.7")),  # mixed records
        ("https://attacker.example/", ("fd00::1",)),
        ("https://10.0.0.1/", ()),
        ("https://127.0.0.1/", ()),
        ("https://169.254.169.254/", ()),
        ("https://localhost/", ()),
    ],
)
async def test_never_connects_to_non_public_addresses(url: str, ips: tuple[str, ...]) -> None:
    connect, calls = _scripted(_verified("Jan  1 00:00:00 2099 GMT", []))
    tls = await check_tls(url, connect=connect, lookup=_lookup(*ips), now=NOW)
    assert tls["skipped"] == "target-rejected"
    assert calls == []


async def test_dns_failure_is_unreachable_not_rejected() -> None:
    from seoprobe.core.net.safety import DNSResolutionError

    async def lookup(host: str, port: int) -> list[str]:
        raise DNSResolutionError("no such host")

    connect, calls = _scripted(None)
    tls = await check_tls("https://nx.example/", connect=connect, lookup=lookup)
    assert tls["skipped"] == "unreachable" and calls == []


async def test_http_url_skips_tls() -> None:
    connect, calls = _scripted(None)
    assert "skipped" in await check_tls("http://example.com/", connect=connect, lookup=_lookup("93.184.216.34"))
    assert calls == []


# ------------------------------------------------------------------ run_transport_checks (scripted fetcher)


class ScriptedFetcher:
    """Returns canned RawResponse per URL (supports redirect chains); records requests."""

    def __init__(self, table: dict[str, RawResponse]) -> None:
        self.table = table
        self.requested: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        self.requested.append(url)
        return self.table.get(url, RawResponse(url, url, None, {}, b"", False, "unreachable: dns"))

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _redirect(url: str, hops: list[tuple[str, int]], final: str, status: int = 200) -> RawResponse:
    return RawResponse(url, final, status, {}, b"", False, None, [Hop(url=u, status=s) for u, s in hops])


def _fetch(url: str = "https://example.com/", headers: dict[str, str] | None = None, **kw: Any) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status=200,
        headers=headers if headers is not None else dict(GOOD),
        **kw,
    )


def _ok_connect(sans: list[str]):
    na = time.strftime("%b %e %H:%M:%S %Y GMT", time.gmtime(time.time() + 200 * DAY))
    return _scripted(_verified(na, sans))[0]


GOOD_REDIRECTS = {
    "http://example.com/": _redirect("http://example.com/", [("http://example.com/", 301)], "https://example.com/"),
    "https://www.example.com/": _redirect(
        "https://www.example.com/", [("https://www.example.com/", 301)], "https://example.com/"
    ),
}


async def _run(fetch: FetchResult, table: dict[str, RawResponse], sans: list[str] | None = None):
    return await run_transport_checks(
        fetch.url,
        fetch,
        ScriptedFetcher(table),  # type: ignore[arg-type]
        connect=_ok_connect(sans or ["example.com", "www.example.com"]),
        lookup=_lookup("93.184.216.34"),
    )


async def test_clean_site_has_no_findings() -> None:
    out = await _run(_fetch(), GOOD_REDIRECTS)
    assert out.findings == [] and out.not_checked == []


async def test_no_https_redirect_and_chain_and_temporary() -> None:
    f = _fetch()
    out = await _run(
        f,
        {
            **GOOD_REDIRECTS,
            "http://example.com/": _redirect("http://example.com/", [], "http://example.com/"),
        },
    )
    assert [x.id for x in out.findings] == ["http-no-https-redirect"]
    # source semantics: the chain finding fires when the FIRST hop does not already land on https
    chain = _redirect(
        "http://example.com/",
        [("http://example.com/", 301), ("http://www.example.com/", 301)],
        "https://example.com/",
    )
    out = await _run(f, {**GOOD_REDIRECTS, "http://example.com/": chain})
    assert [x.id for x in out.findings] == ["http-https-redirect-chain"]
    temp = _redirect("http://example.com/", [("http://example.com/", 302)], "https://example.com/")
    out = await _run(f, {**GOOD_REDIRECTS, "http://example.com/": temp})
    assert [x.id for x in out.findings] == ["http-https-redirect-temporary"]


async def test_www_not_consolidated_and_unreachable_sibling() -> None:
    f = _fetch()
    other = _redirect("https://www.example.com/", [], "https://www.example.com/")
    out = await _run(f, {**GOOD_REDIRECTS, "https://www.example.com/": other})
    assert [x.id for x in out.findings] == ["www-not-consolidated"]
    tbl = {k: v for k, v in GOOD_REDIRECTS.items() if k != "https://www.example.com/"}
    out = await _run(f, tbl)
    assert [x.id for x in out.findings] == ["www-sibling-unreachable"]


async def test_sibling_tls_error_with_uncovered_name() -> None:
    f = _fetch()
    bad = RawResponse(
        "https://www.example.com/",
        "https://www.example.com/",
        None,
        {},
        b"",
        False,
        "tls-handshake-failed: bad cert",
        [],
    )
    out = await _run(f, {**GOOD_REDIRECTS, "https://www.example.com/": bad}, sans=["example.com"])
    ids = [x.id for x in out.findings]
    assert "tls-sibling-not-covered" in ids


async def test_unreachable_page_marks_headers_not_checked_not_passed() -> None:
    f = FetchResult(url="https://example.com/", final_url="https://example.com/", status=None, error="timeout")
    out = await _run(f, GOOD_REDIRECTS)
    nc = {n.check_id for n in out.not_checked if n.reason_code == "headers-unavailable"}
    assert nc == set(HEADER_RULE_IDS)
    assert not any(x.id.startswith(("hsts", "csp", "cookie")) for x in out.findings)


async def test_http_only_site_skips_tls_and_hsts() -> None:
    f = _fetch("http://example.com/", headers={})
    out = await _run(f, {"http://example.com/": _redirect("http://example.com/", [], "http://example.com/")})
    assert "hsts-missing" not in {x.id for x in out.findings}
    assert "http-no-https-redirect" in {x.id for x in out.findings}
    assert {n.reason_code for n in out.not_checked} >= {"tls-not-applicable"}


async def test_findings_carry_registry_metadata_and_evidence_is_clipped() -> None:
    out = await _run(_fetch(headers={}), GOOD_REDIRECTS)
    for fnd in out.findings:
        d = catalog.rules()[fnd.id]
        assert fnd.category == d.category and fnd.scope == "site" and fnd.layer.value == "A"
        assert fnd.heuristic == d.heuristic and fnd.effort and fnd.impact
        assert fnd.evidence is None or len(fnd.evidence) <= 200
        assert fnd.message == catalog.message(d.message_key, "en")


# ------------------------------------------------------------------ the operation


async def test_transport_operation_on_an_http_site_is_json_serializable() -> None:
    url = "http://example.com/"
    pages = {url: fakes.html_page("<html><head><title>x</title></head></html>")}
    async with fakes.session(pages) as s:
        res = await transport(url, session=s)
    assert res.ok and isinstance(res.data, TransportReport)
    assert res.data.tls == {"skipped": "not an https URL"}
    assert res.data.redirects["http_to_https"]["checked_url"] == url
    assert res.data.headers["https"] is False and res.data.set_cookie_limitation
    assert "http-no-https-redirect" in {f.id for f in res.findings}
    assert res.status == "partial"  # TLS rules are not checked on an http site
    assert {n.reason_code for n in res.not_checked} == {"tls-not-applicable"}
    assert '"operation":"transport"' in res.to_json()


async def test_transport_operation_uses_the_given_page() -> None:
    from seoprobe.core.page import Page

    page = Page(url="http://example.com/", final_url="http://example.com/", status=None, error="timeout")
    async with fakes.session({}) as s:
        res = await transport("http://example.com/", page=page, session=s)
    assert res.ok and res.data is not None and res.data.headers == {"error": "timeout"}
    assert set(HEADER_RULE_IDS) <= {n.check_id for n in res.not_checked}


async def test_transport_operation_fails_when_the_page_cannot_be_fetched() -> None:
    async with fakes.session({}, errors={"http://example.com/": "unreachable"}) as s:
        res = await transport("http://example.com/", session=s)
    assert not res.ok and res.error is not None
