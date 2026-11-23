"""
03 - One page: on-page checks, language, readability, previews, structured data, local SEO.

    python examples/03_page_and_content.py [url] [keyword]

The page is fetched once and every analysis below reuses it (no extra request for the HTML).
Findings carry an id, a severity, a category, evidence and a fix, in ``Config.lang``.
"""

from __future__ import annotations

import asyncio
import sys

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"
KEYWORD = sys.argv[2] if len(sys.argv) > 2 else "python"


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(
        f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | not checked {len(res.not_checked)}"
    )


async def main() -> None:
    async with seoprobe.Session(seoprobe.Config(lang="en")) as s:
        page = (await seoprobe.fetch(URL, session=s)).unwrap()

        # onpage: the deterministic single-page checks (title, description, headings, images, links ...).
        res = await seoprobe.onpage(page, session=s)
        head("onpage", res)
        report = res.unwrap()
        print(f"   title={report.title!r} words={report.word_count}")
        for f in res.findings[:5]:
            print(f"   [{f.severity.value:8}] {f.id}: {f.message}")
            if f.evidence:
                print(f"              evidence: {f.evidence[:90]}")

        # classify: primary language and a coarse business type.
        res = await seoprobe.classify(page, session=s)
        head("classify", res)
        print("  ", res.unwrap().model_dump(exclude_none=True))

        # readability: Flesch (en) / Atesman (tr) scores, frequent terms, a keyword audit.
        res = await seoprobe.readability(page, keywords=[KEYWORD], session=s)
        head("readability", res)
        r = res.unwrap()
        print(f"   language={r.language} scores={r.readability}")
        print(f"   top terms: {[t.get('term') if isinstance(t, dict) else t for t in r.terms[:6]]}")
        for kw in r.keywords:
            print(f"   keyword {KEYWORD!r}: {kw}")

        # previews: how the page looks in Google (desktop, mobile) and when shared (Open Graph).
        res = await seoprobe.previews(page, session=s)
        head("previews", res)
        p = res.unwrap()
        for serp in p.serp:  # desktop and mobile
            print(f"   {serp.device:7} {serp.display_url} | {serp.title_shown!r} truncated={serp.title_truncated}")
            print(f"           {serp.description_shown[:90]!r} missing={serp.description_missing}")
        image = p.social.image
        print(f"   og:title={p.social.og.get('og:title')!r} missing og tags={p.social.missing_og}")
        if image is not None:
            print(f"   og:image {image.width}x{image.height} {image.format} reachable={image.reachable}")

        # schema: JSON-LD found on the page and its problems; suggest_schema: what is missing.
        res = await seoprobe.schema(page, session=s)
        head("schema", res)
        print("   types:", res.unwrap().types)
        res = await seoprobe.suggest_schema(page, session=s)
        head("suggest_schema", res)
        for item in res.unwrap().items[:3]:
            print(
                "   suggestion:", item.model_dump(include={"type", "reason"}) if hasattr(item, "model_dump") else item
            )

        # local_seo: name, address, phone (NAP) and their consistency; 'applicable' says if it matters.
        res = await seoprobe.local_seo(page, session=s)
        head("local_seo", res)
        loc = res.unwrap()
        print(f"   applicable={loc.applicable} phones={loc.phones[:2]} tel link={loc.has_tel_link}")
        for nc in res.not_checked:
            print(f"   not checked: {nc.check_id} ({nc.reason_code})")


if __name__ == "__main__":
    asyncio.run(main())
