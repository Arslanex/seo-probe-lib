# 0009. Stateless OAuth, and tokens in a store the integrator provides

**Status:** Accepted, amended by [0013](0013-audits-name-their-google-account.md) · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #12, #24; archive/review.md (OAuth)

## Context

Search Console and GA4 need each customer's own Google consent. A multi-instance service cannot
keep half-finished sign-ins in one process's memory, and refresh tokens are secrets that belong in
the integrator's own, encrypted storage.

## Decision

- `gsc.auth_url` returns an `AuthRequest` (state, PKCE S256 verifier, redirect URI, creation time,
  scopes) that the caller keeps until the callback. `gsc.exchange_code` takes it back, checks the
  state, the redirect URI and the PKCE pair, and marks the request as used. A request expires
  after ten minutes, and the stored record remembers its state, so a replay is rejected.
- Tokens go to a `TokenStore`: any object with `async get`, `set` and `delete`, keyed by an
  account name (the customer id). The interface is async so that a database or secret manager
  can back it. `MemoryTokenStore` and `FileTokenStore` are provided. The file store does its I/O on
  a worker thread, under a lock, because a measured 39 of 40 concurrent writes were lost without
  it.
- API keys travel in headers. The Bing Webmaster API accepts its key only in the URL, so it is
  masked in the request log.
- Without a configured Google app, every `gsc.*` call answers `missing-credential`. With the app
  but no connected account, `not-connected`.

## Consequences

- The callback may land on any instance, and an abandoned sign-in costs nothing.
- Integrators own encryption and deletion of tokens. 0.1.0 has no public operation that revokes a
  token at Google.
- Audits read Search Console through the `"default"` account. A multi-customer worker maps it to
  the current customer (`TenantTokenStore` in the SaaS example), because the audits do not take an
  `account` argument yet.
