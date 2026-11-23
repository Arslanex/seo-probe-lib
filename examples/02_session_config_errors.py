"""
02 - Sessions, configuration, errors and requirements.

    python examples/02_session_config_errors.py [url]

* ``Config`` holds every setting (language, limits, keys). Nothing reads the environment unless
  you call ``Config.from_env()`` (variables ``SEOPROBE_<FIELD>``).
* ``Session`` is the one thing you manage: open it once per process / event loop and share it
  between tasks. Fetch a page once and hand the ``Page`` to several operations.
* Errors from the outside world come back as ``ErrorCode``; ``unwrap()`` turns them into
  ``SeoprobeError`` if you prefer exceptions.
* ``preflight`` says what an operation still needs (an extra, a key, OAuth), without network.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"


async def main() -> None:
    config = seoprobe.Config(lang="tr", max_pages=10)  # findings and report texts in Turkish
    print("from the environment:", seoprobe.Config.from_env().lang, "(SEOPROBE_LANG, default en)")

    events: list[str] = []

    async def on_event(ev: seoprobe.Event) -> None:  # started / progress / finished of every operation
        events.append(f"{ev.kind}:{ev.operation}")

    async with seoprobe.Session(config, on_event=on_event) as s:
        # Fetch once, analyse many times: no request is repeated.
        page = (await seoprobe.fetch(URL, session=s)).unwrap()
        print(f"\nfetched {page.final_url}: HTTP {page.status}, {page.html_bytes} bytes, ttfb {page.ttfb_ms} ms")
        tech, prev, onp = await asyncio.gather(
            seoprobe.techstack(page, session=s),
            seoprobe.previews(page, session=s),
            seoprobe.onpage(page, session=s),
        )
        print("ran side by side:", tech.status.value, prev.status.value, onp.status.value)
        for f in onp.findings[:3]:
            print(f"  [{f.severity.value}] {f.id}: {f.message}")  # localised (lang="tr")
        print("events seen:", events[:6], "...")

        # normalize: the canonical form of a URL and whether it may be fetched, without any request.
        for raw in ("Example.COM/a//b/?utm_source=x&id=1", "http://10.0.0.1/admin", "ftp://example.com/"):
            n = (await seoprobe.normalize(raw, session=s)).unwrap()
            print(f"normalize {raw!r} -> {n.url} allowed={n.allowed} reason={n.reason}")

        # Safety and errors: no request leaves the machine for these.
        for target in ("http://169.254.169.254/latest/meta-data/", "http://localhost:8080/", "ftp://example.com/"):
            r = await seoprobe.fetch(target, session=s)
            print(f"\n{target} -> {r.error.code.value if r.error else 'ok'} (requests made: {len(r.meta.requests)})")
        r = await seoprobe.fetch("https://does-not-exist-seoprobe-example.invalid/", session=s)
        print("unknown host ->", r.error.code.value if r.error else "ok")

        # Exceptions, if you want them: unwrap() raises SeoprobeError with the code and the Result.
        try:
            r.unwrap()
        except seoprobe.SeoprobeError as exc:
            print("unwrap() raised SeoprobeError:", exc.code.value)

        # A deadline per call: timeout_s caps any operation (nested ones included).
        quick = await seoprobe.robots(URL, timeout_s=30, session=s)
        print("\nrobots with a 30 s deadline:", quick.status.value)

        # Requirements, checked without network.
        for op in ("pagespeed", "render", "dns_health", "gsc.summary"):
            pf = await seoprobe.preflight(op, session=s)
            print(f"preflight {op:12} ready={pf.ready} missing={pf.missing}")
        print("check_access google (PageSpeed, CrUX key):", (await seoprobe.check_access("google", session=s)).code)

    # The catalogue: every operation and every rule, as data.
    ops = seoprobe.operations()
    print(f"\n{len(ops)} operations, e.g. {ops[0].name}: {ops[0].description}")
    rules = seoprobe.rules()
    by_category = Counter(r.category for r in rules)
    print(
        f"{len(rules)} rules, e.g. {rules[0].id} [{rules[0].severity}]; by category: {dict(by_category.most_common(5))}"
    )
    print("requirements of render:", seoprobe.requirements("render"))


if __name__ == "__main__":
    asyncio.run(main())
