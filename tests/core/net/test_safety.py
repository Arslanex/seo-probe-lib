"""
SSRF policy vectors, ported from the upstream project tests/test_url_safety.py (MIT).

The vectors are the source's; the assertions target ``seoprobe.core.net.safety``.
DNS-dependent cases live in test_resolver.py (they patched ``socket.getaddrinfo``
in the source, which the new design does not use).
"""

from __future__ import annotations

import pytest

from seoprobe.core.net import safety as policy
from seoprobe.core.net.safety import URLSafetyError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("metadata.google.internal.", "metadata.google.internal"),
        ("example.com.", "example.com"),
        ("Example.COM", "example.com"),
        ("2130706433", "127.0.0.1"),  # decimal integer
        ("0x7f000001", "127.0.0.1"),  # hex integer
        ("017700000001", "127.0.0.1"),  # octal integer
        ("127.0.0.001", "127.0.0.1"),  # leading zeros
        ("0177.0.0.1", "127.0.0.1"),  # octal dotted
        ("0x7f.0.0.1", "127.0.0.1"),  # hex dotted
        ("127.1", "127.0.0.1"),  # two-part
        ("127.0.1", "127.0.0.1"),  # three-part
        ("1.1.1.1", "1.1.1.1"),
        ("8.8.8.8", "8.8.8.8"),
    ],
)
def test_normalize_hostname(raw: str, expected: str) -> None:
    assert policy.normalize_hostname(raw) == expected


def test_normalize_hostname_rejects_empty() -> None:
    with pytest.raises(URLSafetyError, match="Empty hostname"):
        policy.normalize_hostname("")


def test_normalize_hostname_passes_through_dns_names() -> None:
    assert policy.normalize_hostname("example.com") == "example.com"
    assert policy.normalize_hostname("sub.deep.example.org") == "sub.deep.example.org"


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://127.0.0.001/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "https://metadata.google.internal./",
        "https://METADATA.GOOGLE.INTERNAL/",
        "http://Metadata.Google.Internal./",
    ],
)
def test_validate_url_blocks_obfuscated_bypasses(url: str) -> None:
    assert policy.validate_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6666\\@1.1.1.1/",
        "https://169.254.169.254\\@example.com/latest/meta-data/",
        "https://user:pass@example.com/",
        "https://127.0.0.1#@example.com/",
        "https://example.com%5c@1.1.1.1/",
        "https://metadata.google.internal%2e/",
        "http://127.0.0.1%2e/",
    ],
)
def test_validate_url_blocks_authority_confusion(url: str) -> None:
    assert policy.validate_url(url) is False
    with pytest.raises(URLSafetyError):
        policy.check_url(url)


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("1.1.1.1", True),
        ("8.8.8.8", True),
        ("104.20.23.154", True),
        ("2606:4700:4700::1111", True),
        ("192.168.1.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),  # AWS/GCP/Azure metadata
        ("100.100.100.200", False),  # Alibaba metadata (RFC 6598 shared space)
        ("100.64.0.0", False),  # RFC 6598 lower bound
        ("100.127.255.255", False),  # RFC 6598 upper bound
        ("100.128.0.1", True),  # first address past the /10 is public
        ("::ffff:100.100.100.200", False),  # IPv4-mapped form of the above
        ("::ffff:127.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),  # IPv6 link-local
        ("fd00::1", False),  # IPv6 unique-local
        ("224.0.0.1", False),  # multicast
        ("not-an-ip", False),
        ("", False),
    ],
)
def test_is_safe_ip(ip: str, expected: bool) -> None:
    assert policy.is_safe_ip(ip) is expected


@pytest.mark.parametrize(
    "ip",
    [
        "fe80::1%eth0",  # zone identifier
        "64:ff9b::808:808",  # NAT64 well-known
        "64:ff9b:1::1",  # NAT64 local-use
        "2002:c000:204::1",  # 6to4
        "2001::1",  # Teredo
        "fd00:ec2::254",  # AWS IMDS IPv6
        "ff02::1",  # multicast
    ],
)
def test_is_safe_ip_refuses_special_ipv6(ip: str) -> None:
    assert policy.is_safe_ip(ip) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1",
        "https://example.com:8443/api",
        "http://1.1.1.1",
        "https://subdomain.example.com",
    ],
)
def test_validate_url_accepts_public(url: str) -> None:
    assert policy.validate_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://192.168.1.1",
        "https://169.254.169.254",
        "https://metadata.google.internal",
        "https://metadata.azure.com",
        "not a url",
        "https://",
        "https://example.com:notaport/",
        "http://[::ffff:100.100.100.200]/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "https://0.0.0.0/",
    ],
)
def test_validate_url_rejects(url: str) -> None:
    assert policy.validate_url(url) is False


def test_check_url_reports_target_fields() -> None:
    t = policy.check_url("https://Example.COM:8443/x")
    assert (t.hostname, t.port, t.scheme, t.is_ip_literal) == ("example.com", 8443, "https", False)
    t = policy.check_url("http://1.1.1.1")
    assert (t.port, t.is_ip_literal) == (80, True)


# ---------------------------------------------------------------- check_resolved


def test_check_resolved_accepts_public_and_prefers_ipv4() -> None:
    assert policy.check_resolved("h", ["2606:4700:4700::1111", "1.2.3.4"]) == [
        "1.2.3.4",
        "2606:4700:4700::1111",
    ]


def test_check_resolved_refuses_private() -> None:
    with pytest.raises(URLSafetyError, match="non-public IP"):
        policy.check_resolved("attacker.example", ["10.0.0.7"])


def test_check_resolved_refuses_mixed_public_and_private() -> None:
    """One private record poisons the whole host (multi-record race)."""
    with pytest.raises(URLSafetyError, match="non-public IP"):
        policy.check_resolved("attacker.example", ["1.2.3.4", "10.0.0.7"])


def test_check_resolved_refuses_public_a_with_private_aaaa() -> None:
    with pytest.raises(URLSafetyError, match="non-public IP"):
        policy.check_resolved("attacker.example", ["1.2.3.4", "fd00::1"])


def test_check_resolved_drops_dns64_aaaa_when_a_record_exists() -> None:
    """DNS64 networks synthesize 64:ff9b::/96 for IPv4-only hosts; never dialled."""
    assert policy.check_resolved("v4only.example", ["93.184.216.34", "64:ff9b::5db8:d822"]) == ["93.184.216.34"]


def test_check_resolved_refuses_nat64_without_a_record() -> None:
    with pytest.raises(URLSafetyError):
        policy.check_resolved("v6only.example", ["64:ff9b::5db8:d822"])


def test_check_resolved_refuses_empty() -> None:
    with pytest.raises(URLSafetyError, match="No address records"):
        policy.check_resolved("h", [])
