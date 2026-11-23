"""
Session: the one lifecycle the caller manages.

It brings together three concrete objects: the HTTP client (with its per-host limiter), the
browser (started on first use, once) and the optional OAuth token store. Use it as an async
context manager; everything is closed in reverse order on exit, also on cancellation.

Concurrency: many tasks of one event loop may share a session. A session must not be shared
between threads or event loops (the same rule as ``aiohttp.ClientSession``). In a server, one
long-lived session per process shares the connection pool, the politeness limits and the browser.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from seoprobe.core.config import Config
from seoprobe.core.net.client import HttpClient
from seoprobe.core.result import Event

if TYPE_CHECKING:
    from seoprobe.core.net.browser import Renderer

log = logging.getLogger("seoprobe")
EventHook = Callable[[Event], Awaitable[None] | None]


class Session:
    def __init__(
        self,
        config: Config | None = None,
        *,
        on_event: EventHook | None = None,
        token_store: Any = None,
        http: HttpClient | None = None,
        browser: Renderer | None = None,
    ) -> None:
        self.config = config or Config()
        self.on_event = on_event
        self.token_store = token_store
        self._http = http
        self._browser = browser
        self._browser_given = browser is not None
        self._browser_lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners: list[EventHook] = []

    async def __aenter__(self) -> Session:
        if self._stack is not None:
            raise RuntimeError("this Session is already open; share the open session instead of entering it again")
        self._loop = asyncio.get_running_loop()
        self._stack = AsyncExitStack()
        try:
            if self._http is None:
                self._http = HttpClient(self.config)
                await self._stack.enter_async_context(self._http)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._loop = None
        if not self._browser_given:
            self._browser = None

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            raise RuntimeError("use the session as 'async with Session(...) as s'")
        self._check_loop()
        return self._http

    def _check_loop(self) -> None:
        """A clear error instead of a hang when another thread or event loop uses this session."""
        if self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is not self._loop:
            raise RuntimeError(
                "this Session belongs to another event loop; open one Session per event loop "
                "(per thread or process), or pass no session to use a temporary one"
            )

    async def browser(self) -> Renderer:
        """The session's browser, started once on first use (check ``launch_error``)."""
        self._check_loop()
        async with self._browser_lock:
            if self._browser is None:
                from seoprobe.core.net.browser import Renderer

                if self._stack is None:
                    raise RuntimeError("use the session as 'async with Session(...) as s'")
                renderer = Renderer(max_concurrent=self.config.browser_concurrency)
                await self._stack.enter_async_context(renderer)
                self._browser = renderer
            return self._browser

    @contextmanager
    def listening(self, hook: EventHook) -> Iterator[None]:
        """
        Also send every event to ``hook`` while the block runs. ``on_event`` stays as it is, so
        any number of listeners (``audit_stream`` runs, for example) may come and go concurrently.
        """
        self._listeners.append(hook)
        try:
            yield
        finally:
            self._listeners.remove(hook)

    async def emit(self, event: Event) -> None:
        hooks = ([self.on_event] if self.on_event is not None else []) + list(self._listeners)
        for hook in hooks:
            try:
                out = hook(event)
                if inspect.isawaitable(out):
                    await out
            except Exception:  # a failing hook must not break the operation
                log.warning("on_event hook failed for %s %s", event.operation, event.kind, exc_info=True)
