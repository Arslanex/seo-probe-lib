"""
Transport checks: TLS certificate, security headers, http->https and www consolidation.

Port of the upstream project ``scripts/transport_check.py`` (MIT): same ids, same
severities, same thresholds (certificate expiry warning at 14 days; HSTS one year).
What changed:

* Header grading runs on ``FetchResult.headers`` (no extra request).
* The TLS socket connects to an address that passed ``policy.check_url`` *and*
  ``policy.check_resolved``; it never connects to a hostname and never to a
  non-public address. The handshake function is injectable (``connect=``) for tests.
* Redirect checks use the validated ``HttpClient`` (manual redirects, per-hop policy).
* Cookie flags: ``Fetcher`` keeps only the *last* value of a repeated header, so with several
  ``Set-Cookie`` lines only one is graded (limitation, reported in facts). Values never leave
  this module; only names do.

Not verified: ``tls-old-protocol`` cannot fire in practice, because the probe refuses TLS < 1.2
(as in the source); it is kept for id parity.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, FetchResult, Finding, NotChecked, Requirements
from seoprobe.core.net.client import HttpClient
from seoprobe.core.net.safety import (
    DNSResolutionError,
    URLSafetyError,
    check_resolved,
    check_url,
    normalize_hostname,
    system_lookup,
)
from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import make_finding

from .models import TransportReport

EXPIRY_WARN_DAYS = 14
ONE_YEAR = 31536000  # hstspreload.org minimum max-age
OLD_PROTOCOLS = ("SSLv3", "TLSv1", "TLSv1.1")
MAX_SAN_REPORTED = 100
MAX_COOKIES = 40
_ERR_NOT_YET_VALID = 9
_ERR_EXPIRED = 10
_ERR_HOST_MISMATCH = (62, 64)  # hostname, IP address

TLS_RULE_IDS = (
    "tls-handshake-failed",
    "tls-cert-expired",
    "tls-cert-not-yet-valid",
    "tls-cert-hostname-mismatch",
    "tls-cert-not-trusted",
    "tls-cert-expiring-soon",
    "tls-old-protocol",
    "tls-sibling-not-covered",
)
HEADER_RULE_IDS = (
    "hsts-missing",
    "hsts-disabled",
    "hsts-weak",
    "csp-missing",
    "csp-report-only",
    "csp-weak",
    "nosniff-missing",
    "clickjacking-protection-missing",
    "referrer-policy-missing",
    "referrer-policy-unsafe",
    "permissions-policy-missing",
    "cookie-missing-secure",
    "cookie-missing-httponly",
    "cookie-missing-samesite",
)

# connect(ip, port, server_hostname, timeout, verify) -> handshake dict
Connect = Callable[[str, int, str, float, bool], Awaitable[dict[str, Any]]]
Lookup = Callable[[str, int], Awaitable[list[str]]]


def _clip(value: object, limit: int = 200) -> str | None:
    return None if value is None else str(value)[:limit]


def _iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ certificate parsing
# ``getpeercert()`` returns a parsed dict only for a verified certificate; for an unverified
# one the stdlib gives DER bytes and no decoder. The bounds-checked walker below reads the few
# fields needed. The input is whatever the remote server sent: every length is checked.

_OID_COMMON_NAME = b"\x55\x04\x03"
_OID_ORGANIZATION = b"\x55\x04\x0a"
_OID_SUBJECT_ALT_NAME = b"\x55\x1d\x11"
_MAX_DER_BYTES = 256 * 1024


def _tlv(data: bytes, pos: int) -> tuple[int, int, int]:
    if pos + 2 > len(data):
        raise ValueError("DER element is truncated")
    tag, first = data[pos], data[pos + 1]
    if tag & 0x1F == 0x1F:
        raise ValueError("multi-byte DER tags are not supported")
    start = pos + 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or start + count > len(data):
            raise ValueError("bad DER length")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    else:
        length = first
    end = start + length
    if end > len(data):
        raise ValueError("DER element runs past the buffer")
    return tag, start, end


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    pos = start
    while pos < end:
        tag, vs, ve = _tlv(data, pos)
        if ve > end:
            raise ValueError("DER child runs past its parent")
        out.append((tag, vs, ve))
        pos = ve
    return out


def _der_text(tag: int, raw: bytes) -> str:
    if tag == 0x1E:
        return raw.decode("utf-16-be", errors="replace")
    if tag == 0x1C:
        return raw.decode("utf-32-be", errors="replace")
    if tag == 0x14:
        return raw.decode("latin-1")
    return raw.decode("utf-8", errors="replace")


def _der_time(tag: int, raw: bytes) -> float:
    text = raw.decode("ascii")
    digits = text[:-1]
    if not text.endswith("Z") or not digits.isdigit():
        raise ValueError("unsupported certificate time format")
    if tag == 0x17 and len(digits) == 12:  # UTCTime
        year = int(digits[:2])
        year += 2000 if year < 50 else 1900
        rest = digits[2:]
    elif tag == 0x18 and len(digits) == 14:  # GeneralizedTime
        year, rest = int(digits[:4]), digits[4:]
    else:
        raise ValueError("unsupported certificate time format")
    month, day, hour, minute, second = (int(rest[i : i + 2]) for i in range(0, 10, 2))
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()


def _der_name(data: bytes, start: int, end: int) -> dict[str, str | None]:
    name: dict[str, str | None] = {"common_name": None, "organization": None}
    for _t, set_start, set_end in _children(data, start, end):
        for _t2, seq_start, seq_end in _children(data, set_start, set_end):
            parts = _children(data, seq_start, seq_end)
            if len(parts) < 2:
                continue
            oid = data[parts[0][1] : parts[0][2]]
            key = {_OID_COMMON_NAME: "common_name", _OID_ORGANIZATION: "organization"}.get(oid)
            if key and name[key] is None:
                name[key] = _clip(_der_text(parts[1][0], data[parts[1][1] : parts[1][2]]))
    return name


def _der_san(data: bytes, start: int, end: int) -> list[str]:
    names: list[str] = []
    wrapper = _children(data, start, end)
    if not wrapper:
        return names
    for _t, ext_start, ext_end in _children(data, wrapper[0][1], wrapper[0][2]):
        parts = _children(data, ext_start, ext_end)
        if len(parts) < 2 or data[parts[0][1] : parts[0][2]] != _OID_SUBJECT_ALT_NAME:
            continue
        general = _children(data, parts[-1][1], parts[-1][2])
        if not general:
            continue
        for tag, ns, ne in _children(data, general[0][1], general[0][2]):
            raw = data[ns:ne]
            if tag == 0x82:
                names.append(raw.decode("ascii", errors="replace"))
            elif tag == 0x87 and len(raw) in (4, 16):
                names.append(str(ipaddress.ip_address(raw)))
    return names


def parse_der_certificate(der: bytes) -> dict[str, Any]:
    """Names, validity (epoch seconds) and subjectAltName from a DER X.509 certificate."""
    if not der or len(der) > _MAX_DER_BYTES:
        raise ValueError("no certificate, or certificate too large")
    try:
        _tag, start, end = _tlv(der, 0)
        tbs = _children(der, start, end)[0]
        fields = _children(der, tbs[1], tbs[2])
        if fields and fields[0][0] == 0xA0:
            fields = fields[1:]
        _serial, _algorithm, issuer, validity, subject = fields[:5]
        times = _children(der, validity[1], validity[2])
        san: list[str] = []
        for tag, fs, fe in fields[6:]:
            if tag == 0xA3:
                san = _der_san(der, fs, fe)
        return {
            "subject": _der_name(der, subject[1], subject[2]),
            "issuer": _der_name(der, issuer[1], issuer[2]),
            "not_before": _der_time(times[0][0], der[times[0][1] : times[0][2]]),
            "not_after": _der_time(times[1][0], der[times[1][1] : times[1][2]]),
            "san": san,
        }
    except (IndexError, UnicodeError, OverflowError) as exc:
        raise ValueError(f"malformed certificate: {exc}") from exc


def _from_peercert(cert: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ``getpeercert()`` dict to the ``parse_der_certificate`` shape."""

    def name(dn: Any) -> dict[str, str | None]:
        out: dict[str, str | None] = {"common_name": None, "organization": None}
        for rdn in dn or ():
            for key, value in rdn:
                slot = {"commonName": "common_name", "organizationName": "organization"}.get(key)
                if slot and out[slot] is None:
                    out[slot] = _clip(value)
        return out

    def seconds(key: str) -> float | None:
        try:
            return float(ssl.cert_time_to_seconds(cert[key]))
        except (KeyError, ValueError):
            return None

    return {
        "subject": name(cert.get("subject")),
        "issuer": name(cert.get("issuer")),
        "not_before": seconds("notBefore"),
        "not_after": seconds("notAfter"),
        "san": [str(v) for kind, v in cert.get("subjectAltName", ()) if kind in ("DNS", "IP Address")],
    }


def _name_matches(pattern: str, host: str) -> bool:
    """SAN match as browsers do it: a wildcard covers exactly one label."""
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    try:
        return ipaddress.ip_address(pattern) == ipaddress.ip_address(host)
    except ValueError:
        pass
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host.count(".") == pattern.count(".")
    return pattern == host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def sibling_host(host: str) -> str | None:
    """The www / non-www counterpart of ``host``; None for IPs and bare labels."""
    if _is_ip(host) or "." not in host:
        return None
    return host[4:] if host.startswith("www.") and host.count(".") >= 2 else f"www.{host}"


# ------------------------------------------------------------------ TLS handshake


def _handshake_blocking(ip: str, port: int, server_hostname: str, timeout: float, verify: bool) -> dict[str, Any]:
    """
    One TLS handshake to ``ip``; sends and reads nothing after it.

    Refuses anything older than TLS 1.2 (stated explicitly so a lowered system OpenSSL floor
    cannot widen the probe). ``verify=False`` only reads the certificate of a site whose
    verified handshake already failed.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if verify:
        if getattr(ssl, "HAS_NEVER_CHECK_COMMON_NAME", False):
            context.hostname_checks_common_name = False  # browsers match subjectAltName only
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((ip, port), timeout=timeout)
    tls = None
    try:
        raw.settimeout(max(0.1, deadline - time.monotonic()))
        tls = context.wrap_socket(raw, server_hostname=server_hostname)
        cipher = tls.cipher() or (None, None, None)
        return {
            "cert": tls.getpeercert() if verify else None,
            "der": None if verify else tls.getpeercert(binary_form=True),
            "protocol": tls.version(),
            "cipher": cipher[0],
            "cipher_bits": cipher[2],
        }
    finally:
        if tls is not None:
            tls.close()
        raw.close()


async def default_connect(ip: str, port: int, server_hostname: str, timeout: float, verify: bool) -> dict[str, Any]:
    return await asyncio.wait_for(
        asyncio.to_thread(_handshake_blocking, ip, port, server_hostname, timeout, verify),
        timeout=timeout + 2,
    )


async def check_tls(
    url: str,
    *,
    connect: Connect = default_connect,
    lookup: Lookup = system_lookup,
    timeout: float = 10.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Read and judge the certificate ``url``'s host presents. Never raises for network errors."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return {"skipped": "not an https URL"}
    try:
        target = check_url(url)
        host = target.hostname
        port = target.port
        if target.is_ip_literal:
            ip = host
        else:
            ip = check_resolved(host, await lookup(host, port))[0]
    except DNSResolutionError as exc:  # before URLSafetyError: it is both
        return {"skipped": "unreachable", "detail": _clip(exc, 300)}
    except URLSafetyError as exc:
        return {"skipped": "target-rejected", "detail": _clip(exc, 300)}
    except OSError as exc:
        return {"skipped": "unreachable", "detail": _clip(exc, 300)}

    sibling = sibling_host(host)
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "connected_ip": ip,
        "verified": False,
        "verify_error": None,
        "verify_code": None,
        "certificate_read": False,
        "subject": None,
        "issuer": None,
        "not_before": None,
        "not_after": None,
        "days_until_expiry": None,
        "expired": None,
        "not_yet_valid": None,
        "san": [],
        "san_count": 0,
        "covers_host": None,
        "sibling_host": sibling,
        "covers_sibling": None,
        "protocol": None,
        "cipher": None,
        "cipher_bits": None,
    }
    cert: dict[str, Any] | None = None
    shake: dict[str, Any] | None = None
    try:
        shake = await connect(ip, port, host, timeout, True)
        result["verified"] = True
        cert = _from_peercert(shake.get("cert") or {})
    except ssl.SSLCertVerificationError as exc:
        result["verify_error"] = _clip(getattr(exc, "verify_message", "") or str(exc), 300)
        result["verify_code"] = getattr(exc, "verify_code", None)
        try:
            shake = await connect(ip, port, host, timeout, False)
            cert = parse_der_certificate(shake.get("der") or b"")
        except (ssl.SSLError, OSError, ValueError, TimeoutError) as inner:
            result["certificate_error"] = _clip(f"{type(inner).__name__}: {inner}", 300)
    except (ssl.SSLError, OSError, ValueError, TimeoutError) as exc:
        result["error"] = _clip(f"{type(exc).__name__}: {exc}", 300)
        return result

    if shake:
        result.update(
            protocol=shake.get("protocol"),
            cipher=shake.get("cipher"),
            cipher_bits=shake.get("cipher_bits"),
        )
    if cert is None:
        return result

    current = time.time() if now is None else now
    names: list[str] = cert["san"]
    result.update(
        certificate_read=True,
        subject=cert["subject"],
        issuer=cert["issuer"],
        not_before=_iso(cert["not_before"]),
        not_after=_iso(cert["not_after"]),
        san=[_clip(n) for n in names[:MAX_SAN_REPORTED]],
        san_count=len(names),
        # A verified handshake already matched the host; OpenSSL is the judge.
        covers_host=True if result["verified"] else any(_name_matches(n, host) for n in names),
    )
    if sibling:
        result["covers_sibling"] = any(_name_matches(n, sibling) for n in names)
    if cert["not_after"] is not None:
        result["days_until_expiry"] = int((cert["not_after"] - current) // 86400)
        result["expired"] = cert["not_after"] < current
    if cert["not_before"] is not None:
        result["not_yet_valid"] = cert["not_before"] > current
    return result


# ------------------------------------------------------------------ header grading (pure)


def split_set_cookie(value: str) -> list[str]:
    """Split a comma-folded Set-Cookie header without breaking Expires dates."""
    if not value:
        return []
    parts = re.split(r",\s*(?=[^;,=\s]+=[^;,]*)", value)
    return [p.strip() for p in parts if p.strip()]


def _parse_cookie(raw: str, https: bool) -> dict[str, Any]:
    """Flags for one cookie. Only the NAME is kept; the value is dropped here."""
    pieces = [p.strip() for p in raw.split(";")]
    name = pieces[0].split("=", 1)[0].strip() if "=" in pieces[0] else ""
    attrs: dict[str, str] = {}
    for piece in pieces[1:]:
        key, _sep, value = piece.partition("=")
        attrs[key.strip().lower()] = value.strip()
    same_site = attrs.get("samesite")
    cookie: dict[str, Any] = {
        "name": name[:80] or "(unnamed)",
        "secure": "secure" in attrs,
        "http_only": "httponly" in attrs,
        "same_site": same_site.capitalize()[:10] if same_site else None,
        "issues": [],
    }
    if https and not cookie["secure"]:
        cookie["issues"].append("Missing Secure on an HTTPS site.")
    if not cookie["http_only"]:
        cookie["issues"].append("Missing HttpOnly; fine only if scripts must read this cookie.")
    if same_site is None:
        cookie["issues"].append("No SameSite attribute; browsers default to Lax.")
    elif same_site.lower() == "none" and not cookie["secure"]:
        cookie["issues"].append("SameSite=None without Secure is rejected by browsers.")
    return cookie


def _grade_hsts(raw: str | None, https: bool) -> dict[str, Any]:
    hsts: dict[str, Any] = {
        "present": raw is not None,
        "value": _clip(raw),
        "max_age": None,
        "include_subdomains": False,
        "preload": False,
        "issues": [],
    }
    if raw is None:
        if https:
            hsts["issues"].append("Strict-Transport-Security was not sent.")
        return hsts
    directives: dict[str, str] = {}
    for part in raw.split(",")[0].split(";"):  # browsers act on the first header only
        key, _sep, value = part.strip().partition("=")
        if key.strip():
            directives.setdefault(key.strip().lower(), value.strip().strip('"'))
    max_age = directives.get("max-age", "")
    hsts["max_age"] = int(max_age) if max_age.isascii() and max_age.isdigit() else None
    hsts["include_subdomains"] = "includesubdomains" in directives
    hsts["preload"] = "preload" in directives
    if hsts["max_age"] is None:
        hsts["issues"].append("max-age is missing, so browsers ignore the header.")
    elif hsts["max_age"] == 0:
        hsts["issues"].append("max-age=0 switches HSTS off.")
    elif hsts["max_age"] < ONE_YEAR:
        hsts["issues"].append("max-age is below one year.")
    if not hsts["include_subdomains"]:
        hsts["issues"].append("includeSubDomains is not set.")
    return hsts


def _parse_csp(value: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for chunk in value.split(";"):
        tokens = chunk.strip().split()
        if tokens:
            directives.setdefault(tokens[0].lower(), [t.lower() for t in tokens[1:]])
    return directives


def _csp_issues(directives: dict[str, list[str]]) -> list[str]:
    issues: list[str] = []
    script_name = next((n for n in ("script-src", "default-src") if n in directives), None)
    if script_name is None:
        issues.append("No script-src or default-src, so scripts are unrestricted.")
    else:
        sources = directives[script_name]
        strict = any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for s in sources)
        if "'unsafe-inline'" in sources and not strict:
            issues.append(f"'unsafe-inline' in {script_name}.")
        if "'unsafe-eval'" in sources:
            issues.append(f"'unsafe-eval' in {script_name}.")
        for loose in ("*", "https:", "http:", "data:"):
            if loose in sources:
                issues.append(f"{loose} in {script_name} allows scripts from almost anywhere.")
    if "object-src" not in directives and directives.get("default-src") != ["'none'"]:
        issues.append("No object-src restriction.")
    if "base-uri" not in directives:
        issues.append("No base-uri restriction.")
    return issues


_REFERRER_POLICIES = frozenset(
    {
        "no-referrer",
        "no-referrer-when-downgrade",
        "origin",
        "origin-when-cross-origin",
        "same-origin",
        "strict-origin",
        "strict-origin-when-cross-origin",
        "unsafe-url",
    }
)


def analyze_headers(
    headers: dict[str, str] | None, set_cookies: list[str] | None = None, *, https: bool = True
) -> dict[str, Any]:
    """Grade the security headers of one response. Makes no requests."""
    low = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    enforced = low.get("content-security-policy")
    report_only = low.get("content-security-policy-report-only")
    policy = enforced if enforced is not None else report_only
    csp: dict[str, Any] = {
        "present": enforced is not None,
        "report_only": enforced is None and report_only is not None,
        "policies": 0,
        "issues": [],
    }
    directives: dict[str, list[str]] = {}
    if policy is None:
        csp["issues"].append("Content-Security-Policy was not sent.")
    else:
        policies = [p for p in policy.split(",") if p.strip()]
        csp["policies"] = len(policies)
        directives = _parse_csp(policies[0]) if policies else {}
        csp["issues"].extend(_csp_issues(directives))
        if len(policies) > 1:
            csp["issues"].append("More than one policy was sent; only the first is graded.")
        if csp["report_only"]:
            csp["issues"].insert(0, "Only a report-only policy was sent, so nothing is enforced.")

    nosniff = low.get("x-content-type-options")
    xfo = low.get("x-frame-options")
    xfo_ok = xfo is not None and xfo.strip().upper() in ("DENY", "SAMEORIGIN")
    frame_ancestors = enforced is not None and "frame-ancestors" in directives
    referrer = low.get("referrer-policy")
    known = [t.strip().lower() for t in (referrer or "").split(",") if t.strip().lower() in _REFERRER_POLICIES]
    effective = known[-1] if known else None  # browsers use the last value they recognise
    return {
        "hsts": _grade_hsts(low.get("strict-transport-security"), https),
        "csp": csp,
        "x_content_type_options": {
            "present": nosniff is not None,
            "value": _clip(nosniff, 60),
            "ok": nosniff is not None and nosniff.strip().lower() == "nosniff",
        },
        "framing": {
            "x_frame_options": _clip(xfo, 60),
            "frame_ancestors": frame_ancestors,
            "ok": xfo_ok or frame_ancestors,
        },
        "referrer_policy": {
            "present": referrer is not None,
            "value": _clip(referrer, 120),
            "ok": effective is not None and effective != "unsafe-url",
        },
        "permissions_policy": {
            "present": "permissions-policy" in low,
            "feature_policy_present": "feature-policy" in low,
        },
        "cookies": [_parse_cookie(line, https) for line in (set_cookies or [])[:MAX_COOKIES] if line.strip()],
    }


# ------------------------------------------------------------------ redirects (via the fetcher)


def _same_url(left: str | None, right: str | None) -> bool | None:
    if not left or not right:
        return None
    a, b = urlparse(left), urlparse(right)
    return (a.scheme, a.netloc.lower(), a.path or "/", a.query) == (
        b.scheme,
        b.netloc.lower(),
        b.path or "/",
        b.query,
    )


def _host_part(host: str) -> str:
    return f"[{host}]" if ":" in host else host


async def check_redirects(url: str, fetch: FetchResult, fetcher: HttpClient) -> dict[str, Any]:
    """http -> https and www / non-www consolidation for ``url``'s host."""
    parsed = urlparse(url)
    try:
        host = normalize_hostname(parsed.hostname or "")
    except URLSafetyError:
        return {"http_to_https": {"skipped": "bad host"}, "www": {"skipped": "bad host"}}
    port = parsed.port
    port_part = f":{port}" if port else ""

    # http -> https
    http_check: dict[str, Any]
    if parsed.scheme == "https" and port not in (None, 443):
        http_check = {"skipped": "https on a non-default port has no http counterpart"}
    else:
        http_url = f"http://{_host_part(host)}{port_part if parsed.scheme == 'http' else ''}/"
        raw = await fetcher.fetch_bytes(http_url, max_bytes=2048)
        chain = raw.redirect_chain or []
        first = chain[0] if chain else None
        first_target = (chain[1].url if len(chain) > 1 else raw.final_url) if first else None
        ft = urlparse(first_target) if first_target else None
        http_check = {
            "checked_url": http_url,
            "redirects_to_https": bool(raw.final_url and raw.final_url.startswith("https://")),
            "one_hop": bool(ft and ft.scheme == "https"),
            "first_hop_status": first.status if first else None,
            "first_hop_target": first_target,
            "first_hop_same_host": ((ft.hostname or "").lower() == host) if ft else None,
            "permanent": (first.status in (301, 308)) if first else None,
            "hops": len(chain),
            "final_url": raw.final_url if raw.error is None else None,
            "error": raw.error,
        }

    # www / non-www
    sib = sibling_host(host)
    www_check: dict[str, Any]
    if sib is None:
        www_check = {"skipped": "no www counterpart for an IP address or a bare label"}
    else:
        root = f"{parsed.scheme}://{_host_part(host)}{port_part}/"
        if (urlparse(fetch.url).path or "/") == "/" and fetch.status is not None:
            host_final: str | None = fetch.final_url
            host_err: str | None = fetch.error
        else:
            hr = await fetcher.fetch_bytes(root, max_bytes=2048)
            host_final = hr.final_url if hr.error is None else None
            host_err = hr.error
        sibling_url = f"{parsed.scheme}://{sib}{port_part}/"
        sr = await fetcher.fetch_bytes(sibling_url, max_bytes=2048)
        sibling_final = sr.final_url if sr.error is None else None
        www_check = {
            "host": host,
            "sibling_host": sib,
            "host_final_url": host_final,
            "sibling_final_url": sibling_final,
            "sibling_resolves": sr.error is None or not (sr.error or "").startswith(("unreachable", "dns-failed")),
            "sibling_tls_error": bool(sr.error and sr.error.startswith("tls-handshake-failed")),
            "same_final_url": _same_url(host_final, sibling_final),
            "error": host_err or sr.error,
        }
    return {"http_to_https": http_check, "www": www_check}


# ------------------------------------------------------------------ findings


def _ev(**kw: Any) -> str:
    return json.dumps({k: v for k, v in kw.items() if v is not None}, ensure_ascii=False)


def _tls_findings(tls: dict[str, Any], redirects: dict[str, Any], url: str) -> list[Finding]:
    if not tls or "skipped" in tls:
        return []

    def mk(i: str, **kw: Any) -> Finding:
        return make_finding(i, url=url, **kw)

    if tls.get("error"):
        return [mk("tls-handshake-failed", evidence=_ev(error=tls["error"]))]
    out: list[Finding] = []
    code = tls.get("verify_code")
    verify = {"verify_error": tls.get("verify_error")}
    expired = bool(tls.get("expired")) or code == _ERR_EXPIRED
    not_yet = bool(tls.get("not_yet_valid")) or code == _ERR_NOT_YET_VALID
    mismatch = not tls["verified"] and (tls.get("covers_host") is False or code in _ERR_HOST_MISMATCH)
    if expired:
        out.append(
            mk(
                "tls-cert-expired",
                evidence=_ev(
                    not_after=tls.get("not_after"),
                    days_until_expiry=tls.get("days_until_expiry"),
                    **verify,
                ),
            )
        )
    if not_yet:
        out.append(mk("tls-cert-not-yet-valid", evidence=_ev(not_before=tls.get("not_before"), **verify)))
    if mismatch:
        out.append(
            mk(
                "tls-cert-hostname-mismatch",
                evidence=_ev(host=tls["host"], san=tls.get("san", [])[:10], **verify),
            )
        )
    if not tls["verified"] and not (expired or not_yet or mismatch):
        out.append(mk("tls-cert-not-trusted", evidence=_ev(**verify)))
    days = tls.get("days_until_expiry")
    if not expired and days is not None and 0 <= days <= EXPIRY_WARN_DAYS:
        out.append(
            mk(
                "tls-cert-expiring-soon",
                evidence=_ev(days_until_expiry=days, not_after=tls.get("not_after")),
            )
        )
    if tls.get("protocol") in OLD_PROTOCOLS:
        out.append(mk("tls-old-protocol", evidence=_ev(protocol=tls["protocol"])))
    www = redirects.get("www", {})
    if tls.get("covers_sibling") is False and www.get("sibling_tls_error") is True:
        out.append(
            mk(
                "tls-sibling-not-covered",
                evidence=_ev(sibling_host=tls.get("sibling_host"), error=www.get("error")),
            )
        )
    return out


def _header_findings(headers: dict[str, Any], url: str) -> list[Finding]:
    def mk(i: str, **kw: Any) -> Finding:
        return make_finding(i, url=url, **kw)

    out: list[Finding] = []
    https = headers.get("https", True)
    hsts = headers["hsts"]
    if https and not hsts["present"]:
        out.append(mk("hsts-missing"))
    elif https and not hsts["max_age"]:
        out.append(mk("hsts-disabled", evidence=_ev(value=hsts["value"])))
    elif https and hsts["issues"]:
        out.append(mk("hsts-weak", evidence=_ev(value=hsts["value"], issues=hsts["issues"])))
    csp = headers["csp"]
    if not csp["present"] and not csp["report_only"]:
        out.append(mk("csp-missing"))
    elif csp["report_only"]:
        out.append(mk("csp-report-only", evidence=_ev(issues=csp["issues"])))
    elif csp["issues"]:
        out.append(mk("csp-weak", evidence=_ev(issues=csp["issues"])))
    nosniff = headers["x_content_type_options"]
    if not nosniff["ok"]:
        out.append(mk("nosniff-missing", evidence=_ev(value=nosniff["value"])))
    framing = headers["framing"]
    if not framing["ok"]:
        out.append(
            mk(
                "clickjacking-protection-missing",
                evidence=_ev(x_frame_options=framing["x_frame_options"]),
            )
        )
    referrer = headers["referrer_policy"]
    if not referrer["present"]:
        out.append(mk("referrer-policy-missing"))
    elif not referrer["ok"]:
        out.append(mk("referrer-policy-unsafe", evidence=_ev(value=referrer["value"])))
    if not headers["permissions_policy"]["present"]:
        out.append(mk("permissions-policy-missing"))

    def names(text: str) -> list[str]:
        return [c["name"] for c in headers["cookies"] if any(text in i for i in c["issues"])]

    insecure = sorted(set(names("Missing Secure") + names("SameSite=None")))
    if insecure:
        out.append(mk("cookie-missing-secure", evidence=_ev(cookies=insecure)))
    if names("HttpOnly"):
        out.append(mk("cookie-missing-httponly", evidence=_ev(cookies=names("HttpOnly"))))
    if names("No SameSite"):
        out.append(mk("cookie-missing-samesite", evidence=_ev(cookies=names("No SameSite"))))
    return out


def _redirect_findings(redirects: dict[str, Any], url: str) -> list[Finding]:
    def mk(i: str, **kw: Any) -> Finding:
        return make_finding(i, url=url, **kw)

    out: list[Finding] = []
    h = redirects.get("http_to_https", {})
    if h.get("final_url"):
        ev = _ev(
            **{
                k: h[k]
                for k in (
                    "checked_url",
                    "first_hop_status",
                    "first_hop_target",
                    "hops",
                    "final_url",
                )
            }
        )
        if not h["redirects_to_https"]:
            out.append(mk("http-no-https-redirect", evidence=ev))
        elif not h["one_hop"]:
            out.append(mk("http-https-redirect-chain", evidence=ev))
        elif not h["permanent"]:
            out.append(mk("http-https-redirect-temporary", evidence=ev))
    w = redirects.get("www", {})
    if w.get("same_final_url") is False:
        out.append(
            mk(
                "www-not-consolidated",
                evidence=_ev(**{k: w[k] for k in ("host", "sibling_host", "host_final_url", "sibling_final_url")}),
            )
        )
    elif "sibling_host" in w and w.get("host_final_url") and not w.get("sibling_final_url"):
        out.append(
            mk(
                "www-sibling-unreachable",
                evidence=_ev(sibling_host=w["sibling_host"], error=w.get("error")),
            )
        )
    return out


# ------------------------------------------------------------------ entry point


async def run_transport_checks(
    entry_url: str,
    fetch: FetchResult,
    fetcher: HttpClient,
    *,
    connect: Connect = default_connect,
    lookup: Lookup = system_lookup,
    now: float | None = None,
    timeout: float = 10.0,
) -> CheckOutput:
    """
    TLS certificate, header grading and redirect checks for the audited site.

    ``fetch`` is the already-fetched entry page (its headers are graded, no extra request).
    """
    page_url = fetch.final_url or entry_url
    scheme = urlparse(page_url).scheme
    not_checked: list[NotChecked] = []

    tls: dict[str, Any] = {"skipped": "not an https URL"}
    if scheme == "https":
        tls = await check_tls(page_url, connect=connect, lookup=lookup, timeout=timeout, now=now)
    if "skipped" in tls:
        reason = "tls-not-applicable" if tls["skipped"] == "not an https URL" else tls["skipped"]
        not_checked += [NotChecked(check_id=i, reason_code=reason, url=page_url) for i in TLS_RULE_IDS]

    redirects = await check_redirects(page_url, fetch, fetcher)

    facts: dict[str, Any] = {"tls": tls, "redirects": redirects}
    findings: list[Finding] = []
    if fetch.status is None or fetch.error:
        not_checked += [
            NotChecked(check_id=i, reason_code="headers-unavailable", url=page_url) for i in HEADER_RULE_IDS
        ]
        facts["headers"] = {"error": fetch.error or "no response"}
    else:
        cookies = split_set_cookie(fetch.headers.get("set-cookie", ""))
        headers = analyze_headers(fetch.headers, cookies, https=scheme == "https")
        headers["https"] = scheme == "https"
        facts["headers"] = headers
        facts["set_cookie_limitation"] = (
            "Only the last Set-Cookie header value is visible (Fetcher keeps one value per name)."
        )
        findings += _header_findings(headers, page_url)

    findings = _tls_findings(tls, redirects, page_url) + _redirect_findings(redirects, page_url) + findings
    return CheckOutput(findings=findings, not_checked=not_checked, facts=facts)


@operation("transport", output=TransportReport, requires=Requirements(network=True), timeout_s=60.0)
async def transport(ctx: Context, url: str, *, page: Page | None = None) -> Outcome[TransportReport]:
    """TLS certificate, security headers, http->https and www consolidation of the site of ``url``."""
    out = await run_transport_checks(url, page or await ctx.page(url), ctx.http)
    report = TransportReport(
        tls=out.facts.get("tls", {}),
        redirects=out.facts.get("redirects", {}),
        headers=out.facts.get("headers", {}),
        set_cookie_limitation=out.facts.get("set_cookie_limitation"),
    )
    return Outcome(data=report, findings=out.findings, not_checked=out.not_checked)
