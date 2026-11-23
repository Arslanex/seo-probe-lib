# 0003. One SSRF policy for every request, enforced at connect time

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/review.md §2, §6.8; archive/plan.md §11 #18

## Context

The URLs come from end users of a multi-customer service. Without a guard, anyone could make the
service fetch `http://169.254.169.254/` (cloud credentials) or internal admin pages. Checking the
URL before the request is not enough: DNS can answer differently when the connection is made
(DNS rebinding), and redirects can lead anywhere.

## Decision

- One policy (`core/net/safety.py`) judges every URL and address: http and https only, no
  ambiguous authorities, obfuscated IPv4 normalised, known metadata host names refused, and only
  public unicast addresses allowed. The full list of refused ranges is in
  [security.md](../security.md).
- `SafeResolver` resolves host names when the connection is made and refuses the connection if
  any address is not public, so the checked address is the dialled address.
- Redirects are followed by hand, at most five, each hop checked again. External APIs (Google,
  Bing, the archives) use the same checks.
- The browser routes every request through the same policy, blocks service workers and never
  connects WebSockets. Because Chromium resolves names itself, the deployment must add an egress
  network policy for processes that run it.
- A refusal says why: `URLSafetyError.code` is `invalid-url`, `unsupported-scheme` or
  `ssrf-blocked`, and nothing is sent.

## Consequences

- Integrators do not need their own URL filter for the HTTP features.
- Tests can serve local pages only through explicit test hooks (`url_check`, a test resolver).
- Browser features carry a documented deployment requirement
  ([deployment.md](../../guides/deployment.md#restrict-outgoing-network-access)).
