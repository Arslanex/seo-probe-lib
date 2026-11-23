# Parity snapshots

These files are the output of the **source project** (`seo-analyzer-mvp`, the behaviour this
library was ported from) on the demo site in `tests/demo_site.py`: page mode on two pages and
site mode on the home page, sample seed 12345, no browser, no LLM, no API keys, transport checks
disabled. No request leaves the machine, so the snapshots are deterministic.
`tests/features/audit/test_parity.py` runs the same scenarios through `seoprobe` and requires the
same findings, not-checked entries, scores, pages, coverage and analysis data.

Fields that are random or time-based by nature (audit id, timestamps, `wall_ms`, the random soft-404
probe path) are normalised by the test before comparing.

Regenerate (only when the source project changes) from the source repository:

```bash
# empty key variables: load_dotenv() does not override them, so .env keys are not used
env SEOA_GOOGLE_API_KEY= SEOA_PSI_API_KEY= SEOA_OPENPAGERANK_KEY= ANTHROPIC_API_KEY= \
    PYTHONPATH=src:. .venv/bin/python make_parity.py <out dir>
```

```python
"""Run the seo-analyzer-mvp pipeline on the e2e fake site and dump parity snapshots.

Run from a checkout of the seo-analyzer-mvp project with:
    PYTHONPATH=src:. .venv/bin/python <this file> <out dir>
No network, no browser, no LLM, transport checks disabled (they need a real TLS host).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from seoa.application.audit import service as service_mod
from seoa.application.reporting.builder import build_report
from seoa.bootstrap import build_local_service
from seoa.config import Settings
from seoa.domain import AuditMode, CheckOutput
from seoa.infrastructure import adapters
from tests.e2e.site import BASE, build
from tests.support.fixture_fetcher import FixtureFetcher

SEED = 12345
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


async def no_transport(entry_url: str, fr: Any, fetcher: Any) -> CheckOutput:
    return CheckOutput()


adapters.run_transport = no_transport  # type: ignore[assignment]
service_mod.zlib = SimpleNamespace(crc32=lambda b: SEED)  # type: ignore[attr-defined]

SCENARIOS = [
    ("page_hakkimizda", f"{BASE}/hakkimizda", AuditMode.PAGE, {}),
    ("page_home", f"{BASE}/", AuditMode.PAGE, {"keywords": ["demo hizmet"]}),
    ("site_home", f"{BASE}/", AuditMode.SITE, {"keywords": ["demo hizmet"]}),
]


def snapshot(rec: Any) -> dict[str, Any]:
    d = rec.model_dump(mode="json")
    findings = sorted(
        (
            {k: f.get(k) for k in ("id", "url", "severity", "category", "scope", "evidence", "count", "confidence")}
            for f in d["findings"]
        ),
        key=lambda f: (f["id"], f["url"] or "", f["evidence"] or "", f["count"] or 0),
    )
    not_checked = sorted(d["not_checked"], key=lambda n: (n["check_id"], n["reason_code"], n["url"] or ""))
    pages = sorted(
        (
            {
                k: p.get(k)
                for k in (
                    "url",
                    "final_url",
                    "status",
                    "sample_kind",
                    "template_key",
                    "title",
                    "word_count",
                    "page_score",
                    "rendered",
                    "is_spa",
                    "error",
                )
            }
            for p in d["pages"]
        ),
        key=lambda p: p["url"],
    )
    sc = d["scores"] or {}
    scores = {
        "overall": sc.get("overall"),
        "raw_overall": sc.get("raw_overall"),
        "coverage_pct": sc.get("coverage_pct"),
        "insufficient_data": sc.get("insufficient_data"),
        "gate": sc.get("gate"),
        "confidence": sc.get("confidence"),
        "core_overall": sc.get("core_overall"),
        "p25": sc.get("p25"),
        "categories": [
            {
                k: c.get(k)
                for k in ("category", "score", "measured", "reason", "checks_total", "checks_passed", "checks_failed")
            }
            for c in sc.get("categories", [])
        ],
    }
    return {
        "status": d["status"],
        "mode": d["mode"],
        "target_url": d["target_url"],
        "final_origin": d["final_origin"],
        "business_type": d["business_type"],
        "lang": d["lang"],
        "error": d["error"],
        "coverage": d["coverage"],
        "findings": findings,
        "not_checked": not_checked,
        "pages": pages,
        "scores": scores,
        "links": d["links"],
        "indexability": sorted(d["indexability"], key=lambda r: r["url"]),
        "extras": d["extras"],
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            data_dir=Path(tmp),
            render_enabled=False,
            visibility_enabled=False,
            anthropic_api_key=None,
            request_delay_s=0.0,
        )
        svc = build_local_service(
            settings,
            fetcher=FixtureFetcher(
                build(), errors={"https://nowhere.test/": "unreachable", "http://nowhere.test/": "unreachable"}
            ),
            workers=1,
        )
        await svc.start()
        try:
            for name, url, mode, kw in SCENARIOS:
                aid = await svc.create_audit(url, mode, enable_llm=False, lang="en", **kw)
                async for _ in svc.events(aid):
                    pass
                rec = await svc.get_audit(aid)
                assert rec is not None
                snap = snapshot(rec)
                snap["report_en"] = build_report(rec, "en").model_dump(mode="json")
                (out / f"{name}.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
                print(
                    name,
                    rec.status.value,
                    "findings",
                    len(rec.findings),
                    "nc",
                    len(rec.not_checked),
                    "overall",
                    rec.scores and rec.scores.overall,
                )
        finally:
            await svc.stop()


asyncio.run(main())
```
