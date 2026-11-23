"""``audit_stream``: the same audit, as an async iterator of its progress events."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from seoprobe.core.models import AuditMode
from seoprobe.core.result import Event, Result
from seoprobe.core.session import Session

from .audit import audit_page, audit_site
from .models import Audit

QUEUE_SIZE = 100

# The queue of the stream whose audit task is running in this context (child tasks inherit it).
_queue: ContextVar[asyncio.Queue[Event] | None] = ContextVar("seoprobe_audit_stream", default=None)


async def audit_stream(
    url: str,
    *,
    mode: AuditMode = AuditMode.PAGE,
    session: Session | None = None,
    timeout_s: float | None = None,
    max_pages: int | None = None,
    extra_urls: list[str] | None = None,
    keywords: list[str] | None = None,
    competitors: list[str] | None = None,
    seed: int | None = None,
    link_check_budget_s: float | None = None,
    visibility_budget_s: float | None = None,
    account: str = "default",
) -> AsyncIterator[Event]:
    """
    Run ``audit_page`` / ``audit_site`` and yield its progress events as they happen.

    The last event has ``kind="finished"`` and ``data={"result": <Result[Audit] as dict>}``. The
    session's own ``on_event`` hook keeps receiving every event. Stopping the iteration cancels
    the audit.
    """
    async with AsyncExitStack() as stack:
        if session is None:
            session = await stack.enter_async_context(Session())
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)

        async def hook(event: Event) -> None:
            if event.kind == "progress" and _queue.get() is queue:
                await queue.put(event)

        async def run() -> Result[Audit]:
            _queue.set(queue)
            if mode == AuditMode.SITE:
                return await audit_site(
                    url,
                    max_pages=max_pages,
                    extra_urls=extra_urls,
                    keywords=keywords,
                    competitors=competitors,
                    seed=seed,
                    link_check_budget_s=link_check_budget_s,
                    visibility_budget_s=visibility_budget_s,
                    account=account,
                    session=session,
                    timeout_s=timeout_s,
                )
            return await audit_page(
                url,
                keywords=keywords,
                competitors=competitors,
                extra_urls=extra_urls,
                seed=seed,
                link_check_budget_s=link_check_budget_s,
                visibility_budget_s=visibility_budget_s,
                account=account,
                session=session,
                timeout_s=timeout_s,
            )

        stack.enter_context(session.listening(hook))  # the session's own on_event keeps working
        task = asyncio.create_task(run())
        get: asyncio.Task[Event] | None = None
        try:
            while True:
                get = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield get.result()
                    continue
                get.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get
                while not queue.empty():
                    yield queue.get_nowait()
                break
            result = task.result()
            data: dict[str, Any] = {"result": result.to_dict()}
            yield Event(
                kind="finished",
                operation=result.operation,
                trace_id=result.meta.trace_id,
                at=datetime.now(UTC),
                status=result.status,
                duration_ms=result.meta.duration_ms,
                requests=len(result.meta.requests),
                data=data,
            )
        finally:
            if get is not None and not get.done():
                get.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
