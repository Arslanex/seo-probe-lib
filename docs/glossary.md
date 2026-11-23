# Glossary

The terms these documents use, in plain words. Each entry links to where the term is explained in
full. The first part covers the library, the second the SEO terms in findings and reports.

## Library terms

**ADR (architecture decision record).** A short page that records one decision about how seoprobe
is built: why it was needed, what was decided and what follows from it.
[architecture/adr](architecture/adr/README.md)

**Audit.** The full analysis of a page (`audit_page`) or of a sample of a site's pages
(`audit_site`). It runs many operations and ends with a score.
[scoring.md](guides/scoring.md)

**Cap (gate).** A ceiling on the overall score when a show-stopper is found, for example 40 when
the page is set to `noindex`. [scoring.md](guides/scoring.md#caps-for-show-stoppers)

**Carrier-grade NAT (CGNAT).** The address range `100.64.0.0/10` that internet providers use
inside their own networks. Like private networks, seoprobe never contacts it.

**Category.** One of the eight areas a score is split into: technical, content, onpage, links,
performance, schema, ai_readiness and images. [scoring.md](guides/scoring.md#categories-and-their-weights)

**Cloud metadata service.** An address inside a cloud server (usually `169.254.169.254`) that
hands out the server's credentials. It is the classic target of SSRF, and seoprobe refuses it.
IMDSv2 is AWS's token-protected version of it. [security.md](architecture/security.md)

**Core sample.** In a site audit, the pages chosen to represent each section (template) of the
site. Extra pages picked at random are called exploration pages. `core_overall` is the score of
the core pages only, the number to follow over time.

**Coverage.** The share of the score's weight that could actually be measured. Below 60 %, no
overall score is given. [scoring.md](guides/scoring.md#coverage-confidence-and-insufficient-data)

**Customer.** One of the businesses a product built on seoprobe serves. The SaaS example calls
it a `tenant` in its code and URLs, and seoprobe's Google functions take it as `account=`.

**DNS rebinding.** A trick in which a host name first resolves to a public address, to pass a
check, and then to a private one, for the real connection. seoprobe's HTTP client checks the
address at the moment it connects, which defeats it. [security.md](architecture/security.md)

**Egress policy.** A firewall rule that limits where a server may connect to. Processes that run
the browser need one that blocks private and metadata addresses.
[deployment.md](guides/deployment.md#restrict-outgoing-network-access)

**Error code.** The stable, short name of what went wrong (`dns-failed`, `missing-credential`, ...)
in `Result.error.code`. seoprobe returns these instead of raising exceptions.
[errors.md](errors.md)

**Extra.** An optional part of the installation: `browser` (Chromium features) and `dns` (DNS
checks). `all` installs both. Without an extra, its features answer `missing-extra`.

**Finding.** A problem found on a page or site: a rule id, a severity, a category, a message,
the evidence and how to fix it.

**Layer (in import rules).** A level in the package structure. A higher layer may use a lower
one, never the reverse. [overview.md](architecture/overview.md#packages-and-the-rules-between-them)

**Link checks.** Requests that check whether the links on the audited pages still work (status,
redirects, broken). Limited in number per audit, and in time by `link_check_budget_s`.

**`not_checked`.** The list, in every result, of what could not be checked and why (no API key, a
time budget, not applicable). Nothing in it lowers the score.

**OAuth.** The standard way a customer lets your product read their Google data without giving
you their password. seoprobe handles the flow; you store the tokens.
[saas_integration.md](guides/saas_integration.md#per-customer-google-connections)

**Operation.** One of seoprobe's 71 public functions, such as `seoprobe.onpage` or
`seoprobe.audit_site`. Each is `async` and returns a `Result`. [operations.md](operations.md)

**p25.** In a site audit, the page score that a quarter of the pages fall below: a quick measure
of how bad the weakest pages are.

**Parity.** seoprobe gives the same findings and scores as the project it was ported from
(`seo-analyzer-mvp`); tests check this.
[ADR 0005](architecture/adr/0005-parity-and-unscored-rules.md)

**PKCE.** An addition to OAuth that proves the code returned by Google is redeemed by the same
party that asked for it. seoprobe always uses it.

**Politeness.** The limits that keep seoprobe gentle with the sites it checks: at most two
requests at once per site (`per_host_concurrency`) and a pause between them (`request_delay_s`).

**Result (the envelope).** What every operation returns: `status`, `data`, `findings`,
`not_checked`, `error` and `meta`. The same shape for all 71 operations.
[README](../README.md#every-function-works-the-same-way)

**Rule.** One check with a fixed id, category, severity and texts, defined in a `catalog.json`.
`seoprobe.rules()` lists them all. Unscored rules are reported but do not change the score.

**Session.** The object that keeps connections, politeness limits and the browser open between
calls. Use one per process or event loop.
[saas_integration.md](guides/saas_integration.md#the-one-rule-behind-everything)

**Severity.** How serious a finding is: Critical, High, Medium, Low or Info. It sets the
finding's weight in the score.

**SSRF (server-side request forgery).** Tricking a server into fetching an internal address for
an attacker, for example its cloud metadata service. seoprobe refuses such addresses.
[security.md](architecture/security.md)

**Time budget.** How long an audit may spend on its slow, optional parts (link checks, archive
lookups). Different from the time limit. [ADR 0008](architecture/adr/0008-dynamic-time-budgets.md)

**Time limit (`timeout_s`).** The longest an operation may run as a whole. When it is reached,
the result is `timeout`.

**Token store.** The object you give the session to keep customers' Google tokens: anything with
async `get`, `set` and `delete` methods, usually backed by your database.

**XXE.** XML that tries to read local files or blow up into gigabytes when parsed. seoprobe's XML
parsing refuses it.

## SEO terms

**Canonical.** The `<link rel="canonical">` tag that tells search engines which address is the
main copy of a page, when the same content is reachable at several addresses.

**Common Crawl.** A free, public archive of the web, crawled every few months. seoprobe asks it how
many of a site's pages it holds.

**Core Web Vitals.** Google's three speed and stability measures: LCP (how fast the main content
appears), INP (how fast the page reacts) and CLS (how much the layout jumps).

**CrUX (Chrome UX Report).** Speed measured on real visitors' Chrome browsers, published by
Google. This is **field data**. **Lab data** is measured by loading the page once in a test
browser, as seoprobe's `lab_metrics` and PageSpeed do.

**hreflang.** Tags that link the language versions of a page to each other, so searchers get the
version in their own language.

**IndexNow.** A protocol that tells Bing and other search engines at once that a page has
changed. `indexnow.submit` sends to it.

**JSON-LD (structured data).** Machine-readable facts about a page (a product, an article, a
business address) in a `<script type="application/ld+json">` block. Search engines use it for
rich results.

**llms.txt.** A proposed file at a site's root that points AI assistants to its most useful
content, much as `robots.txt` speaks to crawlers.

**Open PageRank.** A free score from 0 to 10 for how well a domain is linked from other sites,
used by `authority`.

**Orphan page.** A page that no other page on the site links to. Visitors and crawlers find it
only through the sitemap or by chance.

**RDAP.** The modern replacement for WHOIS: a public service that says when a domain was
registered and by which registrar.

**SERP.** The search engine results page. `previews` shows how a page will look on it.

**Soft 404.** A missing page that answers with status 200 ("OK") instead of 404, so search
engines may keep it in their index.

**Wayback Machine.** The Internet Archive's history of web pages. seoprobe uses it for the date
a site was first and last archived.
