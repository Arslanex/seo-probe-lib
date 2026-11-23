"""The shared behaviour of every operation (``@operation``) and of ``Session``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from seoprobe.core.config import Config
from seoprobe.core.errors import ErrorCode, OperationError, SeoprobeError
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.client import mask_url, record, recording
from seoprobe.core.result import Event, Status
from seoprobe.core.session import Session
from tests import fakes


class Echo(BaseModel):
    value: int
    trace_id: str = ""
    remaining: float | None = None


@operation("echo", output=Echo, requires=Requirements(network=False), timeout_s=5)
async def echo(ctx: Context, value: int) -> Outcome[Echo]:
    return Outcome(data=Echo(value=value, trace_id=ctx.trace_id, remaining=ctx.remaining_s()))


@operation("slow", output=Echo, requires=Requirements(network=False), timeout_s=0.05)
async def slow(ctx: Context) -> Outcome[Echo]:
    await asyncio.sleep(5)
    return Outcome(data=Echo(value=0))


@operation("boom", output=Echo, requires=Requirements(network=False))
async def boom(ctx: Context) -> Outcome[Echo]:
    raise ZeroDivisionError("secret detail")


@operation("refuse", output=Echo, requires=Requirements(network=False))
async def refuse(ctx: Context) -> Outcome[Echo]:
    ctx.fail(ErrorCode.NO_PERMISSION, service="gsc", internal_ip="10.0.0.1")


@operation("keyed", output=Echo, requires=Requirements(credentials=["google_api_key"]))
async def keyed(ctx: Context) -> Outcome[Echo]:
    return Outcome(data=Echo(value=1))


@operation("needs_extra", output=Echo, requires=Requirements(extras=["no-such-module-xyz"]))
async def needs_extra(ctx: Context) -> Outcome[Echo]:
    return Outcome(data=Echo(value=1))


@operation("outer", output=Echo, requires=Requirements(network=False), timeout_s=1.0)
async def outer(ctx: Context) -> Outcome[Echo]:
    inner = await echo(7)  # no session passed: inherits the caller's
    assert inner.data is not None
    return Outcome(data=Echo(value=inner.data.value, trace_id=inner.data.trace_id, remaining=inner.data.remaining))


@operation("partial", output=Echo, requires=Requirements(network=False))
async def partial(ctx: Context) -> Outcome[Echo]:
    return Outcome(data=Echo(value=1), not_checked=[ctx.not_checked("x", "missing-credential:google_api_key")])


@operation("logs", output=Echo, requires=Requirements(network=False))
async def logs(ctx: Context) -> Outcome[Echo]:
    import time

    record("https://api.example/v1?key=SECRET&q=1", status=200, started=time.monotonic(), size=10, error=None)
    return Outcome(data=Echo(value=1))


@operation("finds", output=Echo, requires=Requirements(network=False))
async def finds(ctx: Context) -> Outcome[Echo]:
    from seoprobe.core.rules import catalog

    return Outcome(data=Echo(value=1), findings=[catalog.make_finding("missing-title", url="https://e.com/")])


async def test_success_result_and_json() -> None:
    res = await echo(3)
    assert res.ok and res.status == Status.OK and res.data and res.data.value == 3
    assert res.operation == "echo" and res.feature == "core" and res.meta.schema_version == "echo/1"
    assert res.unwrap().value == 3
    assert '"value":3' in res.to_json() and res.to_dict()["status"] == "ok"


async def test_wrong_argument_type_is_a_programming_error() -> None:
    with pytest.raises(ValidationError):
        await echo("not a number")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await echo()  # type: ignore[call-arg]


async def test_missing_credential_and_extra_fail_before_running() -> None:
    res = await keyed()
    assert res.status == Status.ERROR and res.error and res.error.code == ErrorCode.MISSING_CREDENTIAL
    assert res.error.details["missing"] == "credential:google_api_key"
    async with Session(Config(google_api_key="k")) as s:
        assert (await keyed(session=s)).ok
    res = await needs_extra()
    assert res.error and res.error.code == ErrorCode.MISSING_EXTRA


async def test_timeout_becomes_a_timeout_error() -> None:
    res = await slow()
    assert res.error and res.error.code == ErrorCode.TIMEOUT and res.error.retryable


async def test_caller_timeout_overrides_the_default() -> None:
    res = await echo(1, timeout_s=0.5)
    assert res.data and res.data.remaining is not None and res.data.remaining <= 0.5


async def test_unexpected_exception_is_internal_error_with_chained_cause() -> None:
    res = await boom()
    assert res.error and res.error.code == ErrorCode.INTERNAL_ERROR
    assert res.error.details == {"exception": "ZeroDivisionError"}
    assert "secret detail" not in res.to_json()
    with pytest.raises(SeoprobeError) as exc:
        res.unwrap()
    assert isinstance(exc.value.__cause__, ZeroDivisionError) and exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_operation_error_keeps_only_safe_details() -> None:
    res = await refuse()
    assert res.error and res.error.code == ErrorCode.NO_PERMISSION
    assert res.error.details == {"service": "gsc"}


async def test_error_messages_follow_the_session_language() -> None:
    async with Session(Config(lang="tr")) as s:
        res = await refuse(session=s)
    assert res.error and res.error.message == "Hesabın bu kaynak için izni yok."


async def test_cancellation_is_never_swallowed() -> None:
    task = asyncio.create_task(slow(timeout_s=10))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_nested_call_inherits_session_trace_and_deadline() -> None:
    async with Session() as s:
        res = await outer(session=s)
    assert res.ok and res.data and res.data.value == 7
    assert res.data.trace_id == res.meta.trace_id  # one trace for the whole call tree
    assert res.data.remaining is not None and res.data.remaining <= 1.0  # inner 5 s capped by outer 1 s


async def test_not_checked_makes_the_result_partial() -> None:
    res = await partial()
    assert res.status == Status.PARTIAL and res.ok


async def test_request_log_masks_secrets() -> None:
    res = await logs()
    [entry] = res.meta.requests
    assert "SECRET" not in entry.url and "key=***" in entry.url and "q=1" in entry.url
    assert mask_url("https://x/?code=abc&state=def&page=2") == "https://x/?code=***&state=***&page=2"


async def test_findings_are_localized_to_the_session_language() -> None:
    async with Session(Config(lang="tr")) as s:
        res = await finds(session=s)
    [f] = res.findings
    assert f.message.startswith("Sayfada title etiketi yok")
    res_en = await finds()
    assert res_en.findings[0].message != f.message


async def test_events_are_emitted_and_a_failing_hook_does_not_break_the_operation() -> None:
    seen: list[Event] = []

    async def hook(ev: Event) -> None:
        seen.append(ev)
        raise RuntimeError("hook bug")

    async with Session(on_event=hook) as s:
        res = await echo(1, session=s)
    assert res.ok
    assert [e.kind for e in seen] == ["started", "finished"]
    assert seen[1].status == Status.OK and seen[1].duration_ms is not None


async def test_byte_budget_stops_further_requests() -> None:
    pages = {f"https://e.com/{i}": fakes.html_page("x" * 1000) for i in range(3)}
    async with fakes.session(pages) as s:
        with recording(1500) as rec:
            from seoprobe.core.net.client import budget_exhausted

            assert not budget_exhausted()
            rec.size_bytes = 1500
            assert budget_exhausted()
        assert not budget_exhausted()  # outside the block no budget applies
        assert s.http is not None


async def test_session_requires_async_with_and_closes_its_client() -> None:
    s = Session()
    with pytest.raises(RuntimeError):
        _ = s.http
    async with s:
        client = s.http
        session = client._get_session()
    assert session.closed


async def test_operation_error_class_is_not_raised_to_callers() -> None:
    # OperationError only travels inside an operation
    try:
        await refuse()
    except OperationError:  # pragma: no cover
        pytest.fail("OperationError escaped")


def test_signature_shows_the_caller_parameters() -> None:
    import inspect

    params = list(inspect.signature(echo).parameters)
    assert params == ["value", "session", "timeout_s"]


async def test_fetch_and_normalize_operations() -> None:
    from seoprobe.core.page import fetch, normalize

    async with fakes.session({"https://e.com/": fakes.html_page("<title>T</title>")}) as s:
        res = await fetch("https://e.com/", session=s)
        assert res.ok and res.data and res.data.parsed and res.data.parsed.title == "T"
        missing = await fetch("https://e.com/none", session=s)
        assert missing.ok and missing.data and missing.data.status == 404  # a response is data, not an error
    async with fakes.session(errors={"https://down.test/": "unreachable"}) as s:
        res = await fetch("https://down.test/", session=s)
        assert res.error and res.error.code == ErrorCode.CONNECT_FAILED
    n = await normalize("Example.COM/a?utm_source=x")
    assert n.data and n.data.allowed and n.data.host == "example.com"
    assert (await normalize("http://10.0.0.1/")).data.reason == "ssrf-blocked"  # type: ignore[union-attr]


def _unused(*_: Any) -> None:  # keep imports used by type checkers
    return None


@operation("needs_browser", output=Echo, requires=Requirements(browser=True))
async def needs_browser(ctx: Context) -> Outcome[Echo]:
    return Outcome(data=Echo(value=1))


async def test_browser_operation_with_rendering_disabled_is_browser_unavailable() -> None:
    async with Session(Config(render=False)) as s:
        res = await needs_browser(session=s)
    assert res.error and res.error.code == ErrorCode.BROWSER_UNAVAILABLE
    assert res.error.details["missing"] == "config:render"


def test_tests_cannot_reach_the_real_network() -> None:
    import socket

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(RuntimeError, match="network access is disabled"):
            udp.sendto(b"x", ("8.8.8.8", 53))
    finally:
        udp.close()
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is disabled"):
            tcp.connect(("93.184.216.34", 80))
    finally:
        tcp.close()


@operation("logs_outer", output=Echo, requires=Requirements(network=False))
async def logs_outer(ctx: Context) -> Outcome[Echo]:
    inner = await logs()
    assert len(inner.meta.requests) == 1
    return Outcome(data=Echo(value=1))


async def test_requests_of_nested_operations_are_part_of_the_outer_log() -> None:
    res = await logs_outer()
    assert len(res.meta.requests) == 1 and "key=***" in res.meta.requests[0].url
