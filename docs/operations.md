# Operations

Generated from `seoprobe.operations()`; `tests/contract/test_docs.py` fails when it is out of date
(regenerate with `python -m tests.contract.test_docs`). Every operation is called as
`await seoprobe.<name>(...)` (dotted names are namespaces: `seoprobe.gsc.summary`) and returns
`Result[<output>]`; `session=` and `timeout_s=` are accepted by all of them.

## core

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `fetch` | `Page` | network | Fetch and parse one URL (SSRF-safe, redirects validated hop by hop). |
| `normalize` | `NormalizedUrl` | — | Normalise a URL and say whether it may be fetched (no network access). |

## page

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `classify` | `Classification` | network | Detect the page's primary language and business type. |
| `onpage` | `OnPageReport` | network | Run the 47 deterministic single-page checks. |

## content

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `local_seo` | `LocalSeo` | network | NAP (name, address, phone) extraction, inconsistencies across pages and ``tel:`` links. |
| `previews` | `Previews` | network | Google SERP preview (desktop and mobile), Open Graph / Twitter card and og:image check. |
| `readability` | `ReadabilityData` | network | Flesch / Atesman readability, frequent terms and the target keyword audit. |
| `schema` | `SchemaReport` | network | JSON-LD detection and validation: deprecated types, placeholders, missing key fields. |
| `suggest_schema` | `SchemaSuggestions` | network | Copy-paste JSON-LD for the types a page is missing, with rich result notes. |

## site

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `ai_access` | `AgenticReadiness` | network | What an AI agent that browses or reads the site meets: llms.txt, Markdown, Content-Signal, |
| `dns_health` | `DnsHealth` | network, `[dns]` | SPF, DMARC, MX, CAA, DNSSEC, IPv6 and name-server checks of ``domain`` (DNS queries only). |
| `generate_sitemap` | `SitemapGenerated` | — | A suggested sitemap.xml from the sampled pages fit for one (site audits only). |
| `robots` | `RobotsReport` | network | robots.txt rules, AI crawler access and llms.txt presence for the site of ``url``. |
| `site_setup` | `SiteSetup` | network | Redirect matrix, soft-404, custom 404, response headers and well-known files of the site. |
| `sitemap` | `SitemapReport` | network | Find, read and judge the sitemaps of the site of ``url`` (robots.txt declarations first). |
| `techstack` | `TechStack` | network | Technologies (CMS, framework, CDN, analytics, ...) seen in the page's response. |
| `transport` | `TransportReport` | network | TLS certificate, security headers, http->https and www consolidation of the site of ``url``. |

## crawl

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `crawl` | `CrawlReport` | network | Discover the site's pages (sitemaps and the entry page's links), pick a stratified seeded |
| `duplicates` | `DuplicateReport` | — | Near-duplicate main text among the sampled pages (no network). |
| `hreflang` | `HreflangReport` | network | Hreflang codes, return links and targets of the sampled pages (targets outside the sample |
| `indexability` | `IndexabilityTable` | — | Can each sampled page be indexed, and if not, why (no network). |
| `links` | `LinkSummary` | network | Link graph, link health (bounded probes), click depth and orphan pages of the sampled pages. |
| `suggest_links` | `LinkSuggestions` | — | Topic-close source pages and anchor candidates for pages with few internal links (no network). |

## render

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `accessibility` | `AccessibilityReport` | network, browser, `[browser]` | WCAG checks with axe-core for the chosen conformance level. |
| `mobile` | `MobileReport` | network, browser, `[browser]` | Mobile usability checks in a phone-sized browser and content parity with desktop. |
| `render` | `RenderReport` | network, browser, `[browser]` | Render the page in the browser when it looks like a SPA shell (or ``force``) and compare |
| `screenshots` | `Screenshots` | network, browser, `[browser]` | Desktop and mobile first-screen pictures and above-the-fold checks. |

## performance

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `carbon` | `CarbonEstimate` | network | CO2e per page view of ``target``. |
| `field_data` | `FieldData` | network, `google_api_key` | CrUX real-user data of ``url`` (the origin's record when the page has none). |
| `lab_metrics` | `LabMetrics` | network, browser, `[browser]` | Lab-measure (url, device) pairs; a plain URL or Page is measured on desktop. |
| `pagespeed` | `PageSpeedReport` | network, `google_api_key` | PageSpeed Insights (mobile) of ``url``; ``measured=False`` when PageSpeed could not be used. |
| `web_vitals` | `WebVitals` | network | Lab (desktop and mobile), PageSpeed and CrUX data of ``url`` with their findings merged. |

## search

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `bing.crawl_issues` | `BingCrawlIssues` | network, `bing_api_key` | URLs with crawl issues (redirects, 4xx/5xx, robots.txt blocks, malware, DNS, timeouts). |
| `bing.crawl_stats` | `BingCrawlStats` | network, `bing_api_key` | Daily crawl statistics of the last six months. |
| `bing.query_stats` | `BingQueryStats` | network, `bing_api_key` | Top queries of the site: impressions, clicks, average positions (weekly data). |
| `bing.sites` | `BingSites` | network, `bing_api_key` | Sites of the Bing Webmaster account the key belongs to. |
| `bing.url_info` | `BingUrlInfo` | network, `bing_api_key` | Bing's index details of one URL (status, crawl and discovery dates, size, links). |
| `ga4.landing_pages` | `Ga4LandingPages` | network, oauth | Sessions, engagement, key events and revenue per landing page (``channel=None``: all). |
| `ga4.properties` | `Ga4Properties` | network, oauth | GA4 properties the connected account can read. |
| `ga4.summary` | `Ga4Summary` | network, oauth | Organic landing pages of the GA4 property serving ``url``. Analytics states (not connected, |
| `gsc.analytics` | `GscAnalytics` | network, oauth | Clicks, impressions, CTR and position (Search Analytics). |
| `gsc.auth_url` | `AuthRequest` | network, oauth | Google authorisation URL plus the pending request the caller stores until the callback. |
| `gsc.connection` | `GscConnection` | network, oauth | Whether the Google app is configured and the account is connected (no network). |
| `gsc.disconnect` | `GscConnection` | network, oauth | Forget the account's Google connection: revoke the grant at Google, then delete its tokens. |
| `gsc.exchange_code` | `GscConnection` | network, oauth | Trade the authorisation code of a stored ``AuthRequest`` for tokens (kept in the token store). |
| `gsc.find_property` | `GscProperty` | network, oauth | The property that covers ``url`` (fail-closed: never another site's property). |
| `gsc.inspect` | `GscInspection` | network, oauth | URL Inspection: Google's own view of one page. |
| `gsc.opportunities` | `Opportunities` | network, oauth | Striking-distance queries, low CTR for the position, cannibalization and declining pages. |
| `gsc.properties` | `GscProperties` | network, oauth | Search Console properties the connected account can access. |
| `gsc.rankings` | `KeywordRankings` | network, oauth | Keyword -> position and page, plus opportunities from the property's own queries. |
| `gsc.sitemaps` | `GscSitemaps` | network, oauth | Sitemaps submitted to the property. |
| `gsc.summary` | `GscData` | network, oauth | Everything above for one site plus the Search Console rules. Search Console states |
| `gsc.top_queries` | `TopQueries` | network, oauth | The site's most shown real Search Console queries of the last 28 days. |
| `indexnow.key_file` | `IndexNowKeyFile` | — | The key file to publish at the site root (validates the key format). |
| `indexnow.submit` | `IndexNowSubmission` | network | Submit changed URLs of ``host`` to IndexNow. **The only write operation of the package**: |
| `indexnow.verify` | `IndexNowVerification` | network | Whether ``https://<host>/<key>.txt`` is served with the key as its content. |

## offsite

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `authority` | `AuthoritySignal` | network | OpenPageRank estimate of the site's registrable domain (third-party, never scored). |
| `competitors` | `CompetitorComparison` | network | Entry page of up to three competitors next to the user's own page. |
| `visibility` | `VisibilitySignals` | network | RDAP registration age, Wayback captures and Common Crawl presence of ``domain`` (a host or |

## monitoring

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `analyze_logs` | `LogAnalysis` | network | Per bot: requests, verified vs spoofed, status distribution, top paths and wasted crawl; |
| `check_redirects` | `RedirectChecks` | network | Per old -> expected row: the status chain, final URL, hop count, loop, whether it lands on |
| `check_urls` | `UrlChecks` | network | Per URL: status, redirects, canonical, noindex (meta and header), robots.txt permission, |
| `diff` | `SnapshotDiff` | — | The changes between two snapshots of a page, each with a severity (no network access). |
| `snapshot` | `Snapshot` | network | Status, final URL, title, description, canonical, robots directives, H1s, hreflang, schema |

## audit

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `audit_page` | `Audit` | network | Audit one page with every applicable analysis, then score it. |
| `audit_site` | `Audit` | network | Audit a site: discover its pages, audit a stratified seeded sample, then score it. |
| `rerun` | `Audit` | network | A fresh audit with the same URL, mode and options as ``audit``. |

## report

| Operation | Output | Requires | What it does |
|---|---|---|---|
| `report.build` | `AuditReport` | — | The full JSON report of an audit, with its quality-gate result in ``quality``. |
| `report.compare` | `Comparison` | — | Score, finding and metric changes between two audits of the same site. |
| `report.findings` | `FindingList` | — | The report's findings, filtered; ``sort="priority"`` orders them by priority score. |
| `report.plan` | `ActionPlan` | — | The 4-phase action plan of a report's recommendations. |
