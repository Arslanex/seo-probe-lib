"""
09 - The full audit and the report: everything above in one call, scored.

    python examples/09_audit_and_report.py [url]

* ``audit_page`` runs every applicable analysis on one page and scores it; ``audit_site``
  samples the site first. ``audit_stream`` is the same with live progress events (for a
  progress bar). The score is deterministic: the same pages give the same findings and score.
* Two time budgets decide how long you wait for the slow, optional parts: the link-health
  probes and the public lookups. What a budget cuts short is ``not_checked``, never a failure.
* ``report.build`` turns an audit into a JSON report (texts in ``Config.lang``); with
  ``previous=`` it compares with the earlier audit. ``report.plan``, ``report.findings`` and
  ``report.compare`` work on stored audits without any network.
The audit and the report are written to ``examples/output/`` as JSON.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import seoprobe

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.python.org/"
OUT = Path(__file__).parent / "output"
BUDGETS = {"link_check_budget_s": 8, "visibility_budget_s": 8}  # wait at most ~8 s for each


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with seoprobe.Session(seoprobe.Config(lang="en", render=True)) as s:
        # 1) One page, with live progress (what a progress bar would show).
        t0 = time.monotonic()
        result = None
        async for ev in seoprobe.audit_stream(URL, keywords=["python"], session=s, **BUDGETS):
            if ev.kind == "progress" and ev.data.get("status") == "done":
                print(f"   {time.monotonic() - t0:5.1f} s  stage {ev.data.get('stage')} done")
            if ev.kind == "finished":
                result = ev.data["result"]  # the Result as a dict: ready to store or send
        assert result is not None
        page_audit = seoprobe.Audit.model_validate(result["data"])
        sc = page_audit.scores
        print(
            f"\naudit_page: {page_audit.status.value} in {time.monotonic() - t0:.0f} s, score {sc.overall if sc else None}"
        )
        print(
            f"   {len(page_audit.findings)} findings, {len(page_audit.not_checked)} not checked, cost {page_audit.cost}"
        )
        for cat in (sc.categories if sc else [])[:6]:
            print(f"   {cat.category.value if hasattr(cat.category, 'value') else cat.category:14} {cat.score}")

        # 2) A small site sample: the same analyses over 5 pages, then one score.
        t0 = time.monotonic()
        site = (await seoprobe.audit_site(URL, max_pages=5, seed=7, session=s, **BUDGETS)).unwrap()
        print(
            f"\naudit_site: {site.status.value} in {time.monotonic() - t0:.0f} s, {len(site.pages)} pages, score {site.scores.overall if site.scores else None}"
        )
        for p in site.pages:
            print(f"   {p.status} score {p.page_score} {p.url}")

        # 3) The report, compared with the previous (page) audit.
        report = (await seoprobe.report.build(site, previous=page_audit, session=s)).unwrap()
        print(
            f"\nreport: overall {report.scores.overall.value} ({report.scores.overall.band}), {len(report.findings)} findings"
        )
        for rec in report.recommendations[:3]:
            print(f"   [{rec.severity.value}] {rec.title} (effort {rec.effort.level}, impact {rec.impact.level})")

        plan = (await seoprobe.report.plan(report, session=s)).unwrap()
        for phase in plan.phases:
            print(f"   phase {phase.number} {phase.name} ({phase.timeframe}): {len(phase.items)} actions")

        high = (await seoprobe.report.findings(report, severity=seoprobe.Severity.HIGH, session=s)).unwrap()
        print(f"   high severity findings: {[f.check_id for f in high.items]}")

        cmp = (await seoprobe.report.compare(page_audit, site, session=s)).unwrap()
        print(f"   compare page vs site audit: comparable={cmp.comparable} warnings={cmp.warnings}")

        # 4) rerun: a fresh audit with the same URL, mode and options (budgets included).
        again = (await seoprobe.rerun(page_audit, session=s)).unwrap()
        print(
            f"\nrerun: {again.status.value}, score {again.scores.overall if again.scores else None}, same options: {again.options == page_audit.options}"
        )

        (OUT / "audit_site.json").write_text(site.model_dump_json(indent=1))
        (OUT / "report.json").write_text(report.model_dump_json(indent=1))
        print(f"\nwritten: {OUT / 'audit_site.json'} and {OUT / 'report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
