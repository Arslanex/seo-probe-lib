# Guides

Practical guides for developers who use seoprobe in their own system.

| Guide | Read it when you... |
|---|---|
| [saas_integration.md](saas_integration.md) | build a product on seoprobe: where the `Session` lives in FastAPI and in a worker, how to store results, how to write a token store for Google, how to turn error codes into HTTP responses |
| [deployment.md](deployment.md) | ship it: a Docker image with Chromium, the network policy the browser features need, sizing and configuration |
| [scoring.md](scoring.md) | need to explain a score to a customer: categories and weights, how a check passes or fails, caps, coverage and confidence |

A runnable version of the SaaS guide is in
[examples/saas_fastapi_celery](../../examples/saas_fastapi_celery/README.md).
