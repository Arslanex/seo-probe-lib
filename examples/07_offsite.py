"""
07 - The site from the outside: public visibility, authority, competitors.

    python examples/07_offsite.py [url] [competitor_url ...]

* ``visibility``: domain age (RDAP), Wayback captures and Common Crawl presence. Keyless and
  informational, but these public services can be slow: ``budget_s`` caps the wait, and a lookup
  that runs out of time keeps ``error="time-budget"`` while the others keep their answers.
* ``authority``: Open PageRank (``SEOPROBE_OPENPAGERANK_KEY``); without a key it says so.
* ``competitors``: the same light on-page analysis for your page and up to five others.
"""

from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlsplit

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"
COMPETITORS = sys.argv[2:] or ["https://www.ruby-lang.org/en/", "https://www.php.net/"]


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(
        f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | not checked {len(res.not_checked)}"
    )


async def main() -> None:
    domain = urlsplit(URL).hostname or URL
    async with seoprobe.Session(seoprobe.Config.from_env()) as s:
        for budget in (3, 30):  # a hurried caller and a patient one
            t0 = time.monotonic()
            res = await seoprobe.visibility(domain, budget_s=budget, session=s)
            head(f"visibility, budget {budget} s ({time.monotonic() - t0:.0f} s)", res)
            v = res.unwrap()
            print(f"   RDAP: {v.rdap}")
            print(f"   Wayback: {v.wayback}")
            print(f"   Common Crawl: {v.common_crawl}")
            print(f"   notes: {v.notes}")

        res = await seoprobe.authority(URL, session=s)
        head("authority", res)
        print(f"   {res.unwrap().model_dump(exclude_none=True)}")
        for nc in res.not_checked:
            print(f"   not checked: {nc.check_id} ({nc.reason_code})")

        res = await seoprobe.competitors(URL, competitors=COMPETITORS, session=s)
        head("competitors", res)
        cmp = res.unwrap()
        rows = ([cmp.you] if cmp.you else []) + cmp.rows
        print(f"   {'host':22} {'score':>5} {'title':>5} {'meta':>4} {'h1':>2} {'words':>6}  schema")
        for row in rows:
            mark = " (you)" if row.is_you else ""
            print(
                f"   {row.host[:22]:22} {row.score if row.score is not None else '-':>5} {row.title_len or 0:>5} "
                f"{row.meta_len or 0:>4} {row.h1_count or 0:>2} {row.words or 0:>6}  {row.schema_types}{mark} {row.error or ''}"
            )


if __name__ == "__main__":
    asyncio.run(main())
