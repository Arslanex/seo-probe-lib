# SEOPROBE Changelog

All notable changes to seoprobe are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The two sections below are the rules every change
follows; the versions start after them.

## Commit messages

Every commit has a short summary and a long description, separated by a blank line:

```text
crawl: report redirect chains longer than three hops

Long chains waste crawl budget, and the source project flagged them, so
seoprobe now does too. The new rule is crawl-redirect-chain (High). It
is unscored, so existing scores do not move.

Not covered: chains through JavaScript redirects, which need the browser.

Verified: pytest (1712 passed); examples/04_crawl.py on python.org.
```

- **The summary line** is `<area>: <what changed>`. It is at most 72 characters, in the
  imperative ("add", "fix", not "added"), and has no full stop.
- **The area** is the feature package (`page`, `content`, `site`, `render`, `performance`,
  `search`, `crawl`, `monitoring`, `offsite`, `audit`, `report`) or one of `core`, `docs`,
  `examples`, `tests`, `build` and `release`. A change that spans several areas names the main
  one.
- **The long description** is wrapped at 72 characters. It says why the change was made, what it
  does, what it deliberately does not do, and how it was verified (tests, live sites, examples).
- **One logical change per commit.** Formatting, refactoring and behaviour changes go in separate
  commits.
- **A breaking change** ends the description with a `BREAKING CHANGE: <what breaks and what to do
  instead>` line, and is listed under "Removed" or "Changed" in this file.
- **A release** is a commit named `release: vX.Y.Z` that sets the version and dates its section
  here, with an annotated tag `vX.Y.Z` on it.

## Versions

Versions are `MAJOR.MINOR.PATCH`. The number lives only in `pyproject.toml`; `seoprobe.__version__`
reads it from the installed package.

| Change | Version part |
|---|---|
| a bug fix that changes no public name, error code or output field | PATCH |
| a new operation, a new optional parameter, a new output field, a new rule, a deprecation | MINOR |
| a public name, parameter, `ErrorCode` value or output field removed or given a new meaning | MAJOR |

- **The public contract** is what `tests/contract/` snapshots: `seoprobe.__all__` with every
  signature, the `ErrorCode` values, and every operation's output schema
  ([docs/architecture/overview.md](docs/architecture/overview.md#versioning)).
- **Before 1.0**, a MINOR version may still break the contract. When it does, its section says
  so under "Changed" or "Removed", with what to do instead.
- **Removals are announced first.** A public name keeps working with a `DeprecationWarning` for at
  least one MINOR version before it is removed.
- **Scores have versions of their own.** `score_version` changes when the scoring formula does,
  and `rule_version` when the rule set does. Each change is listed here, because scores from
  before and after are not directly comparable.
- **Unreleased work** collects under `## [Unreleased]` at the top of the versions until the next
  release commit.

## [0.1.0] - 2026-11-24

The first version. It is not published on PyPI; install it from source (see the README).

seoprobe brings the analyses of the `seo-analyzer-mvp` project into an async Python library, with
the same findings and scores, and adds operations that project did not have (monitoring, mobile,
accessibility, DNS, carbon, search opportunities, GA4, Bing, IndexNow, link suggestions). Those
new operations report their findings but are never part of the score.

### Added

- **core**
  - `Config`, `Session`, `Result`, `ErrorCode`, and the `@operation` decorator. The decorator
    gives every operation input validation, requirement checks, deadlines, error mapping, a
    request log with masked secrets, a byte budget and events.
  - `fetch` and `normalize`.
  - An SSRF-safe HTTP client: every redirect hop is checked, and private, loopback and cloud
    metadata addresses are refused at connect time.
  - A guarded headless browser.
  - Rule and text catalogues in English and Turkish, and deterministic scoring.
  - Stable error codes, including `invalid-url`, `unsupported-scheme`, `dns-failed`,
    `tls-error` and `fetch-timeout`. Outside-world failures are never raised as exceptions.
  - `preflight`, `check_access`, `operations()`, `rules()` and `requirements()`: what is
    installed, configured and available, checked without network access.
- **Embedding.** The library keeps no state between calls and works unchanged in an async app, a
  thread pool or a process pool:
  - HTML is parsed on a worker thread, so a large page never blocks the caller's event loop.
  - `Result`, `SeoprobeError` and every output model pickle and round-trip through JSON.
  - A `Session` used from another event loop, or opened twice, raises a clear `RuntimeError`.
  - `Session.listening(hook)` adds an event listener for a block without replacing
    `on_event`.
  - The library leaves the host's logging and warning settings alone. The one exception is the
    `trafilatura` logger, which is quietened only if the application has not set its level.
- **Time budgets.** The caller decides how long to wait for the slow, optional parts of an
  analysis:
  - `link_check_budget_s` for link-health probes and `visibility_budget_s` for the public archive
    lookups, set in `Config` or per call (`audit_page`, `audit_site`, `audit_stream`, `links`,
    `visibility`).
  - Both stop before the operation's own deadline.
  - What a budget cuts short is reported as `not_checked` (`time-budget`), never as a failure,
    and the score does not depend on the budget.
- **page:** `onpage` (47 checks) and `classify` (language and business type).
- **content:** `readability` (Flesch for English, Ateşman for Turkish, keyword audit),
  `previews` (Google and social cards), `schema`, `suggest_schema`, and `local_seo` (name,
  address and phone, from structured data, `tel:` links and text).
- **site:** `transport` (TLS, security headers, http→https, www), `site_setup` (redirect
  matrix, soft 404, well-known files), `robots`, `sitemap`, `generate_sitemap`, `ai_access`,
  `techstack` (110 technologies, MediaWiki included) and `dns_health` (SPF, DMARC, MX, CAA,
  DNSSEC, IPv6, name servers).
- **crawl:** `crawl` (a stratified, seeded sample), `links` (link graph, link health, click
  depth, orphan pages), `indexability`, `duplicates`, `hreflang` and `suggest_links`.
- **render:** `render` (JavaScript dependence), `screenshots`, `mobile` and `accessibility`
  (axe-core 4.10.2, WCAG 2.0 to 2.2).
- **performance:** `lab_metrics`, `pagespeed`, `field_data` (CrUX, falling back to the origin
  when a page has no record), `web_vitals` and `carbon`. Main text is extracted the same way with
  trafilatura 2.3 and 2.4.
- **offsite:** `authority` (Open PageRank), `visibility` (RDAP domain age, Wayback first and last
  capture, Common Crawl presence) and `competitors`.
- **monitoring:** `check_urls`, `check_redirects`, `snapshot`, `diff`, and `analyze_logs`, which
  verifies search engine bots by reverse and forward DNS, in constant memory.
- **search:**
  - `gsc.*` (properties, find_property, analytics, top_queries, inspect, sitemaps, rankings,
    opportunities, summary), with a stateless OAuth flow (`auth_url`, `exchange_code`,
    `connection`, and `disconnect`, which revokes the grant at Google and deletes the tokens).
  A 403 caused by an API that is not enabled carries `reason="api-not-enabled"`.
  - `ga4.*` (properties, landing_pages, summary).
  - `bing.*` (sites, query_stats, crawl_stats, crawl_issues, url_info).
  - `indexnow.*` (key_file, verify, submit). `submit` is the only operation that writes to a
    third party, and nothing calls it for you.
  - An async `TokenStore` (`MemoryTokenStore`, `FileTokenStore`, or your own database-backed
    store).
- **audit:** `audit_page`, `audit_site`, `audit_stream` (live progress events) and `rerun`.
  `account=` picks the customer whose Search Console data the audit reads; `rerun` keeps it.
- **report:** `report.build` (scores, findings, recommendations, comparison with the previous
  audit), `report.plan`, `report.findings` and `report.compare`, in English or Turkish.
- **Documentation:** a README, `docs/operations.md` (generated from the code),
  `docs/errors.md`, and `examples/01..11`, which together use all 71 operations.
- **SaaS recipe** (`examples/saas_fastapi_celery`): FastAPI with one `Session` in its lifespan, a
  Celery worker with a `Session` per task, Postgres for audits and Fernet-encrypted per-tenant
  OAuth tokens, a Redis token store as an alternative, pending OAuth requests in Redis with a TTL,
  error codes mapped to HTTP statuses, and a docker-compose file.

### Differences from seo-analyzer-mvp

The findings and scores match the source project on its reference pages (the parity tests). The
intended differences are recorded in the decision records (`docs/architecture/adr/`). In short:

- There is no LLM layer, HTTP API or user interface. API keys travel in headers. The default
  language is English.
- The error codes are stable.
  - A DNS failure is `dns-failed`, not `connect-failed`, and the audit does not retry over
    `http://` when the name does not resolve.
  - An unusable input URL is `invalid-url` or `unsupported-scheme`, not `ssrf-blocked`.
- Main text is extracted without trafilatura's fallback extractors. With trafilatura 2.4, the
  fallback put a site's shared menu into the main text of short pages, which made those pages
  look like duplicates of each other.
- Wayback dates come from fast exact-host queries. The source's domain-wide "last capture" query
  took 25 to 50 seconds on large sites and timed out.
- `local_seo` reads English street abbreviations ("425 Madison Ave", "Main St.") and ends an
  English address at its ZIP code.
- `hreflang-lang-attr-mismatch` fires only when none of the codes a page lists itself under
  matches its `<html lang>`.

### Verification

#### Automated tests

- 1698 tests, all offline: network access is blocked in the test suite.
- They cover:
  - the behaviour of each operation,
  - the SSRF policy,
  - the output JSON schemas and the public API,
  - error-code coverage, dependency rules, docs and catalogues,
  - parity with the source project.
- **Python 3.12 on macOS:** the full suite passes on this version.
- **Python 3.11:** the full suite passed on an earlier state of this version, with a fresh install
  that brought trafilatura 2.4.0. It has not been re-run after the final changes.
- **Python 3.13:** only the schema and public-API contract tests were run.
- **trafilatura 2.3.1 and 2.4.0:** both checked.
- **Linux and Windows:** not run.

#### Tested against live sites

Run on 23 November 2026 against public sites: example.com, python.org, docs.python.org, English
and Turkish Wikipedia, MDN, react.dev, apple.com and dentistnyc.com. Some results were also
checked against the raw HTTP and DNS responses: missing sitemap and canonical tag, H1 count,
redirect status, DMARC policy, missing x-default, address and phone, duplicate pair, real 404s.

| Area | Operations | What was confirmed |
|---|---|---|
| core | `fetch`, `normalize`, `preflight`, `check_access` | SSRF refusals send no request; DNS, TLS and scheme failures give the right codes |
| page, content | `onpage`, `classify`, `readability`, `previews`, `schema`, `suggest_schema`, `local_seo` | a Turkish page is detected as Turkish; a dental clinic's address and phone are found |
| site | `robots`, `sitemap`, `transport`, `site_setup`, `ai_access`, `techstack`, `dns_health`, `generate_sitemap` | MediaWiki and its version are recognised; DMARC `p=none` is confirmed by a DNS lookup |
| crawl | `crawl`, `links`, `indexability`, `duplicates`, `hreflang`, `suggest_links` | real 404s and an unknown host are reported as broken; two identical documentation pages are a 100 % duplicate; apple.com's 100 hreflang entries are read |
| render | `render`, `screenshots`, `mobile`, `accessibility` | the screenshots show the real page |
| performance | `lab_metrics`, `carbon`, `pagespeed`, `field_data`, `web_vitals` | with a Google API key: PageSpeed field and lab values, CrUX per URL and the origin fallback |
| offsite | `visibility`, `competitors` | RDAP domain age, Wayback first and last capture |
| monitoring | `check_urls`, `check_redirects`, `snapshot`, `diff`, `analyze_logs` | a real Googlebot IP is verified by reverse DNS, a fake one is caught |
| audit, report | `audit_page`, `audit_site`, `audit_stream`, `rerun`, `report.*` | the score is the same with and without time budgets; Turkish report texts |
| embedding | threads, a spawned process pool, pickling, JSON | results and errors cross process boundaries; no API key appears in any result or log |
| search | `gsc.*` (all 13), `ga4.summary`, the OAuth flow | on 24 November 2026, with a real Google account and two Search Console properties: properties, analytics, top queries, URL inspection, sitemaps, rankings, opportunities and summary return real data; an audit includes it; an expired access token is refreshed; a new sign-in with GSC and GA4 scopes completes with PKCE and a single-use state, and a callback with the wrong state is refused; `gsc.disconnect` revokes the grant at Google, and the revoked refresh token is then refused |
| SaaS recipe | FastAPI + Celery + Postgres 16 + Redis 7 (Docker) | every endpoint over HTTP; page audits in about 20 s and a site audit in about 35 s on the worker; the report compares with the previous audit; both token stores round-trip, and nothing is stored in plain text; OAuth state is single-use; a redelivered task for a finished audit is skipped |

#### Partly tested

- **`authority`**: the request reaches Open PageRank, but the available key is an old DomCop key,
  which the service now rejects. Only the "no key" and "unauthorized" answers have been seen.
- **`visibility`**:
  - Common Crawl was unreachable from the test machine, so its answers have not been seen live.
  - On large sites the Wayback URL sample usually takes longer than its 12 s limit. The capture
    dates are not affected.
- **`indexnow`**: `key_file` and `verify` were run. `submit` was not run on purpose, because it
  notifies search engines.
- **`ga4.*`**: the sign-in granted the Analytics scope, but the Analytics APIs were not enabled
  in the Google Cloud project, so every call answered 403 (now reported as
  `reason="api-not-enabled"`). GA4 data has not been seen live.
- **`bing.*`**: without a Bing key, only the `missing-credential` answer was checked live.

#### Not tested yet

- Real Google Analytics 4 data and a real Bing Webmaster account.
- A current Open PageRank key, and IndexNow submission for a site you control.
- Linux and Windows.
- Small business sites beyond a single one, and Turkish business sites.
- Every individual finding: only the spot checks above were compared with the raw responses.

### Known limitations

- `gsc.disconnect` ends the Google user's whole grant to the app, not one token: other records
  connected with the same Google user must sign in again. This is how Google's revocation works.
- Phone numbers are read from page text only in Turkish or `+international` form. A number such
  as "(212) 581-5360" is found only through a `tel:` link.
- Politeness limits (concurrency per host, spacing) are kept per process. N worker processes
  auditing one site may send N times as many requests at once.
- `FileTokenStore` is safe across tasks and threads of one process, not across processes. Use a
  database-backed store in production.
- Browser speed metrics vary from run to run with network timing, so the performance category
  can move by a few points.
- The Wayback Machine and Common Crawl are free public services. They can be slow or down; the
  rest of an audit is not affected.

[0.1.0]: https://github.com/Arslanex/seo-probe-lib/releases/tag/v0.1.0
