"""
04 - Site level: robots.txt, sitemaps, TLS and security headers, redirects, AI access, DNS.

    python examples/04_site_setup.py [url]

These operations look at the whole site behind ``url``. Each one makes a few bounded requests
(robots.txt, sitemap files, http/https and www variants, well-known files); ``dns_health`` only
queries DNS (needs the ``[dns]`` extra).
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | requests {len(res.meta.requests)}")
    for f in res.findings[:4]:
        print(f"   [{f.severity.value:8}] {f.id}: {f.message[:110]}")


async def main() -> None:
    async with seoprobe.Session() as s:
        page = (await seoprobe.fetch(URL, session=s)).unwrap()

        res = await seoprobe.robots(URL, session=s)
        head("robots", res)
        rb = res.unwrap()
        print(f"   status={rb.status} sitemaps={rb.sitemaps[:2]} llms.txt={rb.llms_txt_present}")
        print(f"   entry allowed for us={rb.entry_allowed_for_bot}, for Googlebot={rb.entry_allowed_for_googlebot}")
        blocked = [
            c.model_dump(include={"token", "allowed"}) for c in rb.ai_crawlers if not getattr(c, "allowed", True)
        ]
        print(f"   AI crawlers blocked: {blocked[:4]}")

        res = await seoprobe.sitemap(URL, robots_sitemaps=rb.sitemaps, session=s)
        head("sitemap", res)
        sm = res.unwrap()
        print(f"   found={sm.found} files={len(sm.files)} urls={len(sm.urls)} truncated={sm.truncated}")

        res = await seoprobe.transport(URL, page=page, session=s)
        head("transport (TLS, security headers, http->https, www)", res)
        tr = res.unwrap()
        print(f"   tls: {dict(list(tr.tls.items())[:5]) if isinstance(tr.tls, dict) else tr.tls}")

        res = await seoprobe.site_setup(page, session=s)
        head("site_setup (redirect matrix, soft 404, headers, files)", res)
        st = res.unwrap()
        print(f"   http version={st.http_version} soft-404={st.soft_404} requests made={st.requests_made}")

        res = await seoprobe.ai_access(page, robots=rb, session=s)
        head("ai_access (what an AI agent meets)", res)
        ai = res.unwrap()
        print(f"   score={ai.fraction} checks={[(c.id, c.status) for c in ai.checks[:5]]}")

        res = await seoprobe.techstack(page, session=s)
        head("techstack", res)
        print("  ", res.unwrap().by_category)

        domain = ".".join((urlsplit(page.final_url).hostname or "").split(".")[-2:])  # the registered domain
        res = await seoprobe.dns_health(domain, session=s)
        head(f"dns_health {domain}", res)
        if res.data is not None:
            d = res.data
            print(f"   SPF valid={d.spf.valid} lookups={d.spf.lookups} | DMARC policy={d.dmarc.policy}")
            print(f"   DNSSEC enabled={d.dnssec.enabled} | IPv6: {[(h.host, len(h.aaaa)) for h in d.ipv6]}")


if __name__ == "__main__":
    asyncio.run(main())
