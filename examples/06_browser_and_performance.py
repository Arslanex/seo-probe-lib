"""
06 - In a real browser: rendering, screenshots, mobile, accessibility, lab speed, carbon.

    pip install "seoprobe[browser]" && python -m playwright install chromium
    python examples/06_browser_and_performance.py [url]

The headless Chromium starts once per session, on first use; every page gets a fresh,
isolated context, and every request Chromium makes passes the same SSRF policy as the HTTP
client. PageSpeed and CrUX need a Google API key (``SEOPROBE_GOOGLE_API_KEY``); without it they
answer ``missing-credential`` and ``web_vitals`` combines what it can measure itself.
Screenshots are written to ``examples/output/``.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"
OUT = Path(__file__).parent / "output"


def head(title: str, res: seoprobe.Result) -> None:  # type: ignore[type-arg]
    code = res.error.code.value if res.error else ""
    print(
        f"\n== {title}: {res.status.value} {code} | findings {len(res.findings)} | not checked {len(res.not_checked)}"
    )
    for f in res.findings[:3]:
        print(f"   [{f.severity.value:8}] {f.id}: {f.message[:100]}")


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with seoprobe.Session(seoprobe.Config.from_env()) as s:
        page = (await seoprobe.fetch(URL, session=s)).unwrap()

        # render: is the content there without JavaScript, or only after it runs (SPA)?
        res = await seoprobe.render(page, force=True, session=s)
        head("render", res)
        r = res.unwrap()
        print(f"   rendered={r.rendered} is_spa={r.is_spa} js_gap={r.js_gap} reasons={r.reasons}")

        # screenshots: desktop and mobile, plus what is visible above the fold.
        res = await seoprobe.screenshots(page, session=s)
        head("screenshots", res)
        shots = res.unwrap()
        for f in shots.files:
            path = OUT / f.name
            path.write_bytes(base64.b64decode(f.data_base64 or ""))
            print(f"   {f.device:7} {f.width}x{f.height} {f.bytes // 1024} KB -> {path}")
        for device, fold in shots.above_fold.items():
            print(f"   above the fold on {device}: h1 visible={fold.h1_visible} call to action={fold.cta_visible}")

        # mobile: tap targets, small text, horizontal overflow, the viewport meta tag.
        res = await seoprobe.mobile(page, session=s)
        head("mobile", res)
        m = res.unwrap()
        print(
            f"   small tap targets {m.small_tap_targets}/{m.tap_targets}, small text {m.small_text_nodes}/{m.text_nodes}"
        )
        print(f"   horizontal overflow={m.horizontal_overflow} viewport meta={m.viewport_meta!r}")

        # accessibility: axe-core against WCAG 2.1 AA (or wcag2a, wcag22aa ...).
        res = await seoprobe.accessibility(page, standard="wcag21aa", session=s)
        head("accessibility", res)
        a = res.unwrap()
        print(f"   axe {a.axe_version}: {len(a.violations)} violated rules, by impact {a.violations_by_impact}")
        for v in a.violations[:3]:
            print(f"   {v.impact:9} {v.id}: {v.help} ({v.nodes_count} elements) {v.wcag}")

        # lab_metrics: timings and weight in our own browser, per device.
        res = await seoprobe.lab_metrics([page, (page.final_url, "mobile")], session=s)
        head("lab_metrics", res)
        for p in res.unwrap().items:
            print(
                f"   {p.device:7} TTFB {p.ttfb_ms} ms, FCP {p.fcp_ms} ms, LCP {p.lcp_ms} ms, CLS {p.cls}, "
                f"TBT {p.tbt_ms} ms, {p.total_requests} requests, {p.total_bytes // 1024 if p.total_bytes else '?'} KB"
            )

        # carbon: an estimate of the CO2 per visit from the transferred bytes (Sustainable Web Design model).
        res = await seoprobe.carbon(page, session=s)
        head("carbon", res)
        c = res.unwrap()
        print(
            f"   {c.co2_grams} g CO2 per visit, rating {c.rating}, {c.bytes // 1024} KB ({c.bytes_source}), model {c.model}"
        )

        # Google data: PageSpeed Insights (lab, Google's machines) and CrUX (real Chrome users).
        for name, call in (
            ("pagespeed", seoprobe.pagespeed(URL, session=s)),
            ("field_data", seoprobe.field_data(URL, session=s)),
        ):
            res = await call
            head(name, res)
            if res.error is not None:
                print(f"   needs: {res.error.details.get('missing')} (set SEOPROBE_GOOGLE_API_KEY)")

        # web_vitals: the best available answer per metric, from lab, PageSpeed and CrUX together.
        res = await seoprobe.web_vitals(URL, session=s)
        head("web_vitals", res)
        wv = res.unwrap()
        print(
            f"   lab rows={len(wv.lab)} pagespeed={'yes' if wv.pagespeed else 'no'} field={'yes' if wv.field else 'no'}"
        )
        for nc in res.not_checked:
            print(f"   not checked: {nc.check_id} ({nc.reason_code})")


if __name__ == "__main__":
    asyncio.run(main())
