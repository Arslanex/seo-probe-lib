# Archive

Documents that shaped seoprobe while it was being built. They are kept exactly as they were
written, in Turkish, and are **not updated**. Where they disagree with the code or with the
current documentation, the current documentation is right.

| File | What it was | What replaced it |
|---|---|---|
| [plan.md](plan.md) | the implementation plan: goals, the layout of `core` and the features, contracts, the operation catalogue, the migration map from `seo-analyzer-mvp`, and in §11 the decisions taken while building | [architecture/overview.md](../architecture/overview.md) and the [decision records](../architecture/adr/README.md) |
| [review.md](review.md) | an architecture review of the plan, written before any code existed, with the problems it found and how they were to be fixed | [architecture/security.md](../architecture/security.md) and the decision records |

Read them when you want the history behind a decision, for example why a rule behaves exactly
like the source project's. The section numbers that older code comments mention (for example
"§11 #4") refer to `plan.md`.
