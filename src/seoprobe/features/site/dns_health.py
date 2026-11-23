"""
DNS health of a domain: SPF, DMARC, MX, CAA, DNSSEC, IPv6 and name-server diversity.

Only DNS queries, through the system's stub resolver (``dnspython``, extra ``dns``); no found host
is ever connected to. Names taken from records (SPF ``include:``/``redirect=`` targets, name
servers) pass the same public-name check as the input before they are queried, so a record cannot
steer the probe into an internal zone.

Methods and their limits:

* SPF (RFC 7208): one ``v=spf1`` TXT record, term syntax, and the DNS-lookup count of s.4.6.4
  (``include``, ``a``, ``mx``, ``ptr``, ``exists``, ``redirect``), counted through ``include`` and
  ``redirect`` targets with a depth and a follow cap. Macro targets (``%{...}``) are counted but not
  followed. Void-lookup limits and ``mx``/``ptr`` sub-lookups are not counted.
* DMARC (RFC 7489): the ``_dmarc.<domain>`` TXT record only; the organizational-domain fallback
  for subdomains is not applied (pass the registered domain).
* CAA (RFC 8659): the set at the domain, else at the closest parent (the TLD is not queried).
* DNSSEC: a DS record for the domain in its parent zone, i.e. the delegation is signed. A stub
  resolver cannot validate signatures itself; the AD flag of the DS answer is reported as
  ``validated`` and is only meaningful when the system resolver validates.
* Name-server diversity is a heuristic: all IPv4 addresses of the name servers in fewer than
  ``min_ns_subnets`` distinct /24 networks.

Every query has a lifetime (``query_timeout_s``); all queries share one deadline
(``total_timeout_s``) and one count cap (``max_queries``). A query that hits either answers
``timeout`` / ``skipped`` and the affected checks are reported as not checked.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any, Protocol
from urllib.parse import urlparse

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.safety import BLOCKED_HOSTNAMES
from seoprobe.core.rules.catalog import make_finding

from .models import CaaInfo, DmarcInfo, DnsHealth, DnssecInfo, HostAddresses, MxInfo, NsInfo, SpfInfo

# not on the public internet (RFC 6761, RFC 6762, RFC 8375, RFC 9476 and common private zones)
_RESERVED_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "intranet",
    "private",
    "corp",
    "home",
    "lan",
    "test",
    "invalid",
    "example",
    "onion",
    "alt",
    "arpa",
)
_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")  # "_" for service names (_spf, _dmarc)
_SPF_MECH = re.compile(r"^([+\-~?]?)(all|include|a|mx|ptr|ip4|ip6|exists)(?:([:/])(.*))?$", re.I)
_SPF_MOD = re.compile(r"^([a-z][a-z0-9_.\-]*)=(.*)$", re.I)
_LOOKUP_MECHS = frozenset({"include", "a", "mx", "ptr", "exists"})
_EVIDENCE_RECORD = 120


@lru_cache(maxsize=1)
def _defaults() -> dict[str, float]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("dns-health.json").read_text("utf-8"))
    return {k: float(v) for k, v in data.items() if not k.startswith("_")}


# ------------------------------------------------------------------ input


def domain_problem(value: str) -> str | None:
    """Why ``value`` (already normalized) is not a public domain name; None when it is."""
    if not value or len(value) > 253:
        return "invalid-domain"
    try:
        ipaddress.ip_address(value.strip("[]"))
        return "ip-address"
    except ValueError:
        pass
    labels = value.split(".")
    if len(labels) < 2:
        return "single-label"
    if not all(_LABEL.match(label) for label in labels):
        return "invalid-domain"
    if labels[-1].isdigit():
        return "invalid-domain"
    if value in BLOCKED_HOSTNAMES or labels[-1] in _RESERVED_SUFFIXES or value.endswith(".home.arpa"):
        return "reserved-name"
    return None


def normalize_domain(value: str) -> str:
    """Lower-case ASCII (IDNA) form of a domain or of the host of a URL; no trailing dot."""
    raw = value.strip()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    raw = raw.rstrip(".").lower()
    try:
        return raw.encode("idna").decode("ascii")
    except UnicodeError:
        return raw


# ------------------------------------------------------------------ resolver


@dataclass
class DnsAnswer:
    status: str  # ok | noanswer | nxdomain | timeout | error | skipped
    records: list[str] = field(default_factory=list)
    authenticated: bool = False  # AD flag of the response

    @property
    def readable(self) -> bool:
        """The name was answered (with or without records of this type)."""
        return self.status in ("ok", "noanswer", "nxdomain")


class Resolver(Protocol):
    async def query(self, name: str, rdtype: str) -> DnsAnswer: ...


def _rdata_text(rdtype: str, rdata: Any) -> str:
    if rdtype == "TXT":
        return b"".join(rdata.strings).decode("utf-8", "replace")
    if rdtype == "MX":
        return f"{rdata.preference} {rdata.exchange.to_text()}"
    if rdtype == "CAA":
        return f"{rdata.flags} {rdata.tag.decode('ascii', 'replace')} {rdata.value.decode('utf-8', 'replace')}"
    if rdtype == "NS":
        return str(rdata.target.to_text())
    if rdtype in ("A", "AAAA"):
        return str(rdata.address)
    return str(rdata.to_text())


class DnspythonResolver:
    """``dns.asyncresolver`` with one lifetime per query; never raises."""

    def __init__(self, lifetime: float) -> None:
        import dns.asyncresolver

        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = lifetime
        self._resolver.timeout = lifetime

    async def query(self, name: str, rdtype: str) -> DnsAnswer:
        import dns.exception
        import dns.flags
        import dns.resolver

        try:
            answer = await self._resolver.resolve(name, rdtype, raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            return DnsAnswer("nxdomain")
        except dns.exception.Timeout:
            return DnsAnswer("timeout")
        except dns.exception.DNSException:
            return DnsAnswer("error")
        ad = bool(answer.response.flags & dns.flags.AD)
        if answer.rrset is None:
            return DnsAnswer("noanswer", authenticated=ad)
        return DnsAnswer("ok", [_rdata_text(rdtype, r) for r in answer.rrset], ad)


def new_resolver(lifetime: float) -> Resolver:
    """The resolver of one operation run (tests replace this function)."""
    return DnspythonResolver(lifetime)


class _Dns:
    """Shared deadline, query cap and per-run cache in front of a resolver."""

    def __init__(self, resolver: Resolver, *, total_s: float, max_queries: int) -> None:
        self._resolver = resolver
        self._deadline = asyncio.get_running_loop().time() + total_s
        self._left = max_queries
        self._cache: dict[tuple[str, str], asyncio.Task[DnsAnswer]] = {}
        self.queries = 0

    async def query(self, name: str, rdtype: str) -> DnsAnswer:
        key = (name, rdtype)
        if key not in self._cache:
            self._cache[key] = asyncio.ensure_future(self._run(name, rdtype))
        return await self._cache[key]

    async def _run(self, name: str, rdtype: str) -> DnsAnswer:
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return DnsAnswer("timeout")
        if self._left <= 0:
            return DnsAnswer("skipped")
        self._left -= 1
        self.queries += 1
        try:
            async with asyncio.timeout(remaining):
                return await self._resolver.query(name, rdtype)
        except TimeoutError:
            return DnsAnswer("timeout")


def _nc(check_id: str, answer: DnsAnswer, domain: str) -> NotChecked:
    return NotChecked(check_id=check_id, reason_code=f"dns-{answer.status}", url=domain)


def _clip(text: str, limit: int = _EVIDENCE_RECORD) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ------------------------------------------------------------------ SPF


@dataclass
class SpfTerms:
    lookups: int = 0
    includes: list[str] = field(default_factory=list)
    redirect: str | None = None
    all_qualifier: str | None = None
    errors: list[str] = field(default_factory=list)


def spf_records(txt: list[str]) -> list[str]:
    return [t for t in txt if t[:6].lower() == "v=spf1" and (len(t) == 6 or t[6] == " ")]


def parse_spf(record: str) -> SpfTerms:
    """Terms of one ``v=spf1`` record: lookup count of this record, targets to follow, errors."""
    out = SpfTerms()
    for term in record.split()[1:]:
        mech = _SPF_MECH.match(term)
        mod = None if mech else _SPF_MOD.match(term)
        if mod:
            name, value = mod.group(1).lower(), mod.group(2)
            if name in ("redirect", "exp") and not value:
                out.errors.append(f"empty {name}=")
            elif name == "redirect":
                if out.redirect is not None:
                    out.errors.append("more than one redirect=")
                out.redirect = value.lower()
                out.lookups += 1
            continue
        if not mech:
            out.errors.append(f"unknown term {_clip(term, 40)}")
            continue
        qualifier, name, sep, value = mech.group(1), mech.group(2).lower(), mech.group(3), mech.group(4)
        if name == "all":
            if sep:
                out.errors.append(f"bad term {_clip(term, 40)}")
            elif out.all_qualifier is None:
                out.all_qualifier = qualifier or "+"
            continue
        if name in ("include", "exists"):
            if sep != ":" or not value:
                out.errors.append(f"{name} without a domain")
                continue
            if name == "include":
                out.includes.append(value.lower())
        elif name in ("ip4", "ip6"):
            try:
                net = ipaddress.ip_network(value or "", strict=False) if sep == ":" else None
            except ValueError:
                net = None
            if net is None or net.version != (4 if name == "ip4" else 6):
                out.errors.append(f"bad {name} value {_clip(value or '', 40)}")
            continue
        elif sep == ":" and not value:
            out.errors.append(f"{name}: without a domain")
            continue
        if name in _LOOKUP_MECHS:
            out.lookups += 1
    return out


async def _check_spf(
    dns: _Dns, domain: str, limits: dict[str, float], findings: list[Finding], not_checked: list[NotChecked]
) -> SpfInfo:
    info = SpfInfo(lookup_limit=int(limits["spf_lookup_limit"]))
    ids = ("spf-missing", "spf-multiple", "spf-invalid", "spf-too-many-lookups")
    txt = await dns.query(domain, "TXT")
    if not txt.readable:
        not_checked.extend(_nc(i, txt, domain) for i in ids)
        return info
    info.records = spf_records(txt.records)
    if not info.records:
        findings.append(make_finding("spf-missing", url=domain, evidence=f"no v=spf1 TXT record at {domain}"))
        return info
    if len(info.records) > 1:
        evidence = f"{len(info.records)} v=spf1 records: " + " | ".join(_clip(r, 60) for r in info.records[:3])
        findings.append(make_finding("spf-multiple", url=domain, evidence=evidence, count=len(info.records)))
        info.valid = False
        return info

    limit, max_depth, max_follow = info.lookup_limit, int(limits["spf_max_depth"]), int(limits["spf_max_follow"])
    visited = {domain}
    followed = 0
    errors: list[str] = []

    async def walk(name: str, record: str, depth: int) -> None:
        nonlocal followed
        terms = parse_spf(record)
        errors.extend(e if name == domain else f"{name}: {e}" for e in terms.errors)
        info.lookups += terms.lookups
        targets = terms.includes + ([terms.redirect] if terms.redirect and terms.all_qualifier is None else [])
        for target in targets:
            if info.lookups > limit:
                return  # already over the limit; the rest cannot change the verdict
            if "%" in target:
                continue  # macro: expanded per message, cannot be followed
            if depth >= max_depth or followed >= max_follow:
                info.lookups_complete = False
                return
            if domain_problem(target) is not None:
                errors.append(f"{name}: target {_clip(target, 60)} is not a public domain")
                continue
            if target in visited:
                errors.append(f"{name}: loop through {target}")
                continue
            visited.add(target)
            followed += 1
            answer = await dns.query(target, "TXT")
            if not answer.readable:
                info.lookups_complete = False
                continue
            recs = spf_records(answer.records)
            if len(recs) != 1:
                errors.append(f"{name}: {target} has {len(recs)} SPF records")
                continue
            await walk(target, recs[0], depth + 1)

    await walk(domain, info.records[0], 0)
    info.all_qualifier = parse_spf(info.records[0]).all_qualifier
    info.errors = errors
    info.valid = not errors
    if errors:
        findings.append(make_finding("spf-invalid", url=domain, evidence="; ".join(errors), count=len(errors)))
    if info.lookups > limit:
        at_least = "" if info.lookups_complete else "at least "
        findings.append(
            make_finding(
                "spf-too-many-lookups",
                url=domain,
                evidence=f"lookups={at_least}{info.lookups} limit={limit}",
                count=info.lookups,
            )
        )
    elif not info.lookups_complete:
        not_checked.append(_nc("spf-too-many-lookups", DnsAnswer("timeout"), domain))
    return info


# ------------------------------------------------------------------ DMARC


def parse_dmarc(record: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in record.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            tags.setdefault(key.strip().lower(), value.strip())
    return tags


async def _check_dmarc(dns: _Dns, domain: str, findings: list[Finding], not_checked: list[NotChecked]) -> DmarcInfo:
    info = DmarcInfo()
    name = f"_dmarc.{domain}"
    answer = await dns.query(name, "TXT")
    if not answer.readable:
        not_checked.extend(_nc(i, answer, domain) for i in ("dmarc-missing", "dmarc-invalid", "dmarc-policy-none"))
        return info
    records = [t for t in answer.records if t.strip().lower().startswith("v=dmarc1")]
    info.records = len(records)
    if not records:
        findings.append(make_finding("dmarc-missing", url=domain, evidence=f"no v=DMARC1 TXT record at {name}"))
        return info
    info.record = records[0]
    if len(records) > 1:
        evidence = f"{len(records)} v=DMARC1 records at {name}; receivers then apply no policy"
        findings.append(make_finding("dmarc-invalid", url=domain, evidence=evidence))
        return info
    tags = parse_dmarc(records[0])
    info.policy = tags.get("p", "").lower() or None
    info.subdomain_policy = tags.get("sp", "").lower() or None
    pct = tags.get("pct", "")
    info.pct = int(pct) if pct.isdigit() else None
    info.rua = [u.strip() for u in tags.get("rua", "").split(",") if u.strip()]
    info.ruf_present = bool(tags.get("ruf", "").strip())
    if info.policy not in ("none", "quarantine", "reject"):
        evidence = f"p={info.policy or '(missing)'} in {_clip(records[0])}"
        findings.append(make_finding("dmarc-invalid", url=domain, evidence=evidence))
    elif info.policy == "none":
        findings.append(make_finding("dmarc-policy-none", url=domain, evidence=_clip(records[0])))
    if not info.rua:
        findings.append(make_finding("dmarc-no-rua", url=domain, evidence=_clip(records[0])))
    return info


# ------------------------------------------------------------------ MX, CAA, DNSSEC, IPv6, NS


async def _check_mx(dns: _Dns, domain: str, findings: list[Finding], not_checked: list[NotChecked]) -> MxInfo:
    answer = await dns.query(domain, "MX")
    if not answer.readable:
        not_checked.append(_nc("mx-missing", answer, domain))
        return MxInfo()
    hosts = sorted(answer.records, key=lambda r: (int(r.split()[0]) if r.split()[0].isdigit() else 0, r))
    info = MxInfo(hosts=hosts, count=len(hosts), null_mx=hosts == ["0 ."])
    if not hosts:
        findings.append(make_finding("mx-missing", url=domain, evidence=f"no MX record at {domain}"))
    return info


def _parents(domain: str) -> list[str]:
    """``a.b.example.com`` -> itself, ``b.example.com``, ``example.com`` (never the TLD)."""
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


async def _check_caa(dns: _Dns, domain: str, findings: list[Finding], not_checked: list[NotChecked]) -> CaaInfo:
    info = CaaInfo()
    for name in _parents(domain):
        answer = await dns.query(name, "CAA")
        if not answer.readable:
            not_checked.append(_nc("caa-missing", answer, domain))
            return info
        if answer.records:
            info.found_at = name
            info.records = sorted(answer.records)
            for rec in info.records:
                parts = rec.split(" ", 2)
                if len(parts) == 3 and parts[1].lower() in ("issue", "issuewild"):
                    issuer = parts[2].strip('"').split(";", 1)[0].strip()
                    if issuer and issuer not in info.issuers:
                        info.issuers.append(issuer)
            return info
    findings.append(make_finding("caa-missing", url=domain, evidence=f"no CAA record at {domain} or its parents"))
    return info


async def _check_dnssec(dns: _Dns, domain: str, findings: list[Finding], not_checked: list[NotChecked]) -> DnssecInfo:
    answer = await dns.query(domain, "DS")
    if not answer.readable:
        not_checked.append(_nc("dnssec-not-enabled", answer, domain))
        return DnssecInfo()
    info = DnssecInfo(ds_records=len(answer.records), enabled=bool(answer.records), validated=answer.authenticated)
    if not answer.records:
        findings.append(make_finding("dnssec-not-enabled", url=domain, evidence=f"no DS record for {domain}"))
    return info


async def _addresses(dns: _Dns, host: str) -> tuple[HostAddresses, bool]:
    a, aaaa = await asyncio.gather(dns.query(host, "A"), dns.query(host, "AAAA"))
    return HostAddresses(host=host, a=sorted(a.records), aaaa=sorted(aaaa.records)), a.readable and aaaa.readable


async def _check_ipv6(
    dns: _Dns, domain: str, findings: list[Finding], not_checked: list[NotChecked]
) -> list[HostAddresses]:
    hosts = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
    results = await asyncio.gather(*(_addresses(dns, h) for h in hosts))
    if not all(ok for _h, ok in results):
        not_checked.append(NotChecked(check_id="ipv6-missing", reason_code="dns-timeout", url=domain))
    out = [h for h, _ok in results]
    lacking = [h.host for h, ok in results if ok and h.a and not h.aaaa]
    if lacking:
        evidence = "A but no AAAA: " + ", ".join(lacking)
        findings.append(make_finding("ipv6-missing", url=domain, evidence=evidence, count=len(lacking)))
    return out


def _provider(host: str) -> str:
    return ".".join(host.rstrip(".").split(".")[-2:])


async def _check_ns(
    dns: _Dns, domain: str, limits: dict[str, float], findings: list[Finding], not_checked: list[NotChecked]
) -> NsInfo:
    info = NsInfo(min_nameservers=int(limits["min_nameservers"]), min_subnets=int(limits["min_ns_subnets"]))
    names: list[str] = []
    for zone in _parents(domain):
        answer = await dns.query(zone, "NS")
        if not answer.readable:
            not_checked.extend(_nc(i, answer, domain) for i in ("ns-too-few", "ns-not-diverse"))
            return info
        if answer.records:
            info.zone = zone
            names = sorted({r.rstrip(".").lower() for r in answer.records})
            break
    info.count = len(names)
    info.providers = sorted({_provider(n) for n in names})
    if info.count < info.min_nameservers:
        evidence = f"{info.count} name server(s) for {info.zone or domain}; minimum {info.min_nameservers}"
        findings.append(make_finding("ns-too-few", url=domain, evidence=evidence, count=info.count))
    public = [n for n in names if domain_problem(n) is None][: int(limits["max_ns_hosts"])]
    results = await asyncio.gather(*(_addresses(dns, n) for n in public))
    info.hosts = [h for h, _ok in results]
    subnets = {str(ipaddress.ip_network(f"{ip}/24", strict=False)) for h in info.hosts for ip in h.a if _is_ipv4(ip)}
    info.distinct_subnets_24 = len(subnets)
    if info.count >= 2:
        if not subnets:
            not_checked.append(NotChecked(check_id="ns-not-diverse", reason_code="no-ns-addresses", url=domain))
        elif len(subnets) < info.min_subnets:
            evidence = f"{info.count} name servers in {len(subnets)} IPv4 /24 network(s); minimum {info.min_subnets}"
            findings.append(make_finding("ns-not-diverse", url=domain, evidence=evidence, confidence="medium"))
    return info


def _is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


# ------------------------------------------------------------------ operation


@operation("dns_health", output=DnsHealth, requires=Requirements(network=True, extras=["dns"]), timeout_s=40.0)
async def dns_health(
    ctx: Context,
    domain: str,
    *,
    spf_lookup_limit: int | None = None,
    min_nameservers: int | None = None,
    min_ns_subnets: int | None = None,
    query_timeout_s: float | None = None,
    total_timeout_s: float | None = None,
) -> Outcome[DnsHealth]:
    """SPF, DMARC, MX, CAA, DNSSEC, IPv6 and name-server checks of ``domain`` (DNS queries only)."""
    name = normalize_domain(domain)
    problem = domain_problem(name)
    if problem is not None:
        ctx.fail(ErrorCode.INVALID_INPUT, field="domain", reason=problem)
    limits = dict(_defaults())
    given = {
        "spf_lookup_limit": spf_lookup_limit,
        "min_nameservers": min_nameservers,
        "min_ns_subnets": min_ns_subnets,
        "query_timeout_s": query_timeout_s,
        "total_timeout_s": total_timeout_s,
    }
    limits.update({k: float(v) for k, v in given.items() if v is not None})
    dns = _Dns(
        new_resolver(limits["query_timeout_s"]),
        total_s=limits["total_timeout_s"],
        max_queries=int(limits["max_queries"]),
    )
    probe = await dns.query(name, "NS")
    if probe.status == "nxdomain":
        ctx.fail(ErrorCode.DNS_FAILED, domain=name, reason="nxdomain")

    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    parts: list[list[Finding]] = [[] for _ in range(7)]
    skipped: list[list[NotChecked]] = [[] for _ in range(7)]
    spf_t = asyncio.ensure_future(_check_spf(dns, name, limits, parts[0], skipped[0]))
    dmarc_t = asyncio.ensure_future(_check_dmarc(dns, name, parts[1], skipped[1]))
    mx_t = asyncio.ensure_future(_check_mx(dns, name, parts[2], skipped[2]))
    caa_t = asyncio.ensure_future(_check_caa(dns, name, parts[3], skipped[3]))
    dnssec_t = asyncio.ensure_future(_check_dnssec(dns, name, parts[4], skipped[4]))
    ipv6_t = asyncio.ensure_future(_check_ipv6(dns, name, parts[5], skipped[5]))
    ns_t = asyncio.ensure_future(_check_ns(dns, name, limits, parts[6], skipped[6]))
    await asyncio.gather(spf_t, dmarc_t, mx_t, caa_t, dnssec_t, ipv6_t, ns_t)
    for f_part, nc_part in zip(parts, skipped, strict=True):  # fixed order: deterministic output
        findings += f_part
        not_checked += nc_part
    data = DnsHealth(
        domain=name,
        spf=spf_t.result(),
        dmarc=dmarc_t.result(),
        mx=mx_t.result(),
        caa=caa_t.result(),
        dnssec=dnssec_t.result(),
        ipv6=ipv6_t.result(),
        ns=ns_t.result(),
        queries=dns.queries,
        thresholds=limits,
    )
    return Outcome(data=data, findings=findings, not_checked=not_checked)
