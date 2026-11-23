# Architecture

How seoprobe is built, and why. Read this before changing the library itself. Integrators will
find the practical side in [guides/](../guides/README.md).

| Document | What it covers |
|---|---|
| [overview.md](overview.md) | the concepts (`Session`, `Context`, `Result`, `Page`), what every operation goes through, the package layers and the rules that keep them apart, the event loop, time budgets, versioning |
| [security.md](security.md) | the SSRF model, the guarded browser, how secrets are kept out of results and logs, untrusted page data, and what the deployment must add |
| [adr/](adr/README.md) | the architecture decision records: one short page per decision, with its context and consequences |

The historical plan and review these were distilled from are in [archive/](../archive/README.md).
