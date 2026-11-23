"""
The public lookups of ``visibility`` take as long as the caller allows; what is cut short stays
``not_checked`` and the lookups that answered keep their data.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import time
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.features.audit import audit_page
from seoprobe.features.offsite import visibility as visibility_op
from tests import fakes
from tests.features.audit.support import BASE, demo_session, no_transport

visibility = importlib.import_module("seoprobe.features.offsite.visibility")


def _lookups(monkeypatch: pytest.MonkeyPatch, *, rdap: float, slow: float) -> None:
    """RDAP answers after ``rdap`` seconds, Wayback and Common Crawl after ``slow`` seconds."""

    def after(delay: float, answer: dict[str, Any]) -> Any:
        async def lookup(d: str, s: Any, **kw: Any) -> dict[str, Any]:
            await asyncio.sleep(delay)
            return answer

        return lookup

    monkeypatch.setattr(visibility, "lookup_rdap", after(rdap, {"age_days": 4000}))
    monkeypatch.setattr(visibility, "lookup_wayback", after(slow, {"urls_archived_sample": 9}))
    monkeypatch.setattr(visibility, "lookup_commoncrawl", after(slow, {"present": True}))


async def test_a_budget_cuts_the_slow_lookups_and_keeps_the_fast_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _lookups(monkeypatch, rdap=0.0, slow=5.0)
    async with fakes.session() as s:
        s.http.api_session = lambda: object()  # type: ignore[method-assign]
        t0 = time.monotonic()
        res = await visibility_op("a.org", budget_s=0.3, session=s)
    assert time.monotonic() - t0 < 1.5
    assert res.status == "partial" and res.data is not None
    assert res.data.rdap == {"age_days": 4000}
    assert res.data.wayback == {"error": "time-budget"} and res.data.common_crawl == {"error": "time-budget"}
    assert [(n.check_id, n.reason_code) for n in res.not_checked] == [("visibility", "time-budget")]
    assert "not-in-common-crawl" not in res.data.notes  # an unanswered lookup is no evidence


async def test_config_sets_the_default_and_a_call_can_wait_longer(monkeypatch: pytest.MonkeyPatch) -> None:
    _lookups(monkeypatch, rdap=0.0, slow=0.5)
    async with fakes.session(config=Config(visibility_budget_s=0.1)) as s:
        s.http.api_session = lambda: object()  # type: ignore[method-assign]
        quick = await visibility_op("a.org", session=s)
        patient = await visibility_op("a.org", budget_s=math.inf, session=s)
    assert quick.status == "partial"
    assert patient.status == "ok" and patient.data is not None and patient.data.common_crawl == {"present": True}


async def test_lookups_leave_the_operation_time_to_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    # the operation allows 6 s and keeps 5 s for itself: the lookups stop after about 1 s
    _lookups(monkeypatch, rdap=0.0, slow=10.0)
    async with fakes.session() as s:
        s.http.api_session = lambda: object()  # type: ignore[method-assign]
        t0 = time.monotonic()
        res = await visibility_op("a.org", timeout_s=6, session=s)
    assert res.status == "partial" and time.monotonic() - t0 < 3


async def test_the_audit_keeps_the_budget_and_reports_what_was_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    no_transport(monkeypatch)
    _lookups(monkeypatch, rdap=0.0, slow=5.0)
    async with demo_session(Config(render=False, visibility=True, request_delay_s=0.0)) as s:
        s.http.api_session = lambda: object()  # type: ignore[method-assign]
        t0 = time.monotonic()
        rec = (await audit_page(f"{BASE}/hakkimizda", visibility_budget_s=0.3, session=s)).unwrap()
    assert time.monotonic() - t0 < 3
    assert rec.options.visibility_budget_s == 0.3 and rec.visibility is not None
    assert rec.visibility.wayback == {"error": "time-budget"}
    assert ("visibility", "time-budget") in {(n.check_id, n.reason_code) for n in rec.not_checked}
