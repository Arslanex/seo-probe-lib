"""Output models of the site feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# --- from site_setup.py


class RedirectVariant(BaseModel):
    name: str  # http | www-counterpart | trailing-slash | uppercase | index-html | index-php
    variant_url: str
    status: int | None = None  # status of the FIRST response
    final_url: str | None = None
    final_status: int | None = None
    hops: int = 0
    permanent: bool | None = None  # None when there was no redirect
    error: str | None = None


class Probe(BaseModel):
    url: str
    status: int | None = None
    present: bool = False
    detail: str | None = None
    error: str | None = None


class SiteSetup(BaseModel):
    redirects: list[RedirectVariant] = Field(default_factory=list)
    soft_404: dict[str, Any] = Field(default_factory=dict)
    custom_404: bool | None = None
    headers: dict[str, Any] = Field(default_factory=dict)
    hsts: dict[str, Any] = Field(default_factory=dict)
    http_version: str = "unknown"
    files: dict[str, Probe] = Field(default_factory=dict)
    requests_made: int = 0
    skipped: dict[str, str] = Field(default_factory=dict)


# --- from techstack.py


class TechItem(BaseModel):
    name: str
    category: str
    version: str | None = None
    evidence: str
    confidence: Literal["high", "medium", "low"]
    website: str | None = None


class TechStack(BaseModel):
    items: list[TechItem] = Field(default_factory=list)
    by_category: dict[str, list[str]] = Field(default_factory=dict)


# --- from agentic.py

Status = Literal["pass", "fail", "unknown", "na"]


class AgenticCheck(BaseModel):
    id: str
    status: Status
    detail: str = ""  # stable code(s), the report translates


class AgenticReadiness(BaseModel):
    checks: list[AgenticCheck] = Field(default_factory=list)
    fraction: str = "0/0"  # our own checklist: pass / (pass + fail); unknown and na are excluded
    lighthouse_fraction: str | None = None  # only ever filled from PSI/Lighthouse, never by us
    llms_txt_notes: list[str] = Field(default_factory=list)
    unnamed: dict[str, int] = Field(default_factory=dict)
    requests: int = 0
    notes: list[str] = Field(default_factory=list)


# --- from sitemap_gen.py


class ExcludedPage(BaseModel):
    url: str
    reason: str  # stable code: status-4xx, status-5xx, redirected, noindex, noindex-header, canonical-elsewhere, fetch-error


class SitemapGenerated(BaseModel):
    xml: str = ""
    urls_total: int = 0
    urls: list[str] = Field(default_factory=list)  # first LIST_CAP, in file order
    with_lastmod: int = 0
    existing_sitemap: bool = False
    missing_from_sitemap: list[str] = Field(default_factory=list)  # fit for a sitemap, not in yours
    missing_total: int = 0
    in_sitemap_not_indexable: list[ExcludedPage] = Field(default_factory=list)
    in_sitemap_not_indexable_total: int = 0
    excluded: list[ExcludedPage] = Field(default_factory=list)
    excluded_total: int = 0
    pages_in_sample: int = 0
    sample_limited: bool = True
    truncated: bool = False


# --- from robots.py


class Group(BaseModel):
    agents: list[str] = Field(default_factory=list)
    rules: list[tuple[str, str]] = Field(default_factory=list)  # ("allow"|"disallow", pattern)
    content_signal: list[str] = Field(default_factory=list)


class RobotsRules(BaseModel):
    groups: list[Group] = Field(default_factory=list)
    sitemaps: list[str] = Field(default_factory=list)
    truncated: bool = False  # byte limit or rule limit hit

    @property
    def has_any_group(self) -> bool:
        return bool(self.groups)


class AICrawlerStatus(BaseModel):
    token: str
    vendor: str
    capability: str
    governs: str
    not_evidence_for: str
    group: str  # named | star | none
    root_allowed: bool


class RobotsReport(BaseModel):
    status: str  # ok | missing | unreachable
    http_status: int | None = None
    rules: RobotsRules | None = None
    sitemaps: list[str] = Field(default_factory=list)
    ai_crawlers: list[AICrawlerStatus] = Field(default_factory=list)
    entry_allowed_for_bot: bool = True
    entry_allowed_for_googlebot: bool = True
    llms_txt_present: bool | None = None
    facts: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        return self.status != "unreachable"

    def allows(self, url: str, user_agent: str) -> bool:
        """
        Crawl permission for discovery/sampling. An unreachable robots.txt allows (the entry URL
        itself was already fetched); RFC 9309 disallow-all is only applied by ``can_fetch``.
        """
        from .robots import can_fetch

        if not self.available or self.rules is None:
            return True
        return can_fetch(self.rules, user_agent, url)


# --- from sitemap.py


class SitemapUrl(BaseModel):
    model_config = ConfigDict(frozen=True)

    loc: str
    lastmod: str | None = None


class SitemapFile(BaseModel):
    url: str
    final_url: str | None = None
    kind: str | None = None  # urlset | sitemapindex | rss | feed | text
    gzip: bool = False
    declared: bool = False  # listed in robots.txt (vs. probed at a common path)
    url_count: int = 0
    child_count: int = 0
    bytes: int | None = None
    error: str | None = None
    status: int | None = None


class SitemapReport(BaseModel):
    urls: list[SitemapUrl] = Field(default_factory=list)  # audited-site URLs, deduplicated
    files: list[SitemapFile] = Field(default_factory=list)
    found: bool = False  # at least one valid sitemap was read
    truncated: bool = False  # max_urls reached
    facts: dict[str, Any] = Field(default_factory=dict)


# --- from transport.py


class TransportReport(BaseModel):
    tls: dict[str, Any] = Field(default_factory=dict)
    redirects: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    set_cookie_limitation: str | None = None


# --- from dns_health.py


class SpfInfo(BaseModel):
    records: list[str] = Field(default_factory=list)  # every v=spf1 TXT record at the domain
    valid: bool | None = None  # None when there is no single record to judge
    errors: list[str] = Field(default_factory=list)
    lookups: int = 0  # DNS-querying terms (include, a, mx, ptr, exists, redirect), counted recursively
    lookups_complete: bool = True  # False when a target could not be read or a cap stopped the walk
    lookup_limit: int = 10
    all_qualifier: str | None = None  # + - ~ ? of the top-level "all" mechanism


class DmarcInfo(BaseModel):
    record: str | None = None
    records: int = 0
    policy: str | None = None  # p=
    subdomain_policy: str | None = None  # sp=
    pct: int | None = None
    rua: list[str] = Field(default_factory=list)
    ruf_present: bool = False


class MxInfo(BaseModel):
    hosts: list[str] = Field(default_factory=list)  # "preference exchange"
    count: int = 0
    null_mx: bool = False  # RFC 7505 "0 .": the domain accepts no mail


class CaaInfo(BaseModel):
    found_at: str | None = None  # the name the CAA set was found at (CAA climbs to parents)
    records: list[str] = Field(default_factory=list)
    issuers: list[str] = Field(default_factory=list)  # issue / issuewild values


class DnssecInfo(BaseModel):
    method: str = "ds-at-parent"
    ds_records: int = 0
    enabled: bool | None = None  # a DS record exists in the parent zone
    validated: bool | None = None  # AD flag of the DS answer (only meaningful with a validating resolver)


class HostAddresses(BaseModel):
    host: str
    a: list[str] = Field(default_factory=list)
    aaaa: list[str] = Field(default_factory=list)


class NsInfo(BaseModel):
    zone: str | None = None  # the name the NS set was found at
    hosts: list[HostAddresses] = Field(default_factory=list)
    count: int = 0
    distinct_subnets_24: int = 0  # distinct IPv4 /24 networks of the name servers
    providers: list[str] = Field(default_factory=list)  # last two labels of each name server
    min_nameservers: int = 2
    min_subnets: int = 2


class DnsHealth(BaseModel):
    domain: str
    spf: SpfInfo = Field(default_factory=SpfInfo)
    dmarc: DmarcInfo = Field(default_factory=DmarcInfo)
    mx: MxInfo = Field(default_factory=MxInfo)
    caa: CaaInfo = Field(default_factory=CaaInfo)
    dnssec: DnssecInfo = Field(default_factory=DnssecInfo)
    ipv6: list[HostAddresses] = Field(default_factory=list)  # the domain and www.<domain>
    ns: NsInfo = Field(default_factory=NsInfo)
    queries: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
