# Architecture decision records

One short page per decision that shapes seoprobe: the context that forced it, what was decided,
and what follows from it. They replace the decision table of the original plan (§11 in
[archive/plan.md](../../archive/plan.md)), which is kept for history.

| # | Decision | In one line |
|---|---|---|
| [0001](0001-library-only-async-api.md) | A library of async functions that all return one envelope | no server, no client class; every operation returns `Result` |
| [0002](0002-core-and-feature-layers.md) | A shared core and isolated, layered features | import rules enforced by import-linter and tests |
| [0003](0003-ssrf-safe-networking.md) | One SSRF policy for every request, enforced at connect time | private and metadata addresses are never contacted |
| [0004](0004-stable-error-codes.md) | Outside-world failures are stable codes, never exceptions | a short list of codes to map once |
| [0005](0005-parity-and-unscored-rules.md) | Same findings and scores as the source project; new rules are not scored | parity tests guard the score |
| [0006](0006-parse-html-off-the-event-loop.md) | Parse HTML on a worker thread | the host's event loop is never blocked |
| [0007](0007-picklable-results-and-loop-bound-sessions.md) | Results cross process boundaries; sessions stay on their event loop | process pools and task queues work |
| [0008](0008-dynamic-time-budgets.md) | Callers decide how long to wait for the slow, optional parts | budgets per call, score unaffected |
| [0009](0009-stateless-oauth-and-async-token-store.md) | Stateless OAuth, and tokens in a store the integrator provides | multi-instance sign-in, tokens in your database |
| [0010](0010-main-text-without-fallback-extractors.md) | Extract the main text without trafilatura's fallback extractors | the same text with trafilatura 2.3 and 2.4 |
| [0011](0011-fast-wayback-host-queries.md) | Wayback dates from fast exact-host queries | dates in about a second instead of a timeout |
| [0012](0012-history-comes-from-the-caller.md) | The library keeps no history; the caller passes earlier results back | reports and comparisons from stored results |
| [0013](0013-audits-name-their-google-account.md) | Audits take `account=`; `gsc.disconnect` revokes and deletes tokens | per-customer Google data in audits, disconnecting |

## Adding a decision

When a change alters how seoprobe behaves for its users or how it is built (a new contract, a
dependency, a security rule, a scoring change), add a record:

1. Copy the shape of an existing one: a title, then status and date, then **Context**,
   **Decision** and **Consequences**.
2. Number it with the next free number (`0013-short-title.md`) and add it to the table above.
3. A decision is not rewritten later. If it changes, write a new record and set the old one's
   status to "Superseded by 00NN".
4. Mention the record in the changelog entry of the change.
