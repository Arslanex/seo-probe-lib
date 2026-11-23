# Security model

seoprobe fetches URLs that your users type in. That makes it a classic target for server-side
request forgery (SSRF): someone enters `http://169.254.169.254/` or `http://localhost:6379/` and
hopes your server fetches it for them. This page explains what the library does about that and
about the other risks of handling untrusted pages, and what your deployment has to add.

A term you do not know is probably in the [glossary](../glossary.md).

## What the HTTP client refuses

Every request the library makes goes through the same policy (`core/net/safety.py`). That covers
the page you asked for, every redirect hop, every link it checks, sitemaps, and calls to Google,
Bing and the public archives.

**Before connecting, the URL itself:**

- **Schemes:** only `http` and `https`.
- **Ambiguous authorities**, the parser tricks used to make one component see a public host while
  another connects to a private one: backslashes, percent-encoding in the host, user info
  (`user@host`), and addresses such as `http://public.example#@10.0.0.1/` that parsers split
  differently at `#` and `@`.
- **Obfuscated IPv4 spellings** (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`) are first
  normalised to the address they really mean, so they get the same verdict.
- **Known internal names:** `localhost`, `metadata.google.internal`, `metadata.azure.com`,
  `metadata.ec2.internal` and similar cloud metadata hosts.
- **Every IP address that is not public unicast:**

  | Range | Examples |
  |---|---|
  | private networks | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7` (AWS IMDS `fd00:ec2::254` included) |
  | carrier-grade NAT | `100.64.0.0/10` |
  | loopback | `127.0.0.0/8`, `::1` |
  | link-local | `169.254.0.0/16` (where cloud metadata lives), `fe80::/10` |
  | reserved, multicast, unspecified | `0.0.0.0`, `240.0.0.0/4`, `ff00::/8` |
  | IPv6 transition and special ranges | NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`), Teredo, 6to4, ORCHID, documentation, site-local |

  An IPv4-mapped IPv6 address is judged as the IPv4 address it carries, and an IPv6 address with
  a zone identifier is refused.

**At connect time, the resolved address.** Host names are resolved by `SafeResolver` when the
connection is made. If any address the name resolves to is not public, the connection is refused,
so an attacker cannot race the check. The address that was checked is the address that is
dialled. This closes DNS rebinding for the HTTP client: the trick in which a name resolves to a
public address for the check and to a private one for the connection.

**Redirects** are followed by hand, at most five, and each hop goes through the whole policy
again. A public page that redirects to `http://10.0.0.1/` gets `ssrf-blocked`.

A refused request never leaves the machine. The result is `ssrf-blocked` (or `invalid-url`,
`unsupported-scheme`), and its request log is empty.

## The browser

`render`, `screenshots`, `mobile`, `accessibility` and `lab_metrics` load pages in headless
Chromium:

- **Every request is checked.** Every request Chromium makes, including sub-resources and redirect
  hops, passes the same policy before it leaves the machine.
- **Every page is isolated.** It gets a fresh browser context.
- **Service workers are blocked and WebSockets are never connected**, because the request
  interception cannot see their traffic.

One gap remains: Chromium resolves host names itself, so in theory a hostile DNS server could
answer the check and the real connection differently. That is why the deployment must add a
network-level egress policy for any process that runs the browser. See
[deployment.md](../guides/deployment.md#restrict-outgoing-network-access).

## Limits against hostile pages

| Limit | Default | Setting |
|---|---|---|
| bytes read per response | 10 MB, then the page is truncated and flagged | `max_response_bytes` |
| bytes downloaded per operation | 256 MB | `max_total_bytes` |
| pages per site audit | 25 by default, at most 200 | `max_pages`, `max_pages_cap` |
| URLs per batch (`check_urls`, ...) | 1000 | `max_batch_urls` |

The parser also caps what it keeps from one page (20,000 links, 5,000 images, 2,000 headings, 50
JSON-LD blocks of 256 KB) and says so when a cap was hit.

**Sitemaps and other XML:**

- any `DOCTYPE` is refused;
- the XML parser runs without entity resolution, network access or DTD loading. That stops XML
  which tries to read local files (XXE) or to expand into gigabytes ("billion laughs");
- gzip-compressed files are decompressed in bounded chunks.

## Secrets

- **API keys travel in headers.** The Google key goes in `X-Goog-Api-Key` and the Open PageRank
  key in `API-OPR`. The Bing Webmaster API accepts its key only in the URL, as `apikey`.
- **Every request in `meta.requests` is logged with its secret query parameters masked:** `key`,
  `api_key`, `apikey`, `access_token`, `token`, `code` and `state` become `***`.
- **Error details carry only safe fields** (the URL, domain, status, service, the exception's
  type name, the missing requirement, a reason code). Exception text and resolved IP addresses
  can reveal your internal network, so they go to the DEBUG log only, never into a result.
- **Tests and live runs check this.** They search every result's JSON and the log output for the
  key values.

## Google sign-in

The OAuth flow for Search Console and GA4 uses PKCE (S256) and a single-use `state`. PKCE proves
that the code Google returns is redeemed by the same party that asked for it.

- **A request expires after ten minutes** and is marked consumed on first use.
- **The stored tokens remember the state that created them**, so a replayed callback is rejected.
- **seoprobe keeps no tokens.** They go to the token store you provide. Encrypt them at rest (the
  [SaaS example](../../examples/saas_fastapi_celery/README.md) uses Fernet).
- **Give your users a way to disconnect.** `gsc.disconnect(account=)` revokes the grant at
  Google and deletes the record from your token store. The revocation is best effort, so the
  record is deleted even when Google cannot be reached or rejects it. The warning that is logged
  then carries only Google's short error code, never the token. The revocation ends the user's
  whole grant to your app, including tokens kept under other records. The user can also revoke
  access in their Google account settings.

## Untrusted data in results

Titles, descriptions, evidence, link texts and anchors come from the pages that were checked, and
anyone can write anything there. Control and bidirectional-override characters are stripped from
report texts. **Still escape every page-derived string before you put it into HTML**, and do not
render it as Markdown or pass it to a template engine unescaped.

## Writing to third parties

The library sends data to a third party only when you call `indexnow.submit`, which notifies
search engines about URLs. Nothing calls it for you.

## What the deployment must add

| Requirement | Why |
|---|---|
| an egress firewall or network policy for processes that run the browser | closes the DNS-rebinding gap inside Chromium |
| secrets (API keys, OAuth client, token encryption key) from a secret manager, not from code | standard hygiene; the token key decrypts every customer's Google access |
| escaping of page-derived text in your UI | the library cannot know where its output ends up |
| authentication and rate limits on endpoints that start audits | every audit makes dozens of requests to the target site |
