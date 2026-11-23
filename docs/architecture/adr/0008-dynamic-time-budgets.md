# 0008. Callers decide how long to wait for the slow, optional parts

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #28, #29

## Context

On real sites, a single-page audit took 35 to 52 seconds. Two parts took most of that time:

- **The link checks.** Up to 60 links on the same site are checked one after another, 0.4 seconds
  apart, to stay polite.
- **The public archive lookups** behind `visibility`. They take 17 to 35 seconds, as fast as those
  free services answer.

A "check now" button cannot wait that long, while a nightly report can. Neither part could be
configured. Worse, if the link checks ran past the operation's time limit, the whole audit was
lost.

## Decision

- **Two time budgets.** `link_check_budget_s` and `visibility_budget_s` say how long those parts
  may take. They can be set in `Config` (by default there is no budget) or per call: `audit_page`,
  `audit_site`, `audit_stream`, `links` and `visibility(..., budget_s=)`. `math.inf` means "as
  long as the operation's time limit allows". Audits store the budgets in `AuditOptions`, so
  `rerun` repeats them.
- **One rule for every budget.** `Context.budget_deadline` turns a budget into a stopping time.
  That time is never later than the operation's own time limit, and it leaves the stages that
  follow a reserve of 20 % of the remaining time, at least 5 seconds.
- **Cut short, not failed.** Links that were not checked in time are marked `unchecked` with
  `error="time-budget"`. Archive lookups keep the answers that did arrive.
- **The score does not depend on the budget.** The existing count limits already stop the link
  checks early, and the rules are then judged over the links that were checked. The time budget
  works the same way. The result says that the check was partial with a `not_checked` entry
  (`link-health`, `time-budget`). That entry is not a rule, so it does not affect the score.
  Only when not a single link of a kind could be checked is the rule itself `not_checked`.
- **A first version got this wrong.** It excluded the whole rule from the score, which lowered a
  real site's score from 84.8 to 81.6. The examples, run against real sites, caught it.
- `max_internal_link_checks` and `link_check_concurrency` moved into `Config`.

## Consequences

- Measured on react.dev, both budgets at 5 s took a page audit from 52 s to 18 s with the same
  score.
- The same audit can be cheap or thorough, decided per call. The README and
  [deployment.md](../../guides/deployment.md#what-an-audit-costs) show the costs.
