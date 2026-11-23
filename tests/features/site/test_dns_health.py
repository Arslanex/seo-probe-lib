"""dns_health: SPF, DMARC, MX, CAA, DNSSEC, IPv6, NS on a fake resolver (no real DNS)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from seoprobe.core.rules import catalog
from seoprobe.features.site import FEATURE, DnsHealth, dns_health
from tests import fakes

M = importlib.import_module("seoprobe.features.site.dns_health")
D = "example.com"
NEW_IDS = (
    "spf-missing",
    "spf-multiple",
    "spf-invalid",
    "spf-too-many-lookups",
    "dmarc-missing",
    "dmarc-invalid",
    "dmarc-policy-none",
    "dmarc-no-rua",
    "mx-missing",
    "caa-missing",
    "dnssec-not-enabled",
    "ipv6-missing",
    "ns-too-few",
    "ns-not-diverse",
)

Zone = dict[tuple[str, str], Any]


def healthy() -> Zone:
    return {
        (D, "NS"): ["ns1.dnshost.net.", "ns2.dnshost.org."],
        (D, "TXT"): ["v=spf1 include:_spf.mail.net ip4:203.0.113.0/24 -all", "google-site-verification=x"],
        ("_spf.mail.net", "TXT"): ["v=spf1 ip4:198.51.100.0/24 ~all"],
        (f"_dmarc.{D}", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@example.com; pct=100"],
        (D, "MX"): ["10 mx2.example.com.", "5 mx1.example.com."],
        (D, "CAA"): ['0 issue "letsencrypt.org"', '0 iodef "mailto:s@example.com"'],
        (D, "DS"): ["12345 13 2 ABCDEF"],
        (D, "A"): ["93.184.216.34"],
        (D, "AAAA"): ["2606:2800:220:1::1"],
        (f"www.{D}", "A"): ["93.184.216.34"],
        (f"www.{D}", "AAAA"): ["2606:2800:220:1::1"],
        ("ns1.dnshost.net", "A"): ["192.0.2.53"],
        ("ns2.dnshost.org", "A"): ["198.51.100.53"],
    }


class FakeResolver:
    """(name, type) -> list of records | "nxdomain" | "timeout" | "error". Unknown -> no answer."""

    def __init__(self, zone: Zone, *, ad: bool = False, nx: set[str] | None = None) -> None:
        self.zone = zone
        self.ad = ad
        self.nx = nx or set()
        self.asked: list[tuple[str, str]] = []

    async def query(self, name: str, rdtype: str) -> Any:
        self.asked.append((name, rdtype))
        if name in self.nx:
            return M.DnsAnswer("nxdomain")
        value = self.zone.get((name, rdtype), [])
        if isinstance(value, str):
            return M.DnsAnswer(value)
        return M.DnsAnswer("ok" if value else "noanswer", list(value), self.ad)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Any:
    holder: dict[str, FakeResolver] = {"r": FakeResolver(healthy())}
    monkeypatch.setattr(M, "new_resolver", lambda lifetime: holder["r"])

    def use(zone: Zone, **kw: Any) -> FakeResolver:
        holder["r"] = FakeResolver(zone, **kw)
        return holder["r"]

    return use


async def run(domain: str = D, **kw: Any) -> Any:
    async with fakes.session() as s:
        return await dns_health(domain, session=s, **kw)


def ids(res: Any) -> list[str]:
    return [f.id for f in res.findings]


# ------------------------------------------------------------------ whole operation


async def test_healthy_domain_has_no_findings(resolver: Any) -> None:
    r = resolver(healthy(), ad=True)
    res = await run()
    assert res.status == "ok" and ids(res) == [] and res.not_checked == []
    d = res.data
    assert isinstance(d, DnsHealth) and d.domain == D
    assert d.spf.valid and d.spf.lookups == 1 and d.spf.all_qualifier == "-" and d.spf.lookup_limit == 10
    assert d.dmarc.policy == "reject" and d.dmarc.rua == ["mailto:d@example.com"] and d.dmarc.pct == 100
    assert d.mx.hosts == ["5 mx1.example.com.", "10 mx2.example.com."] and d.mx.count == 2
    assert d.caa.found_at == D and d.caa.issuers == ["letsencrypt.org"]
    assert d.dnssec.enabled and d.dnssec.validated and d.dnssec.ds_records == 1
    assert [h.host for h in d.ipv6] == [D, f"www.{D}"] and all(h.aaaa for h in d.ipv6)
    assert d.ns.count == 2 and d.ns.distinct_subnets_24 == 2 and d.ns.providers == ["dnshost.net", "dnshost.org"]
    assert d.queries == len(r.asked) == len(set(r.asked))  # each (name, type) asked once
    assert d.thresholds["spf_lookup_limit"] == 10 and d.thresholds["query_timeout_s"] == 3
    back = DnsHealth.model_validate_json(d.model_dump_json())
    assert back == d and '"operation":"dns_health"' in res.to_json()


async def test_output_is_deterministic(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = []
    zone[(f"_dmarc.{D}", "TXT")] = []
    zone[(D, "CAA")] = []
    resolver(zone)
    first = await run()
    resolver(zone)
    second = await run()
    assert first.model_dump(exclude={"meta"}) == second.model_dump(exclude={"meta"})
    assert ids(first) == ["spf-missing", "dmarc-missing", "caa-missing"]


async def test_url_and_case_are_normalized(resolver: Any) -> None:
    resolver(healthy())
    res = await run("https://EXAMPLE.com./path")
    assert res.ok and res.data.domain == D


@pytest.mark.parametrize(
    "value,reason",
    [
        ("93.184.216.34", "ip-address"),
        ("[2001:db8::1]", "ip-address"),
        ("localhost", "single-label"),
        ("intranet", "single-label"),
        ("printer.local", "reserved-name"),
        ("db.corp.internal", "reserved-name"),
        ("router.home.arpa", "reserved-name"),
        ("metadata.google.internal", "reserved-name"),
        ("foo.test", "reserved-name"),
        ("-bad-.com", "invalid-domain"),
        ("a..b.com", "invalid-domain"),
        ("", "invalid-domain"),
        ("x" * 64 + ".com", "invalid-domain"),
    ],
)
async def test_invalid_domains_are_rejected_without_queries(resolver: Any, value: str, reason: str) -> None:
    r = resolver(healthy())
    res = await run(value)
    assert not res.ok and res.error.code == "invalid-input" and res.error.details["reason"] == reason
    assert r.asked == []


async def test_nonexistent_domain_fails_with_dns_failed(resolver: Any) -> None:
    resolver({}, nx={"nope.example.com"})
    res = await run("nope.example.com")
    assert not res.ok and res.error.code == "dns-failed"


async def test_missing_dns_extra(resolver: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: None if n == "dns" else real(n, *a))
    res = await run()
    assert not res.ok and res.error.code == "missing-extra" and "extra:dns" in res.error.details["missing"]
    assert "dns" in dns_health.requires.extras


# ------------------------------------------------------------------ SPF


def test_parse_spf_counts_lookup_terms_and_reports_syntax() -> None:
    t = M.parse_spf("v=spf1 a mx:mail.example.com ptr exists:%{i}.x.com include:a.com redirect=b.com ip4:1.2.3.4 -all")
    assert t.lookups == 6 and t.includes == ["a.com"] and t.redirect == "b.com" and t.all_qualifier == "-"
    assert t.errors == []
    bad = M.parse_spf("v=spf1 ip4:999.1.1.1 ip6:1.2.3.4 include: foo:bar all:x redirect= ~all")
    assert bad.errors == [
        "bad ip4 value 999.1.1.1",
        "bad ip6 value 1.2.3.4",
        "include without a domain",
        "unknown term foo:bar",
        "bad term all:x",
        "empty redirect=",
    ]
    assert M.spf_records(["v=spf1 -all", "v=spf10", "V=SPF1", "x"]) == ["v=spf1 -all", "V=SPF1"]


async def test_spf_multiple(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = ["v=spf1 -all", "v=spf1 include:x.com -all"]
    resolver(zone)
    res = await run()
    assert ids(res) == ["spf-multiple"] and res.findings[0].count == 2 and res.data.spf.valid is False


async def test_spf_too_many_lookups_counts_recursively(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = ["v=spf1 include:a.net include:b.net mx -all"]
    zone[("a.net", "TXT")] = ["v=spf1 include:c.net a mx ptr ~all"]
    zone[("b.net", "TXT")] = ["v=spf1 a mx exists:x.b.net ~all"]
    zone[("c.net", "TXT")] = ["v=spf1 a mx ~all"]
    resolver(zone)
    res = await run()
    # root 3 + a.net 4 + c.net 2 = 9 -> b.net adds 3 -> 12
    f = next(x for x in res.findings if x.id == "spf-too-many-lookups")
    assert f.evidence == "lookups=12 limit=10" and f.count == 12
    assert res.data.spf.lookups == 12 and res.data.spf.lookups_complete


async def test_spf_limit_is_a_parameter(resolver: Any) -> None:
    resolver(healthy())
    res = await run(spf_lookup_limit=0)
    f = next(x for x in res.findings if x.id == "spf-too-many-lookups")
    assert f.evidence == "lookups=1 limit=0" and res.data.thresholds["spf_lookup_limit"] == 0


async def test_spf_broken_targets_loops_and_internal_names(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = ["v=spf1 include:gone.net include:loop.net include:x.corp.internal include:%{d}.m.net -all"]
    zone[("loop.net", "TXT")] = [f"v=spf1 include:{D} ~all"]
    r = resolver(zone)
    res = await run()
    errors = res.data.spf.errors
    assert "gone.net has 0 SPF records" in errors[0] and f"loop through {D}" in errors[1]
    assert "x.corp.internal is not a public domain" in errors[2] and len(errors) == 3
    assert "spf-invalid" in ids(res) and res.data.spf.valid is False
    assert ("x.corp.internal", "TXT") not in r.asked  # never queried
    assert not any("%" in n for n, _t in r.asked)  # macros are not followed


async def test_spf_redirect_followed_only_without_all(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = ["v=spf1 redirect=_spf.mail.net"]
    r = resolver(zone)
    res = await run()
    assert ("_spf.mail.net", "TXT") in r.asked and res.data.spf.lookups == 1
    zone[(D, "TXT")] = ["v=spf1 redirect=other.net -all"]
    r = resolver(zone)
    await run()
    assert ("other.net", "TXT") not in r.asked


async def test_spf_depth_cap_marks_the_count_incomplete(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = ["v=spf1 include:l1.net -all"]
    for i in range(1, 15):
        zone[(f"l{i}.net", "TXT")] = [f"v=spf1 include:l{i + 1}.net ~all"] if i < 9 else ["v=spf1 ~all"]
    resolver(zone)
    res = await run(spf_lookup_limit=50)
    assert res.data.spf.lookups == 9 and res.data.spf.lookups_complete
    zone[("l9.net", "TXT")] = ["v=spf1 include:l10.net ~all"]
    zone[("l10.net", "TXT")] = ["v=spf1 include:l11.net ~all"]
    resolver(zone)
    res = await run(spf_lookup_limit=50)
    assert not res.data.spf.lookups_complete
    assert ("spf-too-many-lookups", "dns-timeout") in {(n.check_id, n.reason_code) for n in res.not_checked}


async def test_unreadable_txt_is_not_checked_not_missing(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "TXT")] = "timeout"
    resolver(zone)
    res = await run()
    assert "spf-missing" not in ids(res) and res.status == "partial"
    assert {n.check_id for n in res.not_checked} == {
        "spf-missing",
        "spf-multiple",
        "spf-invalid",
        "spf-too-many-lookups",
    }


# ------------------------------------------------------------------ DMARC, MX, CAA, DNSSEC, IPv6, NS


@pytest.mark.parametrize(
    "records,expected",
    [
        ([], ["dmarc-missing"]),
        (["v=DMARC1; p=none; rua=mailto:a@x.com"], ["dmarc-policy-none"]),
        (["v=DMARC1; p=quarantine"], ["dmarc-no-rua"]),
        (["v=DMARC1; rua=mailto:a@x.com"], ["dmarc-invalid"]),
        (["v=DMARC1; p=block; rua=mailto:a@x.com"], ["dmarc-invalid"]),
        (["v=DMARC1; p=reject", "v=DMARC1; p=none"], ["dmarc-invalid"]),
        (["some other txt"], ["dmarc-missing"]),
    ],
)
async def test_dmarc(resolver: Any, records: list[str], expected: list[str]) -> None:
    zone = healthy()
    zone[(f"_dmarc.{D}", "TXT")] = records
    resolver(zone)
    assert ids(await run()) == expected


async def test_mx_missing_and_null_mx(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "MX")] = []
    resolver(zone)
    assert ids(await run()) == ["mx-missing"]
    zone[(D, "MX")] = ["0 ."]
    resolver(zone)
    res = await run()
    assert ids(res) == [] and res.data.mx.null_mx


async def test_caa_climbs_to_the_parent_and_dnssec_missing(resolver: Any) -> None:
    sub = f"shop.{D}"
    zone = healthy()
    zone.update({(sub, "NS"): [], (sub, "A"): ["93.184.216.34"], (sub, "AAAA"): ["2606:2800:220:1::1"]})
    zone[(f"www.{sub}", "A")] = []
    zone[(f"_dmarc.{sub}", "TXT")] = zone[(f"_dmarc.{D}", "TXT")]
    zone[(sub, "TXT")] = ["v=spf1 -all"]
    zone[(sub, "MX")] = ["10 mx.example.com."]
    r = resolver(zone)
    res = await run(sub)
    assert res.data.caa.found_at == D and res.data.ns.zone == D
    assert ("com", "CAA") not in r.asked and ("com", "NS") not in r.asked
    assert ids(res) == ["dnssec-not-enabled"]  # no DS for the subdomain
    assert res.data.dnssec.enabled is False and res.data.dnssec.method == "ds-at-parent"
    zone[(D, "CAA")] = []
    resolver(zone)
    assert "caa-missing" in ids(await run())


async def test_ipv6_missing_for_www(resolver: Any) -> None:
    zone = healthy()
    zone[(f"www.{D}", "AAAA")] = []
    resolver(zone)
    res = await run()
    f = next(x for x in res.findings if x.id == "ipv6-missing")
    assert f.evidence == f"A but no AAAA: www.{D}" and f.count == 1


async def test_host_without_any_address_is_not_an_ipv6_gap(resolver: Any) -> None:
    zone = healthy()
    zone[(f"www.{D}", "A")] = []
    zone[(f"www.{D}", "AAAA")] = []
    resolver(zone)
    assert "ipv6-missing" not in ids(await run())


async def test_ns_too_few_and_not_diverse(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "NS")] = ["ns1.dnshost.net."]
    resolver(zone)
    res = await run()
    f = next(x for x in res.findings if x.id == "ns-too-few")
    assert f.evidence == f"1 name server(s) for {D}; minimum 2"
    zone = healthy()
    zone[("ns2.dnshost.org", "A")] = ["192.0.2.54"]  # same /24 as ns1
    resolver(zone)
    res = await run()
    f = next(x for x in res.findings if x.id == "ns-not-diverse")
    assert f.heuristic and f.confidence == "medium" and "minimum 2" in (f.evidence or "")
    assert res.data.ns.distinct_subnets_24 == 1
    resolver(zone)
    assert "ns-not-diverse" not in ids(await run(min_ns_subnets=1))


async def test_internal_name_servers_are_not_resolved(resolver: Any) -> None:
    zone = healthy()
    zone[(D, "NS")] = ["ns1.dnshost.net.", "ns.corp.internal."]
    r = resolver(zone)
    res = await run()
    assert ("ns.corp.internal", "A") not in r.asked and [h.host for h in res.data.ns.hosts] == ["ns1.dnshost.net"]


# ------------------------------------------------------------------ caps and timeouts


async def test_query_cap_skips_the_rest(resolver: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    base = M._defaults()
    monkeypatch.setattr(M, "_defaults", lambda: {**base, "max_queries": 3.0})
    r = resolver(healthy())
    res = await run()
    assert res.ok and res.data.queries == 3 == len(r.asked) and res.status == "partial"
    assert {n.reason_code for n in res.not_checked} <= {"dns-skipped", "dns-timeout", "no-ns-addresses"}


async def test_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class Slow(FakeResolver):
        async def query(self, name: str, rdtype: str) -> Any:
            if (name, rdtype) != (D, "NS"):
                await asyncio.sleep(5)
            return await super().query(name, rdtype)

    monkeypatch.setattr(M, "new_resolver", lambda lifetime: Slow(healthy()))
    res = await run(total_timeout_s=0.05)
    assert res.ok and res.status == "partial" and res.findings == []
    assert {n.reason_code for n in res.not_checked} <= {"dns-timeout", "no-ns-addresses"}


# ------------------------------------------------------------------ the real resolver adapter


async def test_dnspython_adapter_converts_records_and_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import dns.asyncresolver
    import dns.exception
    import dns.flags
    import dns.rdata
    import dns.rdataclass
    import dns.rdatatype
    import dns.resolver

    def rd(rdtype: str, text: str) -> Any:
        return dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rdtype), text)

    data = {
        ("t.com", "TXT"): [rd("TXT", '"v=spf1 " "-all"')],
        ("t.com", "MX"): [rd("MX", "10 mx.t.com.")],
        ("t.com", "CAA"): [rd("CAA", '0 issue "letsencrypt.org"')],
        ("t.com", "NS"): [rd("NS", "ns1.t.com.")],
        ("t.com", "AAAA"): [rd("AAAA", "2001:db8::1")],
    }

    class Answer:
        def __init__(self, rrset: Any, ad: bool) -> None:
            self.rrset = rrset
            self.response = type("R", (), {"flags": dns.flags.AD if ad else 0})()

    async def resolve(self: Any, name: str, rdtype: str, raise_on_no_answer: bool = True) -> Any:
        assert raise_on_no_answer is False
        if name == "nx.com":
            raise dns.resolver.NXDOMAIN
        if name == "slow.com":
            raise dns.exception.Timeout
        if name == "fail.com":
            raise dns.resolver.NoNameservers
        return Answer(data.get((name, rdtype)), ad=name == "t.com")

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)
    r = M.DnspythonResolver(2.0)
    assert r._resolver.lifetime == 2.0
    assert (await r.query("t.com", "TXT")).records == ["v=spf1 -all"]
    assert (await r.query("t.com", "MX")).records == ["10 mx.t.com."]
    assert (await r.query("t.com", "CAA")).records == ["0 issue letsencrypt.org"]
    assert (await r.query("t.com", "NS")).records == ["ns1.t.com."]
    aaaa = await r.query("t.com", "AAAA")
    assert aaaa.records == ["2001:db8::1"] and aaaa.authenticated and aaaa.status == "ok"
    assert (await r.query("other.com", "A")).status == "noanswer"
    assert (await r.query("nx.com", "A")).status == "nxdomain"
    assert (await r.query("slow.com", "A")).status == "timeout"
    assert (await r.query("fail.com", "A")).status == "error"


# ------------------------------------------------------------------ catalog


def test_new_rules_are_unscored_with_texts() -> None:
    msgs = FEATURE.catalog.messages
    for rid in NEW_IDS:
        d = catalog.rules()[rid]
        assert d.scored is False and d.category.value == "technical" and d.scope == "site" and d.since == "0.1.0"
        assert catalog.rule_feature(rid) == "site" and d.references
        for key in (d.message_key, d.recommendation_key):
            assert msgs["en"].get(key) and msgs["tr"].get(key), (rid, key)
    assert catalog.rules()["ns-not-diverse"].heuristic
    assert sum(1 for d in FEATURE.catalog.rules.values() if not d.scored) == len(NEW_IDS)
