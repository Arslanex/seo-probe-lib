"""
08 - Monitoring: URL lists, redirect maps, page snapshots and diffs, server log analysis.

    python examples/08_monitoring.py [url]

The jobs a SaaS runs on a schedule. Nothing is stored by the library: keep the ``Snapshot`` you
took yesterday (it is JSON) and hand it to ``diff`` today.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urljoin

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(
        f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | not checked {len(res.not_checked)}"
    )
    for f in res.findings[:4]:
        print(f"   [{f.severity.value:8}] {f.id} {f.url or ''}")


def sample_log() -> list[str]:
    """A few combined-format lines: a real Googlebot IP, a fake 'Googlebot', a browser, a 404."""
    ua_google = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ua_browser = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    rows = [
        ("66.249.66.1", "/", 200, ua_google),  # Google's crawler range: verifiable by reverse DNS
        ("66.249.66.1", "/about/", 200, ua_google),
        ("203.0.113.9", "/", 200, ua_google),  # says Googlebot, comes from elsewhere: spoofed
        ("198.51.100.7", "/downloads/", 200, ua_browser),
        ("66.249.66.1", "/old-page", 404, ua_google),
    ]
    return [
        f'{ip} - - [23/Nov/2026:10:00:00 +0000] "GET {path} HTTP/1.1" {st} 512 "-" "{ua}"' for ip, path, st, ua in rows
    ]


async def main() -> None:
    async with seoprobe.Session() as s:
        # check_urls: status, redirects, canonical, noindex and robots.txt for a list of URLs.
        urls = [URL, urljoin(URL, "/about/"), urljoin(URL, "/this-page-does-not-exist-seoprobe")]
        res = await seoprobe.check_urls(urls, session=s)
        head("check_urls", res)
        for row in res.unwrap().items:
            print(f"   {row.status} indexable={row.indexable!s:5} hops={row.hops} {row.url} {row.reasons}")

        # check_redirects: does every old URL of a migration end where it should, permanently?
        mapping = {urljoin(URL, "/about"): urljoin(URL, "/about/"), "http://python.org/": "https://www.python.org/"}
        res = await seoprobe.check_redirects(mapping, session=s)
        head("check_redirects", res)
        for row in res.unwrap().items:
            print(
                f"   {row.old} -> {row.final_url} ({row.statuses}) matches={row.matches_expected} issues={row.issues}"
            )

        # snapshot + diff: what changed on a page since the last run (title, canonical, noindex, ...).
        old = (await seoprobe.snapshot(URL, session=s)).unwrap()
        print(f"\n== snapshot: title={old.title!r} h1={old.h1[:2]} words={old.word_count} hash={old.content_hash[:12]}")
        changed = old.model_copy(
            update={"title": "A different title", "noindex": True, "word_count": old.word_count // 3}
        )
        res = await seoprobe.diff(old, changed, session=s)  # pretend this is tomorrow's snapshot
        head("diff", res)
        d = res.unwrap()
        print(f"   changed={d.changed} worst severity={d.max_severity}")
        for c in d.changes:
            print(f"   {c.severity:8} {c.field}: {str(c.old)[:30]!r} -> {str(c.new)[:30]!r}")

        # analyze_logs: which bots crawl what; real search bots are verified by reverse + forward DNS.
        res = await seoprobe.analyze_logs(sample_log(), urls=[URL, urljoin(URL, "/never-crawled/")], session=s)
        head("analyze_logs", res)
        logs = res.unwrap()
        print(
            f"   {logs.lines_parsed}/{logs.lines_total} lines, bot requests {logs.bot_requests}, other {logs.other_requests}"
        )
        for b in logs.bots:
            print(
                f"   {b.name:12} requests={b.requests} verified={b.verified_requests} spoofed={b.spoofed_requests} "
                f"unverified={b.unverified_requests} top paths={b.top_paths[:3]}"
            )
        print(f"   watched URLs no bot fetched: {logs.uncrawled_urls}")


if __name__ == "__main__":
    asyncio.run(main())
