"""The link-health probes take as long as the caller allows, and never cost the operation its result."""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from seoprobe.core.config import Config
from seoprobe.core.net.client import MAX_PAGE_BYTES, RawResponse
from seoprobe.core.page import Page
from seoprobe.core.session import Session
from seoprobe.features.crawl import links
from seoprobe.features.crawl.links import analyze_links
from tests.features.crawl.support import HOST, page

N_LINKS = 12


class Slow:
    """Answers every URL with 200 after ``delay`` seconds; records what it was asked."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.requested: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
        self.requested.append(url)
        await asyncio.sleep(self.delay)
        return RawResponse(url, url, 200, {}, b"ok", False, None, [], 1, 1)

    async def close(self) -> None:
        return None


def _home() -> Page:
    body = "".join(f'<a href="/p{i}">page {i}</a>' for i in range(N_LINKS))
    return page("/", f"<html><body><main>{body}</main></body></html>")


async def test_probes_stop_at_the_deadline_and_the_rest_is_not_checked_not_broken() -> None:
    slow = Slow(0.2)
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    res = await analyze_links(
        [_home()], entry_url=f"{HOST}/", sitemap_urls=set(), fetcher=slow, concurrency=4, deadline=loop.time() + 0.3
    )  # type: ignore[arg-type]
    assert time.monotonic() - t0 < 0.8
    s = res.summary
    assert 0 < s.checked < N_LINKS and s.skipped_time_budget == N_LINKS - s.checked and s.broken == 0
    # some targets were checked: the rule stays judged over them, as with the count limits
    assert {(n.check_id, n.reason_code) for n in res.output.not_checked} >= {("link-health", "time-budget")}
    assert "broken-internal-link" not in {n.check_id for n in res.output.not_checked}
    assert {r.error for r in s.links if not r.checked} == {"time-budget"}


async def test_a_budget_that_checks_nothing_leaves_the_rule_not_checked() -> None:
    loop = asyncio.get_running_loop()
    res = await analyze_links(
        [_home()], entry_url=f"{HOST}/", sitemap_urls=set(), fetcher=Slow(0.2), deadline=loop.time()
    )  # type: ignore[arg-type]
    assert res.summary.checked == 0 and res.summary.skipped_time_budget == N_LINKS
    nc = {(n.check_id, n.reason_code) for n in res.output.not_checked}
    assert ("broken-internal-link", "time-budget") in nc and "link-health" not in {c for c, _ in nc}


async def test_without_a_deadline_every_link_is_probed() -> None:
    res = await analyze_links([_home()], entry_url=f"{HOST}/", sitemap_urls=set(), fetcher=Slow(0.0))  # type: ignore[arg-type]
    assert res.summary.checked == N_LINKS and res.summary.skipped_time_budget == 0
    assert not [n for n in res.output.not_checked if n.reason_code == "time-budget"]


async def test_config_sets_the_default_budget_and_a_call_can_wait_longer() -> None:
    async with Session(Config(link_check_budget_s=0.3, request_delay_s=0), http=Slow(0.2)) as s:  # type: ignore[arg-type]
        quick = await links([_home()], entry_url=f"{HOST}/", session=s)
        patient = await links([_home()], entry_url=f"{HOST}/", link_check_budget_s=math.inf, session=s)
    assert quick.ok and quick.data is not None and quick.data.skipped_time_budget > 0
    assert patient.ok and patient.data is not None and patient.data.checked == N_LINKS


async def test_link_checks_leave_the_operation_time_to_finish() -> None:
    # 12 probes of 1 s at concurrency 4 need 3 s; the operation allows 6 s, and 5 s are kept for
    # the stages after the links: the probes stop after about 1 s and the result is not a timeout
    async with Session(Config(request_delay_s=0), http=Slow(1.0)) as s:  # type: ignore[arg-type]
        t0 = time.monotonic()
        res = await links([_home()], entry_url=f"{HOST}/", timeout_s=6, session=s)
    assert res.ok and res.data is not None and res.data.skipped_time_budget > 0
    assert time.monotonic() - t0 < 3


async def test_probe_limits_and_concurrency_come_from_config() -> None:
    slow = Slow(0.0)
    async with Session(Config(max_internal_link_checks=3, request_delay_s=0), http=slow) as s:  # type: ignore[arg-type]
        res = await links([_home()], entry_url=f"{HOST}/", session=s)
    assert res.data is not None and res.data.checked == 3 and len(slow.requested) == 3


def test_link_settings_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEOPROBE_LINK_CHECK_BUDGET_S", "7.5")
    monkeypatch.setenv("SEOPROBE_MAX_INTERNAL_LINK_CHECKS", "10")
    cfg = Config.from_env()
    assert cfg.link_check_budget_s == 7.5 and cfg.max_internal_link_checks == 10
    assert Config().link_check_budget_s is None  # default: wait as long as the operation allows
