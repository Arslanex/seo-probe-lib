# 0005. Same findings and scores as the source project; new rules are not scored

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §1 (relation to the source), §11 #1, #4, #10, #14, #16, #26

## Context

seoprobe's rules, thresholds and scoring come from `seo-analyzer-mvp`. Customers comparing the two
should get the same answer, and seoprobe also adds checks the source never had (monitoring,
mobile, accessibility, DNS, carbon, search opportunities, link suggestions).

## Decision

- The source project is the reference for behaviour, not for architecture. Parity tests run the
  same scenarios as the source on a demo site and require the same findings, not-checked entries,
  scores and report data. The snapshots are produced offline, without keys
  (`tests/fixtures/parity/README.md`).
- Rules the source did not have carry `"scored": false` in their catalogue: they are reported but
  never part of the score, so the score stays the source's.
- Text-only differences are allowed and listed in the parity test (for example, a rule the source
  registered without a message now has one).
- The default language is English. Turkish is `Config(lang="tr")`.
- The first released version is 0.1.0, and that is every rule's `since` and the `rule_version`.
  Rules are told apart by origin (ported or new), not by version numbers.

## Consequences

- Scoring changes have to break the parity tests on purpose, which makes them visible.
- New checks can ship without moving existing customers' scores. Adding them to the score later is
  a separate, deliberate decision.
