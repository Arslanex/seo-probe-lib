# 0001. A library of async functions that all return one envelope

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §1-§3, §6.1

## Context

seoprobe brings the analyses of `seo-analyzer-mvp` (an application with an HTTP API, a database,
an LLM layer and a UI) to developers who want them inside their own products. Those products
already have a web framework, a queue and a database. A second copy of any of them would be in the
way.

## Decision

- seoprobe is a library only: no web server, command line tool, database, background worker or
  LLM.
- Every capability is one `async` function, defined once: `await seoprobe.techstack(url)`. There
  is no client class that repeats the operations as methods. A shared `Session` is passed with
  `session=`. Without it, a temporary one is opened and closed for the call.
- There is no synchronous API.
- Every operation returns the same `Result` envelope: `status` (`ok`, `partial`, `error`),
  typed `data`, `findings`, `not_checked`, `error`, `meta`. It always serialises to JSON.
- Inputs are typed parameters, validated before anything runs. There are no free-form option
  dictionaries.

## Consequences

- Integrators learn one shape and handle every operation the same way: store it, check `ok`, map
  `error.code`.
- The common behaviour (validation, requirements, deadlines, error mapping, request log, events)
  lives once, in the `@operation` decorator (see [overview](../overview.md#what-every-operation-goes-through)).
- Callers without an event loop use `asyncio.run(...)`. There is no sync wrapper to maintain.
