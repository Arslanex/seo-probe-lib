"""SafeResolver behaviour with an injected lookup (no real DNS, no sockets)."""

from __future__ import annotations

import socket

import pytest

from seoprobe.core.net.safety import BlockedAddressError, DNSResolutionError, SafeResolver


def _lookup(mapping: dict[str, list[str]]):
    async def lookup(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise DNSResolutionError(f"DNS resolution failed for {host}")
        return mapping[host]

    return lookup


async def test_resolves_public_host_and_returns_the_judged_address() -> None:
    r = SafeResolver(_lookup({"example.com": ["93.184.216.34"]}))
    out = await r.resolve("example.com", 443, socket.AF_INET)
    assert out == [
        {
            "hostname": "example.com",
            "host": "93.184.216.34",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 0,
            "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        }
    ]


async def test_refuses_private_resolution() -> None:
    r = SafeResolver(_lookup({"attacker.example": ["10.0.0.7"]}))
    with pytest.raises(BlockedAddressError, match="non-public IP"):
        await r.resolve("attacker.example", 443)


async def test_refuses_mixed_records_even_if_caller_asked_for_ipv4_only() -> None:
    """The private AAAA must poison the host although the caller wants IPv4 only."""
    r = SafeResolver(_lookup({"attacker.example": ["1.2.3.4", "fd00::1"]}))
    with pytest.raises(BlockedAddressError):
        await r.resolve("attacker.example", 443, socket.AF_INET)


async def test_family_filter_after_validation() -> None:
    r = SafeResolver(_lookup({"dual.example": ["1.2.3.4", "2606:4700:4700::1111"]}))
    v6 = await r.resolve("dual.example", 443, socket.AF_INET6)
    assert [x["host"] for x in v6] == ["2606:4700:4700::1111"]
    both = await r.resolve("dual.example", 443, socket.AF_UNSPEC)
    assert [x["host"] for x in both] == ["1.2.3.4", "2606:4700:4700::1111"]


async def test_dns64_aaaa_is_not_returned() -> None:
    r = SafeResolver(_lookup({"v4only.example": ["93.184.216.34", "64:ff9b::5db8:d822"]}))
    out = await r.resolve("v4only.example", 80, socket.AF_UNSPEC)
    assert [x["host"] for x in out] == ["93.184.216.34"]


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "metadata.google.internal.",
        "2130706433",
        "0x7f000001",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "100.100.100.200",
    ],
)
async def test_blocked_names_and_literals_never_reach_lookup(host: str) -> None:
    async def boom(h: str, p: int) -> list[str]:
        raise AssertionError("lookup must not be called")

    with pytest.raises(BlockedAddressError):
        await SafeResolver(boom).resolve(host, 80)


async def test_public_ip_literal_is_allowed() -> None:
    out = await SafeResolver(_lookup({})).resolve("1.1.1.1", 443, socket.AF_INET)
    assert out[0]["host"] == "1.1.1.1"


async def test_dns_failure_has_the_uniform_error_type() -> None:
    with pytest.raises(DNSResolutionError):
        await SafeResolver(_lookup({})).resolve("does-not-exist.example", 443)


async def test_rebinding_every_resolution_is_judged_again() -> None:
    """First answer public, second private: the second connect is refused."""
    answers = iter([["93.184.216.34"], ["10.0.0.7"]])

    async def lookup(host: str, port: int) -> list[str]:
        return next(answers)

    r = SafeResolver(lookup)
    assert (await r.resolve("rebind.example", 443))[0]["host"] == "93.184.216.34"
    with pytest.raises(BlockedAddressError):
        await r.resolve("rebind.example", 443)


def test_errors_are_oserrors_so_aiohttp_wraps_them() -> None:
    assert issubclass(BlockedAddressError, OSError)
    assert issubclass(DNSResolutionError, OSError)
