# 0002. A shared core and isolated, layered features

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §4, §11 #2, #3, #9, #17

## Context

Seventy operations need a structure in which one feature can be changed, added or removed
without touching the others, and in which the shared foundation is easy to find.

## Decision

- `core/` holds what every feature needs, and `features/<name>/` holds one group of related
  operations. `core` never imports a feature.
- Features are layered: `report > audit > monitoring | offsite > crawl > content | site | render |
  performance | search > page`. A feature imports another only through its package root and its
  `__all__` (operations, output models, and deliberately exported pure helpers), never an
  internal module.
- Every feature has the same layout: `__init__.py` (exports and the `FEATURE` declaration),
  `models.py` (every output model), operation files, `catalog.json` (rules and texts), data files.
  A file that grows past about 400 lines becomes a subpackage of the same name (for example
  `crawl/links/`).
- `Page`, `fetch` and `normalize` live in `core/page.py`, because `core` itself fetches pages.
  Scoring lives in `core/rules/scoring.py`, because both `audit` and `report` need it.
- `core` is split by purpose: `parse/` (pure parsing), `net/` (network and browser), `rules/`
  (catalogues and scoring), with the contract types at the bottom and the runtime (`feature`,
  `session`, `page`) on top. There is no `utils` module.
- import-linter contracts (`lint-imports`) and `tests/contract/test_dependencies.py` enforce all
  of this.

## Consequences

- A feature can be read, tested (`pytest tests/features/<name>`) and removed on its own.
- A violation fails the checks instead of turning into a hidden dependency.
- Shared code needs a deliberate home. Helpers cannot pile up in a catch-all module.
