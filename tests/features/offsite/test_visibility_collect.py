"""collect_visibility: concurrency, error isolation, note codes."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from seoprobe.features.offsite import FEATURE, VisibilitySignals
from seoprobe.features.offsite import visibility as visibility_op
from tests import fakes

visibility = importlib.import_module("seoprobe.features.offsite.visibility")
collect_visibility = visibility.collect_visibility
notes_for = visibility.notes_for


def _v(
    age: int | None = None, wb: int | None = 5, wb_err: str | None = None, cc: bool | None = True
) -> VisibilitySignals:
    return VisibilitySignals(
        domain="d",
        rdap={"age_days": age},
        wayback={"urls_archived_sample": wb, "error": wb_err},
        common_crawl={"present": cc},
    )


@pytest.mark.parametrize(
    "age,expected",
    [
        (0, ["domain-under-180-days"]),
        (179, ["domain-under-180-days"]),
        (180, ["domain-under-1-year"]),
        (364, ["domain-under-1-year"]),
        (365, []),
        (None, []),
    ],
)
def test_domain_age_thresholds(age: int | None, expected: list[str]) -> None:
    assert notes_for(_v(age=age)) == [*expected, "gsc-not-connected"]


def test_other_note_codes() -> None:
    assert "no-wayback-captures" in notes_for(_v(wb=0))
    assert "no-wayback-captures" not in notes_for(_v(wb=0, wb_err="timeout"))  # an error is not "no captures"
    assert "not-in-common-crawl" in notes_for(_v(cc=False))
    assert "not-in-common-crawl" not in notes_for(_v(cc=None))  # unknown is not "absent"
    assert notes_for(_v()) == ["gsc-not-connected"]


class _Sess:
    closed = False

    async def close(self) -> None:
        self.closed = True


async def test_all_three_sources_are_collected_and_search_console_is_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def rd(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        seen.append(f"rdap:{d}")
        return {"created": "2026-10-26", "age_days": 17, "error": None}

    async def wb(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        seen.append(f"wb:{d}")
        return {"urls_archived_sample": 0, "error": None}

    async def cc(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        seen.append(f"cc:{d}")
        return {"present": False, "error": None}

    monkeypatch.setattr(visibility, "lookup_rdap", rd)
    monkeypatch.setattr(visibility, "lookup_wayback", wb)
    monkeypatch.setattr(visibility, "lookup_commoncrawl", cc)
    sess = _Sess()
    v = await collect_visibility("https://www.getlastcheck.com/x", sess)
    assert sorted(seen) == [
        "cc:www.getlastcheck.com",
        "rdap:getlastcheck.com",
        "wb:www.getlastcheck.com",
    ]
    assert v.domain == "www.getlastcheck.com" and v.search_console == {"connected": False}
    assert v.notes == [
        "domain-under-180-days",
        "no-wayback-captures",
        "not-in-common-crawl",
        "gsc-not-connected",
    ]
    assert sess.closed is False  # a caller-supplied session is never closed


async def test_one_failing_source_does_not_break_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    async def ok_wb(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        return {"first_capture": "2014-04-20", "urls_archived_sample": 9, "error": None}

    async def ok_cc(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        return {"present": True, "pages_seen_sample": 3, "error": None}

    monkeypatch.setattr(visibility, "lookup_rdap", boom)
    monkeypatch.setattr(visibility, "lookup_wayback", ok_wb)
    monkeypatch.setattr(visibility, "lookup_commoncrawl", ok_cc)
    v = await collect_visibility("a.org", _Sess())
    assert v.rdap["error"] == "internal-error: RuntimeError" and "secret detail" not in str(v.rdap)
    assert v.wayback["first_capture"] == "2014-04-20" and v.common_crawl["present"] is True
    assert v.notes == ["gsc-not-connected"]


async def test_the_operation_uses_the_api_session_and_never_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Source: "an own session is closed". The library owns the API session (HttpClient closes
    # it with the Session); the operation only borrows it.
    sess = _Sess()
    got: list[Any] = []

    async def nothing(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        got.append(s)
        return {"error": "timeout"}

    for n in ("lookup_rdap", "lookup_wayback", "lookup_commoncrawl"):
        monkeypatch.setattr(visibility, n, nothing)
    async with fakes.session() as s:
        s.http.api_session = lambda: sess  # type: ignore[method-assign]
        res = await visibility_op("a.org", session=s)
    assert got == [sess, sess, sess] and sess.closed is False
    assert res.status == "ok" and res.data is not None
    assert res.data.rdap == {"error": "timeout"} and res.data.notes == ["gsc-not-connected"]


async def test_lookup_errors_never_fail_the_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(d: str, s: Any, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    for n in ("lookup_rdap", "lookup_wayback", "lookup_commoncrawl"):
        monkeypatch.setattr(visibility, n, boom)
    async with fakes.session() as s:
        s.http.api_session = lambda: _Sess()  # type: ignore[method-assign]
        res = await visibility_op("https://a.org/", session=s)
    assert res.ok and res.data is not None and res.data.domain == "a.org"
    assert res.data.common_crawl == {"error": "internal-error: RuntimeError"}
    body = json.loads(res.to_json())
    assert body["operation"] == "visibility" and "secret" not in res.to_json()


def test_registered() -> None:
    assert visibility_op in FEATURE.operations and visibility_op.timeout_s == 120
