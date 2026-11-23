# 0013. Audits name their Google account, and disconnecting is an operation

**Status:** Accepted · **Date:** 2026-11-24 · **Amends:** [0009](0009-stateless-oauth-and-async-token-store.md)

## Context

[ADR 0009](0009-stateless-oauth-and-async-token-store.md) left two gaps for a product with many
customers:

- **Audits always read Search Console through the `"default"` account.** Every `gsc.*` and
  `ga4.*` call took `account=`, but `audit_page` and `audit_site` did not. A worker had to wrap
  its token store so that `"default"` meant the current customer (`TenantTokenStore` in the SaaS
  example).
- **There was no public way to disconnect.** The OAuth class could already revoke a grant at
  Google, but only internally. Integrators could only delete the record from their own store,
  which leaves the grant alive at Google until the customer revokes it.

## Decision

- **`account=` on the audits.** `audit_page`, `audit_site` and `audit_stream` take
  `account="default"`. It is stored in `AuditOptions.account`, so `rerun` reads the same
  account. The audit passes it to `gsc.summary`.
- **`gsc.disconnect(account=)`.** It revokes the refresh token at Google, then deletes the
  record from the token store, and returns the new `GscConnection`. The revocation is best
  effort. When Google cannot be reached, or rejects the revocation (for example because the
  grant was already revoked), a warning with Google's short error code is logged and the record
  is deleted anyway, because the local deletion is what the customer asked for.
- **`disconnect` works without a configured Google app**, like `connection`, so tokens can be
  removed after the app has gone. Without a token store it answers `not-connected`, as the other
  calls do.
- The audits' `schema_version` went from 1 to 2, because `AuditOptions` gained a field.

## Consequences

- A multi-customer worker passes `account=customer_id` and needs no wrapper around its store.
  The SaaS example dropped `TenantTokenStore` and gained `DELETE /tenants/{tenant}/google`.
- Audits stored before this change still load: the new field has a default.
- There are 71 operations instead of 70.
- **Disconnecting ends the whole grant.** Found live on 24 November 2026: Google keeps one grant
  per Google user and OAuth client. Revoking a fresh token also invalidated an older refresh
  token of the same user, held by another application on the same client. seoprobe cannot
  revoke one token alone, so the guides say this to integrators instead.
- **A disabled API is reported as such.** The same live run showed every GA4 call failing with
  403 because the Analytics APIs were not enabled in the Cloud project, which looked exactly like
  a missing permission. Google's recognised causes are now kept: `no-permission` carries
  `reason="api-not-enabled"` or `"scope-not-granted"`, and summaries put the reason in `error`.
  Nothing else from Google's error body is kept.
