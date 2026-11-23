"""
01 - Quick start: one call, and the Result envelope every operation returns.

    python examples/01_quickstart.py [url]

Every capability of seoprobe is one ``async`` function. Called without ``session=``, it opens a
temporary session (connection pool, politeness limits) and closes it again: the simplest way to
use the library. Whatever happens, you get a ``Result`` back; outside-world failures (DNS, TLS, a
blocked address) are never raised as exceptions.
"""

from __future__ import annotations

import asyncio
import sys

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"


async def main() -> None:
    res = await seoprobe.techstack(URL)

    # The envelope: the same fields for every operation.
    print("operation  :", res.operation, "| feature:", res.feature)
    print("status     :", res.status.value)  # ok | partial | error
    print("ok         :", res.ok)  # True unless status == error
    print("trace id   :", res.meta.trace_id)  # one id per call, for your logs
    print("duration   :", res.meta.duration_ms, "ms")
    print("requests   :", [(r.method, r.url, r.status) for r in res.meta.requests])  # every request, secrets masked

    # data: the typed output model of this operation (here TechStack).
    if res.data is not None:
        for item in res.data.items:
            print(f"  found {item.name} ({item.category}) version={item.version} confidence={item.confidence}")

    # findings: rule violations (techstack is informational, so none); not_checked: what could not run.
    print("findings   :", len(res.findings), "| not checked:", len(res.not_checked))

    # Everything serialises to JSON: store it, put it on a queue, return it from your API.
    print("JSON size  :", len(res.to_json()), "characters")

    # unwrap() returns data or raises SeoprobeError: handy when you want exceptions after all.
    stack = res.unwrap()
    print("by category:", stack.by_category)

    # A failure is a Result too, with a stable error code (docs/errors.md).
    bad = await seoprobe.techstack("https://does-not-exist-seoprobe-example.invalid/")
    print("\nan unreachable host ->", bad.status.value, bad.error.code.value if bad.error else None)
    print(
        "   message:",
        bad.error.message if bad.error else None,
        "| retryable:",
        bad.error.retryable if bad.error else None,
    )


if __name__ == "__main__":
    asyncio.run(main())
