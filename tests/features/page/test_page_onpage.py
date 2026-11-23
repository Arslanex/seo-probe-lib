"""
The ``onpage`` operation: same findings as ``run_page_checks`` on the golden pages, the
not-parsed branch, language hint, localisation, JSON output and catalog coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from seoprobe.core.config import Config
from seoprobe.core.page import Page
from seoprobe.core.result import Status
from seoprobe.core.rules import catalog
from seoprobe.features.page import FEATURE, OnPageReport, onpage
from seoprobe.features.page.onpage import PAGE_RULE_IDS, run_page_checks
from tests import fakes

U = "https://example.com/guide/"
SRC = Path(__file__).resolve().parents[3] / "src" / "seoprobe" / "features" / "page"
GOLDEN = sorted(p.name for p in (Path(__file__).resolve().parents[2] / "fixtures").glob("*.html"))


def _headers(name: str) -> dict[str, str]:
    return {"x-robots-tag": "noindex"} if name == "x_robots_header.html" else {}


@pytest.mark.parametrize("name", GOLDEN)
async def test_onpage_matches_run_page_checks_on_golden_pages(fixtures: Path, name: str) -> None:
    pages = {U: fakes.html_page((fixtures / name).read_text(encoding="utf-8"), headers=_headers(name))}
    async with fakes.session(pages) as s:
        res = await onpage(U, session=s)
        page = Page.from_fetch(await s.http.fetch(U))
    assert res.ok and res.data is not None and res.data.checked
    assert page.parsed is not None
    direct = run_page_checks(page, page.parsed)
    assert [f.model_dump() for f in res.findings] == [f.model_dump() for f in direct.findings]
    assert [n.model_dump() for n in res.not_checked] == [n.model_dump() for n in direct.not_checked]
    assert res.data.facts == direct.facts
    assert res.data.title == (page.parsed.title or None)
    assert res.data.word_count == page.parsed.word_count
    assert res.status == (Status.PARTIAL if direct.not_checked else Status.OK)


async def test_onpage_accepts_a_page_without_fetching(fixtures: Path) -> None:
    html = (fixtures / "missing_basics.html").read_text(encoding="utf-8")
    page = Page(url=U, final_url=U, status=200, headers={"content-type": "text/html"}, html=html)
    async with fakes.session({}) as s:
        res = await onpage(page, session=s)
        assert s.http.requested == []  # type: ignore[attr-defined]
    assert res.ok and res.data is not None and res.data.checked
    assert {f.id for f in res.findings} == {f.id for f in run_page_checks(page, page.parsed).findings}  # type: ignore[arg-type]
    assert all(f.url == U for f in res.findings)


@pytest.mark.parametrize(
    "response",
    [
        fakes.html_page("<html><body>gone</body></html>", status=404),
        (200, {"content-type": "application/pdf"}, b"%PDF-1.4"),
    ],
    ids=["status-404", "not-html"],
)
async def test_page_without_parseable_html_is_not_checked(response: tuple[int, dict[str, str], bytes]) -> None:
    async with fakes.session({U: response}) as s:
        res = await onpage(U, session=s)
    assert res.status == Status.OK
    assert res.data == OnPageReport(url=U, checked=False)
    assert res.findings == [] and res.not_checked == []


async def test_fetch_error_fails_the_operation() -> None:
    async with fakes.session(errors={U: "unreachable"}) as s:
        res = await onpage(U, session=s)
    assert res.status == Status.ERROR and res.error is not None and res.data is None


async def test_lang_hint_is_passed_to_the_relevance_gate(fixtures: Path) -> None:
    pages = {U: fakes.html_page((fixtures / "relevance_tr.html").read_text(encoding="utf-8"))}
    async with fakes.session(pages) as s:
        plain = await onpage(U, session=s)
        hinted = await onpage(U, session=s, lang="en")
    assert "language-unsupported" in {n.reason_code for n in plain.not_checked}
    assert "language-unsupported" not in {n.reason_code for n in hinted.not_checked}
    assert hinted.data is not None and hinted.data.facts["relevance_language_supported"] is True


async def test_findings_are_localized_to_the_session_language(fixtures: Path) -> None:
    pages = {U: fakes.html_page((fixtures / "missing_basics.html").read_text(encoding="utf-8"))}
    async with fakes.session(pages, config=Config(lang="tr")) as s:
        res = await onpage(U, session=s)
    assert res.findings
    for f in res.findings:
        assert f.message == catalog.message(f.message_key, "tr")


async def test_result_is_json_serializable(fixtures: Path) -> None:
    pages = {U: fakes.html_page((fixtures / "missing_basics.html").read_text(encoding="utf-8"))}
    async with fakes.session(pages) as s:
        res = await onpage(U, session=s)
    body = json.loads(res.to_json())
    assert body["operation"] == "onpage" and body["feature"] == "page"
    assert body["data"]["checked"] is True and body["data"]["url"] == U
    assert {f["id"] for f in body["findings"]} == {f.id for f in res.findings}
    assert OnPageReport.model_validate(body["data"]) == res.data


def test_every_rule_id_in_the_code_exists_in_the_catalog() -> None:
    code = (SRC / "onpage.py").read_text(encoding="utf-8")
    used = set(re.findall(r'(?:add|skip)\(\s*"([a-z0-9-]+)"', code))
    for block in re.findall(r"for cid in \((.*?)\):", code, re.S):
        used |= set(re.findall(r'"([a-z0-9-]+)"', block))
    assert len(used) == 47
    assert used == set(PAGE_RULE_IDS)
    assert set(PAGE_RULE_IDS) == set(FEATURE.catalog.rules)
    assert used <= set(catalog.rules())
