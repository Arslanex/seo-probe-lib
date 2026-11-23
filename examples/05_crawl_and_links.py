"""
05 - Several pages: crawl a sample, then links, indexability, duplicates, hreflang, link ideas.

    python examples/05_crawl_and_links.py [url] [max_pages]

``crawl`` discovers the site's pages (sitemaps and the entry page's links) and fetches a
stratified, seeded sample: the same seed gives the same sample. The other operations work on the
fetched ``Page`` objects; only ``links`` makes requests of its own (link-health probes), and
``link_check_budget_s`` decides how long you are willing to wait for them.
"""

from __future__ import annotations

import asyncio
import sys
import time

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"
MAX_PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 6


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(
        f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | not checked {len(res.not_checked)}"
    )
    for f in res.findings[:3]:
        print(f"   [{f.severity.value:8}] {f.id} {f.url or ''}")


async def main() -> None:
    async with seoprobe.Session() as s:
        t0 = time.monotonic()
        res = await seoprobe.crawl(URL, max_pages=MAX_PAGES, seed=42, session=s)
        head(f"crawl ({time.monotonic() - t0:.0f} s, {len(res.meta.requests)} requests)", res)
        cr = res.unwrap()
        pages = cr.pages
        print(f"   sampled {len(pages)} pages, coverage={cr.coverage}")
        for p in pages:
            print(f"   {p.status} {p.sample_kind:11} {p.url}")

        # links: the link graph of the sample plus link-health probes, here at most 8 seconds.
        t0 = time.monotonic()
        res = await seoprobe.links(
            pages, entry_url=cr.entry_url, sitemap_urls=cr.sitemap_urls, link_check_budget_s=8, session=s
        )
        head(f"links ({time.monotonic() - t0:.0f} s)", res)
        lk = res.unwrap()
        print(f"   internal={lk.internal_total} external={lk.external_total} checked={lk.checked} broken={lk.broken}")
        print(f"   not probed in time: {lk.skipped_time_budget} | max depth={lk.max_depth} | orphans={lk.orphans[:3]}")

        res = await seoprobe.indexability(pages, links=lk, sitemap_urls=cr.sitemap_urls, session=s)
        head("indexability", res)
        for row in res.unwrap().items:
            print(f"   indexable={row.indexable!s:5} {row.url} {row.reasons}")

        res = await seoprobe.duplicates(pages, session=s)
        head("duplicates (near-duplicate main text)", res)
        print(
            "   near-duplicate pairs:",
            res.unwrap().duplicate_pairs,
            "| each pair is a finding:",
            [f.url for f in res.findings][:4],
        )

        res = await seoprobe.hreflang(pages[0], pages=pages, session=s)
        head("hreflang", res)
        hl = res.unwrap()
        print(f"   applicable={hl.applicable} languages={hl.languages} x-default={hl.has_x_default}")

        res = await seoprobe.suggest_links(pages, links=lk, session=s)
        head("suggest_links (pages with few inlinks: where to link from)", res)
        for t in res.unwrap().targets[:3]:
            print(f"   {t.url} inlinks={t.inlinks} suggestions={len(t.suggestions)}")

        res = await seoprobe.generate_sitemap(pages, sitemap_urls=cr.sitemap_urls, session=s)
        head("generate_sitemap", res)
        gen = res.unwrap()
        print(f"   {gen.urls_total} URLs, missing from the current sitemap: {gen.missing_total}")
        print("   " + gen.xml[:160].replace("\n", " ") + " ...")


if __name__ == "__main__":
    asyncio.run(main())
