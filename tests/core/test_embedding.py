"""
The library is a component: it runs unchanged inside an async app, a thread pool or a process pool.

Results and exceptions cross process boundaries (pickle), parsing never blocks the caller's event
loop, and a session used from the wrong event loop fails clearly instead of hanging.
"""

from __future__ import annotations

import asyncio
import importlib
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import seoprobe
from seoprobe.core.errors import ErrorCode, OperationError, SeoprobeError
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.result import Result, Status
from tests import fakes

URL = "https://example.com/"
PAGE = fakes.html_page("<html><head><title>Hello</title></head><body><h1>Hi</h1></body></html>")


@operation("explode", output=dict, requires=Requirements(network=False))
async def explode(ctx: Context) -> Outcome[dict]:  # type: ignore[type-arg]
    raise ValueError("internal detail")


async def test_results_and_their_errors_survive_pickling() -> None:
    async with fakes.session({URL: PAGE}) as s:
        ok = await seoprobe.fetch(URL, session=s)
    failed = await seoprobe.fetch("ftp://example.com/")
    internal = await explode()
    assert internal.error is not None and internal.error.code is ErrorCode.INTERNAL_ERROR
    for res in (ok, failed, internal):
        back = pickle.loads(pickle.dumps(res))
        assert isinstance(back, Result) and back.to_dict() == res.to_dict()
    page = pickle.loads(pickle.dumps(ok)).unwrap()
    assert page.parsed is not None and page.parsed.title == "Hello"


def test_exceptions_survive_pickling() -> None:
    err = SeoprobeError(ErrorCode.TIMEOUT, "took too long", result=None)
    back = pickle.loads(pickle.dumps(err))
    assert (back.code, back.message, str(back)) == (err.code, err.message, str(err))
    op = pickle.loads(pickle.dumps(OperationError(ErrorCode.SSRF_BLOCKED, details={"url": "u", "ip": "10.0.0.1"})))
    assert op.code is ErrorCode.SSRF_BLOCKED and op.details == {"url": "u"}


async def test_unwrap_error_from_a_failed_result_pickles_with_its_result() -> None:
    res = await seoprobe.fetch("ftp://example.com/")
    with pytest.raises(SeoprobeError) as info:
        res.unwrap()
    back = pickle.loads(pickle.dumps(info.value))
    assert back.code is ErrorCode.UNSUPPORTED_SCHEME and back.result.status is Status.ERROR


async def test_pages_are_parsed_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    page_module = importlib.import_module("seoprobe.core.page")
    real = page_module.parse_html
    threads: list[str] = []

    def spy(html: str, url: str) -> object:
        threads.append(threading.current_thread().name)
        return real(html, url)

    monkeypatch.setattr(page_module, "parse_html", spy)
    async with fakes.session({URL: PAGE}) as s:
        res = await seoprobe.onpage(URL, session=s)
    assert res.ok and threads and threading.main_thread().name not in threads


def test_operations_run_in_many_threads_each_with_its_own_loop() -> None:
    def work(i: int) -> str:
        async def run() -> str:
            async with fakes.session({URL: PAGE}) as s:
                res = await seoprobe.fetch(URL, session=s)
            return res.unwrap().parsed.title  # type: ignore[union-attr]

        return asyncio.run(run())

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(work, range(8))) == ["Hello"] * 8


def test_a_session_used_from_another_event_loop_fails_clearly() -> None:
    session = fakes.session({URL: PAGE})

    async def open_it() -> None:
        await session.__aenter__()

    asyncio.run(open_it())  # the session now belongs to that (finished) loop

    async def use_it() -> None:
        await seoprobe.fetch(URL, session=session)

    with pytest.raises(RuntimeError, match="another event loop"):
        asyncio.run(use_it())


async def test_an_open_session_cannot_be_entered_twice() -> None:
    async with fakes.session() as s:
        with pytest.raises(RuntimeError, match="already open"):
            await s.__aenter__()
