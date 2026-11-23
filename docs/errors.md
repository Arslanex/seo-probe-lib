# Error codes

Every failure that comes from the outside world is returned in `Result.error` with one of these
codes (`seoprobe.ErrorCode`). Values are stable: they change only in a major release. External-service
codes are not service-specific; `error.details.service` names the service (`gsc`, `ga4`, `bing`,
`indexnow`, `pagespeed`, `crux`, `openpagerank`). `details` never carries exception text, resolved
addresses or secrets.

Exceptions are raised only for programming errors (wrong argument types: `TypeError` /
`pydantic.ValidationError`) and by `Result.unwrap()` (`SeoprobeError`, with the original exception
as `__cause__` for `internal-error`). `asyncio.CancelledError` is never swallowed.

Some outcomes are data, not errors: a site answering 404 or 500 (`Page.status`), a response that is
not HTML (`Page.content_type`; `onpage` answers `checked=false`) and a response larger than
`max_response_bytes` (kept up to the limit, `Page.truncated`).

## Input

| Code | Retryable | Meaning |
|---|---|---|
| `invalid-url` | no | The URL is not valid. |
| `unsupported-scheme` | no | Only http and https URLs are supported. |
| `ssrf-blocked` | no | The address points to a private or reserved network and was not contacted. |
| `invalid-input` | no | The input is not valid. |

## Network

| Code | Retryable | Meaning |
|---|---|---|
| `dns-failed` | yes | The host name could not be resolved. |
| `connect-failed` | yes | The server could not be reached. |
| `tls-error` | no | The TLS handshake failed. |
| `fetch-timeout` | yes | The server did not answer in time. |
| `too-many-redirects` | no | The URL redirects too many times. |

## Policy

| Code | Retryable | Meaning |
|---|---|---|
| `rate-limited` | yes | Too many requests or failures for this host; try again later. |
| `request-budget-exceeded` | no | The request or download budget of the operation was used up. |

## Requirements

| Code | Retryable | Meaning |
|---|---|---|
| `missing-extra` | no | An optional dependency is not installed. |
| `missing-credential` | no | A required key or credential is not configured. |
| `browser-unavailable` | yes | The headless browser could not be started. |

## External services

| Code | Retryable | Meaning |
|---|---|---|
| `not-connected` | no | The account is not connected. |
| `no-permission` | no | The account has no permission for this resource. `details.reason` says why when Google tells: `scope-not-granted` (sign in again with the right `services`) or `api-not-enabled` (enable the API in the Google Cloud project). |
| `property-not-found` | no | No matching property was found for this site. |
| `api-quota-exceeded` | yes | The quota of the external service is exhausted. |
| `api-error` | yes | The external service failed. |

## General

| Code | Retryable | Meaning |
|---|---|---|
| `timeout` | yes | The operation took longer than allowed. |
| `internal-error` | no | An unexpected error occurred. |
