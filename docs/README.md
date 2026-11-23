# Documentation

Start with the [project README](../README.md) for installation and a quick start. This folder
goes deeper. Pick the part that matches what you are doing:

| You want to... | Read |
|---|---|
| look up what an operation takes, returns and needs | [operations.md](operations.md) |
| know what an error code means and whether a retry helps | [errors.md](errors.md) |
| put seoprobe into a FastAPI / Celery / Temporal product | [guides/saas_integration.md](guides/saas_integration.md) |
| run it in Docker and on a server, safely | [guides/deployment.md](guides/deployment.md) |
| understand how the 0-100 score is calculated | [guides/scoring.md](guides/scoring.md) |
| understand how the library is built | [architecture/overview.md](architecture/overview.md) |
| see what the library protects against, and what you must do | [architecture/security.md](architecture/security.md) |
| know why something is the way it is | [architecture/adr/](architecture/adr/README.md) |
| look up a term (SSRF, coverage, time budget, ...) | [glossary.md](glossary.md) |

## Folders

| Folder | Contents | Kept up to date by |
|---|---|---|
| `.` (this folder) | the reference: every operation, every error code, and the glossary | operations and errors: tests (`tests/contract/test_docs.py` fails when they drift from the code); the glossary: maintainers, by hand |
| [guides/](guides/README.md) | how to use seoprobe in a real system | maintainers, by hand |
| [architecture/](architecture/README.md) | how seoprobe is built and why, including the decision records | maintainers, by hand; one new ADR per new decision |
| [archive/](archive/README.md) | the original design plan and review, kept as written (in Turkish) | nobody: historical, not updated |

## The two generated files

`operations.md` is generated from the code. After changing an operation, regenerate it with:

```bash
python -m tests.contract.test_docs
```

`errors.md` is written by hand, but a test checks that it lists exactly the codes in
`seoprobe.ErrorCode`, with the right retry flag.
