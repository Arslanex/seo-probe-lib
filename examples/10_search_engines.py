"""
10 - Search engines: Google Search Console and GA4 (OAuth), Bing Webmaster, IndexNow.

    python examples/10_search_engines.py [url]
    python examples/10_search_engines.py [url] --code <code from the Google redirect>
    python examples/10_search_engines.py [url] --disconnect  # revoke and forget the Google tokens
    python examples/10_search_engines.py [url] --submit      # really notifies IndexNow

What each needs (set them as environment variables, read by ``Config.from_env()``):

* Search Console / GA4: a Google Cloud OAuth client, ``SEOPROBE_GOOGLE_CLIENT_ID`` and
  ``SEOPROBE_GOOGLE_CLIENT_SECRET``, plus a ``TokenStore`` for each customer's refresh token.
  The flow is stateless: ``gsc.auth_url`` returns an ``AuthRequest`` that *you* keep (a database
  row, a signed cookie) until Google redirects back with a code; ``gsc.exchange_code`` then
  stores the tokens. Here the request is kept in a JSON file between the two runs.
* Bing Webmaster: ``SEOPROBE_BING_API_KEY``.
* IndexNow: a key you choose, published at ``https://<host>/<key>.txt``. ``indexnow.submit`` is
  the only operation of the library that writes to a third party; nothing calls it for you.

Without credentials every call answers with a stable code (``missing-credential``,
``not-connected``) instead of failing: the script shows them and what to configure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

import seoprobe

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
URL = ARGS[0] if ARGS and not ARGS[0].startswith("4/") else "https://www.python.org/"
CODE = sys.argv[sys.argv.index("--code") + 1] if "--code" in sys.argv else None
SUBMIT = "--submit" in sys.argv
DISCONNECT = "--disconnect" in sys.argv
OUT = Path(__file__).parent / "output"
REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"  # must be registered in the Google Cloud console


def line(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    if res.error is None:
        print(f"   {title}: {res.status.value}")
    else:
        print(f"   {title}: {res.error.code.value} {dict(res.error.details)}")


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    host = urlsplit(URL).hostname or ""
    store = seoprobe.FileTokenStore(OUT / "google-tokens.json")  # use an encrypted store in a product
    pending = OUT / "pending-auth-request.json"
    async with seoprobe.Session(seoprobe.Config.from_env(), token_store=store) as s:
        print("== Google Search Console and GA4")
        conn = (await seoprobe.gsc.connection(session=s)).unwrap()
        print(f"   app configured={conn.configured} account connected={conn.connected} email={conn.email}")
        print(f"   preflight gsc.summary: {(await seoprobe.preflight('gsc.summary', session=s)).missing or 'ready'}")

        if DISCONNECT:  # revoke the grant at Google and delete the tokens from the store
            res = await seoprobe.gsc.disconnect(session=s)
            line("gsc.disconnect", res)
            conn = (await seoprobe.gsc.connection(session=s)).unwrap()
        elif CODE and pending.exists():  # second run: trade the code for tokens
            request = seoprobe.AuthRequest.model_validate_json(pending.read_text())
            res = await seoprobe.gsc.exchange_code(request, CODE, session=s)
            line("exchange_code", res)
            pending.unlink(missing_ok=True)
            conn = (await seoprobe.gsc.connection(session=s)).unwrap()
        elif not conn.connected:  # first run: where to send the user
            res = await seoprobe.gsc.auth_url(REDIRECT_URI, services=["gsc", "ga4"], session=s)
            line("auth_url", res)
            if res.ok and res.data is not None:
                pending.write_text(res.data.model_dump_json())  # keep it until the callback
                print(f"   open this URL, allow access, then rerun with --code <code>:\n   {res.data.url}")

        # Every Search Console call. Without a connected account each answers a stable code
        # (not-connected, missing-credential); once connected the same lines print real data.
        props = await seoprobe.gsc.properties(session=s)
        line("gsc.properties", props)
        found = await seoprobe.gsc.find_property(URL, session=s)  # the property that covers URL
        line("gsc.find_property", found)
        prop = found.data.site_url if found.data else f"sc-domain:{host.removeprefix('www.')}"
        if props.data:
            print(f"   properties: {[(p.site_url, p.permission) for p in props.data.items][:5]}")

        res = await seoprobe.gsc.analytics(prop, days=28, dimensions=["query"], row_limit=5, session=s)
        line("gsc.analytics (top queries, 28 days)", res)
        for row in res.data.items if res.data else []:
            print(f"   {row.keys} clicks={row.clicks} impressions={row.impressions} position={row.position:.1f}")
        res = await seoprobe.gsc.top_queries(URL, session=s)
        line("gsc.top_queries", res)
        res = await seoprobe.gsc.inspect(prop, URL, session=s)  # URL Inspection: is it indexed?
        line("gsc.inspect", res)
        if res.data:
            print(
                f"   verdict={res.data.verdict} coverage={res.data.coverage_state} google canonical={res.data.google_canonical}"
            )
        res = await seoprobe.gsc.sitemaps(prop, session=s)
        line("gsc.sitemaps", res)
        res = await seoprobe.gsc.rankings(prop, keywords=["python", "download python"], session=s)
        line("gsc.rankings", res)
        for r in res.data.rows if res.data else []:
            print(f"   {r.keyword!r}: position={r.position} clicks={r.clicks} top page={r.top_page}")
        opp = await seoprobe.gsc.opportunities(prop, session=s)
        line("gsc.opportunities", opp)
        if opp.data:
            print(
                f"   striking={len(opp.data.striking)} low CTR={len(opp.data.low_ctr)} declining={len(opp.data.declining)}"
            )

        # Not an error: the summary says why it has no data, so an audit can carry on without it.
        summary = await seoprobe.gsc.summary(URL, session=s)
        line("gsc.summary", summary)
        print(f"   its data.status = {summary.data.status if summary.data else None!r}")
        print(f"   check_access search_console: {(await seoprobe.check_access('search_console', session=s)).code}")

        print("\n== Google Analytics 4 (the same OAuth connection, the Analytics scope)")
        ga_props = await seoprobe.ga4.properties(session=s)
        line("ga4.properties", ga_props)
        ga_prop = ga_props.data.items[0].property if ga_props.data and ga_props.data.items else "properties/0"
        res = await seoprobe.ga4.landing_pages(ga_prop, days=28, limit=5, session=s)  # organic landing pages
        line("ga4.landing_pages", res)
        for p in res.data.items if res.data else []:
            print(f"   {p.page} sessions={p.sessions} engagement={p.engagement_rate}")
        res = await seoprobe.ga4.summary(URL, session=s)  # finds the GA4 property of URL by itself
        line("ga4.summary", res)
        print(f"   its data.status = {res.data.status if res.data else None!r}")

        print("\n== Bing Webmaster")
        sites = await seoprobe.bing.sites(session=s)
        line("bing.sites", sites)
        site = sites.data.items[0].url if sites.data and sites.data.items else URL
        for name, call in (
            ("bing.query_stats", seoprobe.bing.query_stats(site, session=s)),
            ("bing.crawl_stats", seoprobe.bing.crawl_stats(site, session=s)),
            ("bing.crawl_issues", seoprobe.bing.crawl_issues(site, session=s)),
            ("bing.url_info", seoprobe.bing.url_info(site, URL, session=s)),
        ):
            res = await call
            line(name, res)
            items = getattr(res.data, "items", None) if res.data else None
            if items:
                print(f"   {len(items)} rows, e.g. {items[0].model_dump()}")

        print("\n== IndexNow")
        key = "0123456789abcdef0123456789abcdef"  # choose your own, 8-128 characters [a-zA-Z0-9-]
        kf = (await seoprobe.indexnow.key_file(key, session=s)).unwrap()
        print(f"   publish {kf.path!r} with content {kf.content!r} ({kf.content_type})")
        ver = (await seoprobe.indexnow.verify(host, key, session=s)).unwrap()
        print(f"   verify {ver.key_url}: found={ver.found} matches={ver.matches} status={ver.status}")
        if SUBMIT:
            sub = await seoprobe.indexnow.submit(host, key, [URL], session=s)
            line("indexnow.submit", sub)
        else:
            print("   submit skipped (pass --submit once the key file is published)")


if __name__ == "__main__":
    asyncio.run(main())
