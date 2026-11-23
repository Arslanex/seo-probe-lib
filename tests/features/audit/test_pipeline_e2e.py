"""End-to-end: the whole deterministic pipeline against a fake site (no network, no browser)."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from seoprobe.core.errors import ErrorCode
from seoprobe.core.models import AuditMode
from seoprobe.core.result import Event
from seoprobe.core.rules import catalog
from seoprobe.features.audit import Audit, AuditStatus, audit_page, audit_site, audit_stream, rerun
from seoprobe.features.audit.modules import Module, ModuleInputs, ModuleOutput
from seoprobe.features.performance import FieldData, FieldMetric
from seoprobe.features.report import build
from tests.features.audit.support import BASE, CONFIG, demo_session, no_transport


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    no_transport(monkeypatch)


async def _run(url: str, mode: AuditMode, **kw: Any) -> tuple[list[str], Audit]:
    stages: list[str] = []
    last: Event | None = None
    async with demo_session() as s:
        async for ev in audit_stream(url, mode=mode, session=s, **kw):
            if ev.data.get("stage") and ev.data.get("status") == "done":
                stages.append(ev.data["stage"])
            last = ev
    assert last is not None and last.kind == "finished"
    return stages, Audit.model_validate(last.data["result"]["data"])


async def test_page_mode_end_to_end() -> None:
    stages, rec = await _run(f"{BASE}/hakkimizda", AuditMode.PAGE)
    assert rec.status == AuditStatus.COMPLETED, rec.error
    assert stages == ["validate", "fetch_entry", "robots", "measure", "analyze", "score"]
    assert rec.scores and rec.scores.overall is not None
    ids = {f.id for f in rec.findings}
    assert "missing-meta-description" in ids  # the page has none
    assert rec.lang == "tr" and len(rec.pages) == 1


async def test_site_mode_samples_fetches_scores_and_finds_site_issues() -> None:
    stages, rec = await _run(BASE, AuditMode.SITE, max_pages=25)
    assert rec.status in (AuditStatus.COMPLETED, AuditStatus.PARTIAL), rec.error
    assert {"discovery", "sample", "fetch_pages"} <= set(stages)
    ids = {f.id for f in rec.findings}
    assert "duplicate-title" in ids  # two blog posts share a title
    assert "images-missing-alt" in ids or "images-missing-dimensions" in ids
    assert rec.coverage["pages_fetched"] >= 8 and rec.coverage["templates"]
    assert rec.scores and rec.scores.overall is not None and 0 < rec.scores.overall <= 100
    assert rec.scores.core_overall is not None
    statuses = {p.url: p.status for p in rec.pages}
    assert statuses.get(f"{BASE}/eski-sayfa") == 404 or f"{BASE}/eski-sayfa" not in statuses
    assert "page-status-4xx" in ids or f"{BASE}/eski-sayfa" not in statuses


async def test_unreachable_target_fails_cleanly() -> None:
    _, rec = await _run("https://nowhere.test/", AuditMode.PAGE)
    assert rec.status == AuditStatus.FAILED and rec.error == "unreachable"  # the code only, no detail
    assert rec.error_code == ErrorCode.CONNECT_FAILED
    assert rec.scores is None  # no number is invented for a site we could not reach


async def test_unresolvable_host_fails_with_dns_failed_and_no_http_fallback() -> None:
    async with demo_session() as s:
        res = await audit_page("https://nxdomain.test/", session=s)
        requested = s.http.requested  # type: ignore[attr-defined]
    rec = res.unwrap()
    assert rec.status == AuditStatus.FAILED and rec.error == "dns-failed"
    assert rec.error_code == ErrorCode.DNS_FAILED
    assert requested == ["https://nxdomain.test/"]  # http:// resolves the same name: not retried


async def test_entry_404_is_reportable_and_caps_the_score() -> None:
    _, rec = await _run("https://demo.test/yok", AuditMode.PAGE)
    assert rec.status == AuditStatus.COMPLETED
    assert rec.scores and rec.scores.gate and rec.scores.gate.cap == 40
    assert rec.scores.overall is None or rec.scores.overall <= 40


async def test_private_target_is_rejected_at_validation() -> None:
    _, rec = await _run("http://127.0.0.1:8080/", AuditMode.PAGE)
    assert rec.status == AuditStatus.FAILED and rec.error == "target-rejected"
    assert rec.error_code == ErrorCode.SSRF_BLOCKED
    assert [st.status.value for st in rec.stages] == ["failed"]


async def test_stopping_the_stream_cancels_the_audit() -> None:
    """Source: cancelling a queued audit. Here: a consumer that stops early cancels the run."""
    seen: list[str] = []
    async with demo_session() as s:
        stream = audit_stream(BASE, mode=AuditMode.SITE, session=s)
        async for ev in stream:
            seen.append(ev.kind)
            break
        await stream.aclose()  # type: ignore[attr-defined]
        assert s.on_event is None  # the stream's hook is removed
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    assert seen == ["progress"]


async def test_concurrent_streams_on_one_session_leave_its_hook_as_it_was() -> None:
    seen: list[str] = []

    async def own(ev: Event) -> None:
        seen.append(ev.kind)

    async def stream(url: str, s: Any) -> Event | None:
        last = None
        async for ev in audit_stream(url, session=s):
            last = ev
        return last

    async with demo_session(on_event=own) as s:
        a, b = await asyncio.gather(stream(f"{BASE}/hakkimizda", s), stream(f"{BASE}/", s))
        assert s.on_event is own and s._listeners == []
    assert a is not None and b is not None and a.kind == b.kind == "finished"
    assert seen.count("finished") >= 2  # the session's own hook saw both audits


async def test_the_stream_passes_the_time_budgets_on() -> None:
    last = None
    async with demo_session() as s:
        async for ev in audit_stream(f"{BASE}/hakkimizda", link_check_budget_s=7.0, visibility_budget_s=3.0, session=s):
            last = ev
    assert last is not None and last.kind == "finished"
    options = Audit.model_validate(last.data["result"]["data"]).options
    assert (options.link_check_budget_s, options.visibility_budget_s) == (7.0, 3.0)


async def test_rerun_repeats_url_mode_and_options() -> None:
    async with demo_session() as s:
        first = (
            await audit_page(f"{BASE}/hakkimizda", keywords=[" seo ", ""], link_check_budget_s=12.5, session=s)
        ).unwrap()
        again = (await rerun(first, session=s)).unwrap()
    assert again.id != first.id and again.status == AuditStatus.COMPLETED
    assert (again.target_url, again.mode, again.options) == (first.target_url, first.mode, first.options)
    assert first.options.keywords == ["seo"] and first.options.link_check_budget_s == 12.5


async def test_account_picks_the_search_console_account_and_rerun_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from seoprobe.features.search import MemoryTokenStore, gsc
    from tests.features.search.support import APP

    seen: list[str] = []
    real = gsc.summary

    async def summary(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["account"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(gsc, "summary", summary)
    async with demo_session(replace(CONFIG, google_app=APP), token_store=MemoryTokenStore()) as s:
        first = (await audit_page(f"{BASE}/hakkimizda", account="acme", session=s)).unwrap()
        again = (await rerun(first, session=s)).unwrap()
        default = (await audit_page(f"{BASE}/hakkimizda", session=s)).unwrap()
    assert first.options.account == again.options.account == "acme"
    assert default.options.account == "default"
    assert seen == ["acme", "acme", "default"]


async def test_deep_analysis_results_are_stored_and_scored() -> None:
    stages, rec = await _run(BASE, AuditMode.SITE, max_pages=25)
    assert "analyze" in stages
    # link graph: the home page links to a URL that answers 404
    assert rec.links is not None and rec.links.internal_total > 10 and rec.links.broken >= 1
    assert "broken-internal-link" in {f.id for f in rec.findings}
    assert any(p.url == BASE + "/" and p.depth == 0 for p in rec.links.pages)
    # indexability table: every sampled page has a row, the 404 page is not indexable
    by_url = {r.url: r for r in rec.indexability}
    assert by_url[BASE + "/"].indexable is True
    if BASE + "/eski-sayfa" in by_url:
        assert by_url[BASE + "/eski-sayfa"].indexable is False
    # score v2: every measured category shows the checks behind its score
    assert rec.scores and rec.scores.score_version == "mvp-0.2"
    for c in rec.scores.categories:
        if c.measured:
            assert c.checks_total > 0 and c.checks_passed + c.checks_failed == c.checks_total
    # unmeasured categories say why instead of getting a number
    perf = next(c for c in rec.scores.categories if c.category.value == "performance")
    assert perf.score is None and perf.reason
    # schema: the demo site has Organization markup on the home page only
    assert next(c for c in rec.scores.categories if c.category.value == "schema").measured


async def test_crux_field_data_is_stored_scored_and_answers_inp(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_crux(url: str, origin: str, **kw: Any) -> FieldData:
        return FieldData(
            scope="origin",
            metrics=[
                FieldMetric(metric="lcp", p75=3300, rating="needs-improvement"),
                FieldMetric(metric="inp", p75=420, rating="needs-improvement"),
                FieldMetric(metric="cls", p75=0.02, rating="good"),
            ],
        )

    async def no_psi(url: str, **kw: Any) -> None:
        return None

    google = importlib.import_module("seoprobe.features.performance.google")
    monkeypatch.setattr(google, "fetch_crux", fake_crux)
    monkeypatch.setattr(google, "run_psi", no_psi)
    from dataclasses import replace

    async with demo_session(replace(CONFIG, google_api_key="test-key")) as s:
        s.http.api_session = lambda: object()  # type: ignore[method-assign]  # PSI and CrUX are patched
        rec = (await audit_page(f"{BASE}/hakkimizda", session=s)).unwrap()
    assert rec.field is not None and {m.metric for m in rec.field.metrics} == {"lcp", "inp", "cls"}
    ids = {f.id for f in rec.findings}
    assert "cwv-lcp-needs-improvement" in ids and "cwv-inp-needs-improvement" in ids and "cwv-cls-poor" not in ids
    assert not any(n.check_id.startswith("cwv-inp") for n in rec.not_checked)  # field data answered it
    assert rec.coverage.get("crux") is True and rec.coverage.get("psi") is False


# ----------------------------------------------------------------------------- module isolation


async def _good(inp: ModuleInputs) -> ModuleOutput:
    return ModuleOutput(kind="t_good", data={"value": 7}, available={"gsc"})


async def _crashes(inp: ModuleInputs) -> ModuleOutput:
    raise RuntimeError("boom")


async def _hangs(inp: ModuleInputs) -> ModuleOutput:
    await asyncio.sleep(5)
    return ModuleOutput(kind="t_hang")


async def test_modules_are_isolated_stored_and_provide_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    mods = importlib.import_module("seoprobe.features.audit.modules")
    extra = [
        Module("t-good", "t_good", 5.0, _good),
        Module("t-crash", "t_crash", 5.0, _crashes),
        Module("t-hang", "t_hang", 0.2, _hangs),
    ]
    monkeypatch.setattr(mods, "MODULES", [*mods.MODULES, *extra])
    async with demo_session() as s:
        rec = (await audit_page(f"{BASE}/hakkimizda", session=s)).unwrap()
    assert rec.status == AuditStatus.COMPLETED  # a crashing and a hanging module did not break it
    assert rec.extras["t_good"] == {"value": 7}
    assert "t_crash" not in rec.extras and "t_hang" not in rec.extras
    assert "techstack" in rec.extras  # the real modules still ran


async def test_a_failing_module_operation_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    techstack_mod = importlib.import_module("seoprobe.features.site.techstack")

    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(techstack_mod, "detect", boom)
    async with demo_session() as s:
        rec = (await audit_page(f"{BASE}/hakkimizda", session=s)).unwrap()
    assert rec.status == AuditStatus.COMPLETED and "techstack" not in rec.extras


# ----------------------------------------------------------------------------- contract


async def test_result_is_json_serializable_and_builds_a_report() -> None:
    async with demo_session() as s:
        res = await audit_site(BASE, max_pages=5, seed=1, session=s)
        assert json.loads(res.to_json())["data"]["mode"] == "site"
        report = (await build(res.unwrap(), session=s)).unwrap()
    assert report.audit.status == "completed" and report.quality is not None
    assert report.quality.status != "FAIL"


async def test_invalid_input_is_an_error_result() -> None:
    async with demo_session() as s:
        res = await audit_page("   ", session=s)
    assert res.error is not None and res.error.code == ErrorCode.INVALID_URL


async def test_options_are_limited_like_the_source_service() -> None:
    async with demo_session() as s:
        rec = (
            await audit_page(
                f"{BASE}/hakkimizda",
                keywords=[f"k{i}" for i in range(30)] + ["x" * 100],
                competitors=[f"https://c{i}.test" for i in range(9)],
                extra_urls=[f"{BASE}/{i}" for i in range(30)],
                session=s,
            )
        ).unwrap()
    assert len(rec.options.keywords) == 20 and len(rec.options.competitors) == 5
    assert len(rec.options.extra_urls) == 20


def test_every_rule_id_of_the_pipeline_is_in_the_catalog() -> None:
    root = Path(__file__).parents[3] / "src/seoprobe/features/audit"
    src = (root / "pipeline.py").read_text(encoding="utf-8")
    used = set(re.findall(r'finding\(\s*"([a-z0-9-]+)"', src)) | {"duplicate-title", "duplicate-meta-description"}
    own = set(json.loads((root / "catalog.json").read_text(encoding="utf-8"))["rules"])
    assert len(used) == 9 and used <= own
    assert own <= set(catalog.rules())


# ----------------------------------------------------------------------------- render stage

SHELL = "<html lang='en'><head><title>App</title></head><body><div id='root'></div></body></html>"
RENDERED = (
    "<html lang='en'><head><title>App</title></head><body><div id='root'><h1>Rendered</h1><p>"
    + " ".join(f"word{i}" for i in range(120))
    + "</p></div></body></html>"
)


class StubRenderer:
    launch_error: str | None = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def render(self, url: str) -> Any:
        from seoprobe.core.net.browser import RenderResult

        self.calls.append(url)
        return RenderResult(html=RENDERED, final_url=url, status=200)

    async def judge(self, url: str) -> None:
        return None


async def test_render_stage_renders_spa_pages_and_flags_js_dependent_content() -> None:
    from dataclasses import replace

    from tests import fakes

    renderer = StubRenderer()
    pages = {f"{BASE}/app": fakes.html_page(SHELL)}
    async with fakes.session(pages, config=replace(CONFIG, render=True), browser=renderer) as s:
        rec = (await audit_page(f"{BASE}/app", session=s)).unwrap()
    assert renderer.calls == [f"{BASE}/app"]
    assert "render" in [st.stage.value for st in rec.stages if st.status.value == "done"]
    page = rec.pages[0]
    assert page.is_spa is True and page.rendered is True and page.js_gap and page.js_gap["gap"] is True
    assert page.word_count and page.word_count > 100  # measured on the rendered HTML
    assert "js-dependent-content" in {f.id for f in rec.findings}
    assert len(rec.perf) == 2 and all(p.error for p in rec.perf)  # the stub cannot measure
