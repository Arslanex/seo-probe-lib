"""
SSRF policy: which URLs and addresses are acceptable audit targets, and the resolver that
enforces it at connect time.

Derived from the upstream project ``scripts/url_safety.py`` (MIT). Only the
*policy* is ported. The policy functions (``check_url``, ``check_resolved``, ...) are pure: no
DNS lookups, no sockets, no I/O. Resolution and connection pinning are ``SafeResolver``, at the
end of this module.

Deliberately not ported from the source:

* ``LOCAL_TARGETS`` (allow private hosts): a multi-user service must
  never offer it; the MVP is single-user but keeps the same code path.
* the global ``socket.getaddrinfo`` monkey-patch (not safe under concurrency).
* HTTP proxy handling.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse

from aiohttp.abc import AbstractResolver, ResolveResult

from seoprobe.core.errors import ErrorCode


class URLSafetyError(ValueError):
    """
    A URL or address failed the SSRF policy. ``code`` says why: ``INVALID_URL``,
    ``UNSUPPORTED_SCHEME`` or (every other refusal) ``SSRF_BLOCKED``.
    """

    def __init__(self, message: str, code: ErrorCode = ErrorCode.SSRF_BLOCKED) -> None:
        super().__init__(message)
        self.code = code


BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "metadata.azure.com",
        "metadata.ec2.internal",
        "metadata.oraclecloud.com",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "169.254.169.254",  # AWS, Azure, GCP, Oracle, Alibaba metadata IPv4
        "fd00:ec2::254",  # AWS IMDS IPv6
    }
)

# RFC 6598 shared address space (carrier-grade NAT). Not ``is_private`` on any
# supported Python, yet never publicly routable (Alibaba metadata lives at
# 100.100.100.200; Tailscale hands out 100.64/10).
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# Spelled out because the ``ipaddress`` flags differ between Python releases.
_UNSAFE_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/96",  # IPv4-compatible (deprecated); holds :: and ::1
        "64:ff9b::/96",  # NAT64 well-known prefix (RFC 6052)
        "64:ff9b:1::/48",  # NAT64 local-use prefix (RFC 8215)
        "2001::/32",  # Teredo
        "2001:10::/28",  # ORCHID (deprecated)
        "2001:20::/28",  # ORCHIDv2
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4
        "3fff::/20",  # documentation (RFC 9637)
        "fc00::/7",  # unique-local; holds AWS IMDS fd00:ec2::254
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)

# decimal / hex / octal / short IPv4 spellings that glibc's resolver accepts
_IPV4_OBFUSCATED_RE = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}$",
    re.IGNORECASE,
)


def is_safe_ip(ip_str: str) -> bool:
    """
    True iff ``ip_str`` is a public unicast address.

    IPv4-mapped IPv6 is judged as the embedded IPv4 address. Addresses with a
    zone identifier are refused (a zone only means something on the host that
    names it).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id is not None:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in network for network in _UNSAFE_IPV6_NETWORKS):
            return False
    return not (
        ip.is_private
        or (ip.version == 4 and ip in _SHARED_ADDRESS_SPACE)
        or ip.is_loopback
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_dns64_synthesized(ip_str: str) -> bool:
    """True for an address in the NAT64 well-known prefix 64:ff9b::/96."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return isinstance(ip, ipaddress.IPv6Address) and ip.scope_id is None and ip in _NAT64_WELL_KNOWN


def normalize_hostname(hostname: str) -> str:
    """
    Canonicalize a hostname so obfuscated forms cannot bypass the policy.

    Lowercases, strips one trailing dot (``metadata.google.internal.``) and
    rewrites obfuscated IPv4 (``2130706433``, ``0x7f000001``, ``0177.0.0.1``,
    ``127.1``) to dotted-quad via ``inet_aton``, the same canonicalization the
    system resolver would apply at connect time.
    """
    if not hostname:
        raise URLSafetyError("Empty hostname", ErrorCode.INVALID_URL)
    h = hostname.lower().strip()
    if h.endswith(".") and not h.endswith(".."):
        h = h[:-1]
    if _IPV4_OBFUSCATED_RE.match(h):
        try:
            h = socket.inet_ntoa(socket.inet_aton(h))
        except OSError as exc:
            raise URLSafetyError(f"Malformed IPv4 obfuscation refused: {hostname!r} ({exc})") from exc
    return h


def _raw_authority(url: str) -> str:
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)", url)
    return match.group(1) if match else ""


def _reject_authority_confusion(url: str, parsed: ParseResult) -> None:
    """
    Refuse forms where a URL parser and an HTTP stack can disagree.

    Backslashes, userinfo and fragment/userinfo ambiguity have been used to make
    one component see a public host while another connects to a private one.
    Audit URLs never need credentials, so userinfo is refused outright.
    """
    authority = _raw_authority(url)
    authority_lower = authority.lower()
    url_lower = url.lower()
    if "\\" in authority or "%5c" in authority_lower:
        raise URLSafetyError("URL authority contains a backslash")
    if "%" in authority:
        raise URLSafetyError("URL authority contains percent-encoding")
    if parsed.username is not None or parsed.password is not None or "@" in authority:
        raise URLSafetyError("URL userinfo is not allowed")
    if "#@" in url or "%23@" in url_lower:
        raise URLSafetyError("URL fragment/userinfo confusion refused")


@dataclass(frozen=True)
class Target:
    """A URL that passed the static (no-DNS) policy."""

    url: str
    scheme: str
    hostname: str  # normalized
    port: int
    is_ip_literal: bool


def check_url(url: str) -> Target:
    """
    Static policy check, no DNS. Raises ``URLSafetyError``.

    An IP-literal URL is fully decided here. A hostname still needs
    ``check_resolved`` once its addresses are known; ``SafeResolver`` does that
    at connect time. Both are required: aiohttp never consults the resolver for
    IP-literal URLs (verified empirically, see docs: P1), so every request and
    every redirect hop must pass through this function first.
    """
    parsed = urlparse(url)
    _reject_authority_confusion(url, parsed)
    if parsed.scheme not in ("http", "https"):
        code = ErrorCode.UNSUPPORTED_SCHEME if parsed.scheme else ErrorCode.INVALID_URL
        raise URLSafetyError(f"Invalid URL scheme: {parsed.scheme!r}", code)
    if not parsed.hostname:
        raise URLSafetyError("URL has no hostname", ErrorCode.INVALID_URL)
    hostname = normalize_hostname(parsed.hostname)
    try:
        explicit = parsed.port
    except ValueError as exc:
        raise URLSafetyError(f"Invalid port in URL authority: {url!r}", ErrorCode.INVALID_URL) from exc
    port = explicit if explicit is not None else (443 if parsed.scheme == "https" else 80)

    if hostname in BLOCKED_HOSTNAMES:
        raise URLSafetyError(f"Blocked hostname: {hostname}")

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return Target(url, parsed.scheme, hostname, port, is_ip_literal=False)
    if not is_safe_ip(hostname):
        raise URLSafetyError(f"Blocked IP literal: {hostname}")
    return Target(url, parsed.scheme, hostname, port, is_ip_literal=True)


def validate_url(url: str) -> bool:
    """Boolean form of ``check_url``. Does not resolve DNS."""
    try:
        check_url(url)
    except URLSafetyError:
        return False
    return True


def check_resolved(hostname: str, addresses: Iterable[str]) -> list[str]:
    """
    Judge every resolved address of ``hostname``; return the usable ones.

    Every A and AAAA record must be public: a host with one public and one
    private record is refused outright so an attacker cannot race the resolver
    between check and connect. The one record passed over is a DNS64
    synthesized AAAA (64:ff9b::/96) on a host that also has an A record; that
    address is never dialled and is dropped from the result.

    IPv4 sorts first, so a dual-stack host is reached over IPv4 as before.
    """
    ips = sorted({a for a in addresses}, key=lambda ip: (":" in ip, ip))
    if not ips:
        raise URLSafetyError(f"No address records for {hostname}")
    has_a_record = ":" not in ips[0]
    usable: list[str] = []
    for ip in ips:
        if is_safe_ip(ip):
            usable.append(ip)
        elif has_a_record and is_dns64_synthesized(ip):
            continue
        else:
            raise URLSafetyError(f"DNS rebinding refused: {hostname} resolves to non-public IP {ip}")
    return usable


# --------------------------------------------------------------------------- connect-time resolver
# ``SafeResolver``: validate every address at the moment aiohttp connects.
#
# aiohttp connects to the address a resolver returns and keeps the ``Host``
# header and TLS server name as the original hostname (verified empirically in
# the P1 prototype). Judging the addresses *inside* the resolver therefore closes
# the validate-then-connect race of a separate pre-flight check: the address that
# was judged is the address that is dialled, and every redirect hop to a hostname
# goes through the same resolver.
#
# Two limits, both handled elsewhere:
#
# * aiohttp never calls the resolver for IP-literal URLs, so those must pass
#   ``policy.check_url`` first (the fetcher does this for every hop).
# * a resolver cannot stop a redirect to an IP literal on its own, so redirects
#   are followed manually, one validated hop at a time.

# lookup(host, port) -> every address string for the host, all families
Lookup = Callable[[str, int], Awaitable[list[str]]]

_NUMERIC = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV


class BlockedAddressError(URLSafetyError, OSError):
    """
    Policy refusal raised from inside aiohttp's connect path.

    Also an ``OSError`` so aiohttp wraps it as ``ClientConnectorError`` instead
    of letting a bare ``ValueError`` escape.
    """


class DNSResolutionError(URLSafetyError, OSError):
    """DNS failure, surfaced with the same uniform type as a policy refusal."""


async def system_lookup(host: str, port: int) -> list[str]:
    """Resolve through the event loop's resolver; ALL families, never one."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise DNSResolutionError(f"DNS resolution failed for {host}: {exc}") from exc
    return [str(info[4][0]) for info in infos]


class SafeResolver(AbstractResolver):
    def __init__(self, lookup: Lookup = system_lookup) -> None:
        self._lookup = lookup

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        try:
            name = normalize_hostname(host)
        except URLSafetyError as exc:
            raise BlockedAddressError(str(exc)) from exc
        if name in BLOCKED_HOSTNAMES:
            raise BlockedAddressError(f"Blocked hostname: {name}")

        try:
            literal = socket.inet_pton(socket.AF_INET, name)
            literal_ip: str | None = socket.inet_ntop(socket.AF_INET, literal)
        except OSError:
            try:
                literal_ip = socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, name))
            except OSError:
                literal_ip = None
        if literal_ip is not None:
            if not is_safe_ip(literal_ip):
                raise BlockedAddressError(f"Blocked IP literal: {name}")
            addresses = [literal_ip]
        else:
            try:
                resolved = await self._lookup(name, port)
                addresses = check_resolved(name, resolved)
            except URLSafetyError as exc:
                if isinstance(exc, OSError):
                    raise
                raise BlockedAddressError(str(exc)) from exc

        results: list[ResolveResult] = []
        for ip in addresses:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            if family not in (socket.AF_UNSPEC, fam):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": _NUMERIC,
                }
            )
        if not results:
            raise DNSResolutionError(f"No usable {family.name} address for {host}")
        return results

    async def close(self) -> None:
        return None
