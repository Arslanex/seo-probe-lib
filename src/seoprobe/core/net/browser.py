"""
Headless renderer (Playwright) with an SSRF guard on every request.

Design: one browser, a fresh isolated context per render,
at most ``max_concurrent`` renders at a time. Every request Chromium makes,
including redirect hops and sub-resources, is judged by the same policy as the
HTTP fetcher before it leaves the machine. Service workers are blocked and
WebSockets are mocked (never connected), because ``route()`` cannot see them.

Residual risk: Chromium resolves DNS itself, so a hostile resolver can still race the route
handler. A server deployment therefore needs a network-level egress policy (see the
"Deployment requirements" section of the README).

The policy hooks (``url_check``, ``resolve``, ``chromium_args``) exist so tests can
serve a local page. Production callers never pass them.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, Field

from seoprobe.core.net.safety import Target, URLSafetyError, check_resolved, check_url

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        Request,
        Route,
        WebSocketRoute,
    )

try:  # the [browser] extra; without it the renderer reports itself unavailable
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - exercised by the packaging test
    async_playwright = None  # type: ignore[assignment]

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


def playwright_installed() -> bool:
    return async_playwright is not None


UrlCheck = Callable[[str], Target]
Resolve = Callable[[str], Awaitable[list[str]]]

DEFAULT_BLOCKED_TYPES = ("image", "media", "font")
MAX_REQUESTS = 150
MAX_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
STABILITY_BUDGET_MS = 5000
VIEWPORT = {"width": 1366, "height": 768}
DESKTOP_VIEWPORT = VIEWPORT
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_DPR = 3.0
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)


class RenderUnavailable(RuntimeError):
    """The browser could not be started (not installed, sandbox problem)."""


class RenderResult(BaseModel):
    html: str | None = None
    final_url: str | None = None
    status: int | None = None
    error: str | None = None  # stable code: "target-rejected: ...", "timeout", "render-failed: ..."
    duration_ms: int = 0
    request_count: int = 0
    bytes_downloaded: int = 0
    dom_stable: bool = False
    blocked_requests: list[dict[str, str]] = Field(default_factory=list)  # [{url, reason}] (capped)


async def system_resolve(host: str) -> list[str]:
    """Every A/AAAA record of ``host`` via the event loop's resolver."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise URLSafetyError(f"DNS resolution failed for {host}: {exc}") from exc
    return [str(i[4][0]) for i in infos]


class _State:
    def __init__(
        self,
        max_requests: int = MAX_REQUESTS,
        max_bytes: int = MAX_BYTES,
        recorder: list[dict[str, Any]] | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        # Optional per-response log (``open_page``). The route handler sees the ORIGINAL response
        # headers (content-encoding, cache-control, content-length) that ``fulfill`` strips, so
        # the lab performance measurement records them here.
        self.recorder = recorder
        self.requests = 0
        self.bytes = 0
        self.final_url: str | None = None  # last URL of the main document's redirect chain
        self.blocked: list[dict[str, str]] = []

    def block(self, url: str, reason: str) -> None:
        if len(self.blocked) < 50:
            self.blocked.append({"url": url[:200], "reason": reason})


def _never_connect(ws: WebSocketRoute) -> None:
    """Leave the routed WebSocket mocked: no connection ever reaches a server."""
    return None


@dataclass
class PageSession:
    page: Page
    state: _State
    recorder: list[dict[str, Any]]


class Renderer:
    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        max_requests: int = MAX_REQUESTS,
        max_bytes: int = MAX_BYTES,
        user_agent: str | None = None,
        url_check: UrlCheck = check_url,
        resolve: Resolve = system_resolve,
        chromium_args: list[str] | None = None,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_requests = max_requests
        self._max_bytes = max_bytes
        self._ua = user_agent
        self._url_check = url_check
        self._resolve = resolve
        self._args = chromium_args or []
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_error: str | None = None

    async def __aenter__(self) -> Renderer:
        await self.start()
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            if async_playwright is None:
                raise RenderUnavailable("playwright is not installed (pip install 'seoprobe[browser]')")
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=self._args)
        except Exception as exc:  # noqa: BLE001 - browser not installed, sandbox problem, ...
            self._launch_error = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            await self.close()

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except PlaywrightError:
                pass
            self._pw = None

    # ------------------------------------------------------------------ policy

    async def _judge(self, url: str) -> None:
        """Raise ``URLSafetyError`` unless ``url`` is an acceptable network target."""
        target = self._url_check(url)
        if not target.is_ip_literal:
            check_resolved(target.hostname, await self._resolve(target.hostname))

    def _handler(
        self, state: _State, blocked_types: tuple[str, ...], context: BrowserContext
    ) -> Callable[[Route, Request], Awaitable[None]]:
        async def handler(route: Route, request: Request) -> None:
            try:
                url = request.url
                scheme = urlsplit(url).scheme.lower()
                if scheme in ("data", "blob", "about"):
                    await route.continue_()  # no network involved
                    return
                if scheme not in ("http", "https"):
                    state.block(url, "scheme")
                    await route.abort()
                    return
                if request.resource_type in blocked_types:
                    await route.abort()
                    return
                state.requests += 1
                if state.requests > state.max_requests:
                    state.block(url, "request-limit")
                    await route.abort()
                    return
                if state.bytes > state.max_bytes:
                    state.block(url, "byte-limit")
                    await route.abort()
                    return
                try:
                    await self._judge(url)
                except URLSafetyError as exc:
                    state.block(url, f"policy: {exc}")
                    await route.abort()
                    return
                # Chromium follows redirects internally and the route handler never sees the
                # hops (verified in tests: a 302 to a loopback IP was fetched, and a fulfilled
                # 302 is followed WITHOUT being routed again). So no 3xx ever reaches Chromium:
                # the chain is followed here, every hop is judged, and Chromium only receives
                # the final response.
                fetch_t0 = time.monotonic()
                resp = await route.fetch(max_redirects=0, timeout=15000)
                current = url
                for _hop in range(MAX_REDIRECTS + 1):
                    location = resp.headers.get("location")
                    if not (300 <= resp.status < 400 and location):
                        break
                    if request.method != "GET":
                        state.block(url, "policy: redirect on a non-GET request")
                        await route.abort()
                        return
                    nxt = urljoin(current, location)
                    try:
                        await self._judge(nxt)
                    except URLSafetyError as exc:
                        state.block(nxt, f"policy: redirect: {exc}")
                        await route.abort()
                        return
                    state.requests += 1
                    if state.requests > state.max_requests:
                        state.block(nxt, "request-limit")
                        await route.abort()
                        return
                    resp = await context.request.get(nxt, max_redirects=0, timeout=15000)
                    current = nxt
                else:
                    state.block(url, "policy: too many redirects")
                    await route.abort()
                    return
                if current != url and request.is_navigation_request() and request.frame.parent_frame is None:
                    state.final_url = current
                body = await resp.body()
                state.bytes += len(body)
                if state.recorder is not None and len(state.recorder) < 600:
                    try:
                        is_main = request.is_navigation_request() and request.frame.parent_frame is None
                    except Exception:  # noqa: BLE001 - a detached frame must not stop the recording
                        is_main = False
                    state.recorder.append(
                        {
                            "url": url,
                            "resolved_url": current,
                            "method": request.method,
                            "resource_type": request.resource_type,
                            "status": resp.status,
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "body_len": len(body),
                            "head": bytes(body[:32]),
                            "fetch_ms": int((time.monotonic() - fetch_t0) * 1000),
                            "is_main": is_main,
                        }
                    )
                headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
                }
                await route.fulfill(status=resp.status, headers=headers, body=body)
            except Exception:  # noqa: BLE001 - fail closed
                try:
                    await route.abort()
                except Exception:  # noqa: BLE001 - the route may already be handled
                    pass

        return handler

    # ------------------------------------------------------------------ guarded page sessions

    async def judge(self, url: str) -> None:
        """Public form of the policy check (raises ``URLSafetyError``)."""
        await self._judge(url)

    @property
    def launch_error(self) -> str | None:
        return self._launch_error

    @asynccontextmanager
    async def open_page(
        self,
        *,
        viewport: dict[str, int] | None = None,
        mobile: bool = False,
        device_scale_factor: float = 1.0,
        user_agent: str | None = None,
        blocked_types: tuple[str, ...] = (),
        max_requests: int | None = None,
        max_bytes: int | None = None,
    ) -> AsyncIterator[PageSession]:
        """
        An isolated, fully guarded page for measurements (same route guard as ``render``).

        Every request is judged and recorded in ``session.recorder`` (original response headers).
        Raises ``RenderUnavailable`` when the browser cannot start. The caller must have judged the
        entry URL with ``judge`` first.
        """
        if self._browser is None:
            await self.start()
        if self._browser is None:
            raise RenderUnavailable(self._launch_error or "browser unavailable")
        recorder: list[dict[str, Any]] = []
        state = _State(max_requests or self._max_requests, max_bytes or self._max_bytes, recorder)
        async with self._sem:
            context: BrowserContext | None = None
            try:
                kwargs: dict[str, Any] = {
                    "service_workers": "block",
                    "accept_downloads": False,
                    "viewport": viewport or VIEWPORT,
                    "device_scale_factor": device_scale_factor,
                    "is_mobile": mobile,
                    "has_touch": mobile,
                }
                ua = user_agent or self._ua
                if ua:
                    kwargs["user_agent"] = ua
                context = await self._browser.new_context(**kwargs)
                await context.route("**/*", self._handler(state, blocked_types, context))
                await context.route_web_socket("**/*", _never_connect)
                page = await context.new_page()
                yield PageSession(page=page, state=state, recorder=recorder)
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except PlaywrightError:
                        pass

    # ------------------------------------------------------------------ render

    async def render(
        self,
        url: str,
        *,
        timeout_ms: int = 15000,
        block_resource_types: tuple[str, ...] = DEFAULT_BLOCKED_TYPES,
    ) -> RenderResult:
        """Render ``url``; never raises."""
        t0 = time.monotonic()

        def done(res: RenderResult) -> RenderResult:
            res.duration_ms = int((time.monotonic() - t0) * 1000)
            return res

        try:
            await self._judge(url)
        except URLSafetyError as exc:
            return done(RenderResult(error=f"target-rejected: {exc}"))
        if self._browser is None:
            await self.start()
        if self._browser is None:
            return done(RenderResult(error=f"render-failed: {self._launch_error or 'browser unavailable'}"))
        async with self._sem:
            try:
                res = await asyncio.wait_for(
                    self._render_once(url, timeout_ms, block_resource_types),
                    timeout=timeout_ms / 1000 + STABILITY_BUDGET_MS / 1000 + 5,
                )
            except TimeoutError:
                res = RenderResult(error="timeout")
            except Exception as exc:  # noqa: BLE001 - a render failure is a result, not an exception
                res = RenderResult(error=f"render-failed: {type(exc).__name__}: {str(exc)[:200]}")
        return done(res)

    async def _render_once(self, url: str, timeout_ms: int, blocked_types: tuple[str, ...]) -> RenderResult:
        assert self._browser is not None
        state = _State(self._max_requests, self._max_bytes)
        context: BrowserContext | None = None
        try:
            kwargs: dict[str, Any] = {
                "service_workers": "block",  # route() cannot see service-worker traffic
                "accept_downloads": False,
                "viewport": VIEWPORT,
            }
            if self._ua:
                kwargs["user_agent"] = self._ua
            context = await self._browser.new_context(**kwargs)
            await context.route("**/*", self._handler(state, blocked_types, context))
            await context.route_web_socket("**/*", _never_connect)
            page = await context.new_page()

            status: int | None = None
            error: str | None = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response else None
            except PlaywrightError as exc:
                msg = str(exc)
                error = "timeout" if "Timeout" in msg else f"render-failed: navigation: {msg.splitlines()[0][:200]}"
            if error is not None:
                return RenderResult(error=error, request_count=state.requests, blocked_requests=state.blocked)
            stable = await self._wait_for_dom_stability(page, timeout_ms)
            html = await page.content()
            return RenderResult(
                html=html,
                final_url=state.final_url or page.url,
                status=status,
                request_count=state.requests,
                bytes_downloaded=state.bytes,
                dom_stable=stable,
                blocked_requests=state.blocked,
            )
        finally:
            if context is not None:
                try:
                    await context.close()
                except PlaywrightError:
                    pass

    @staticmethod
    async def _wait_for_dom_stability(page: Page, timeout_ms: int) -> bool:
        """Wait up to five seconds for meaningful body text and a stable DOM."""
        budget = max(250, min(timeout_ms, STABILITY_BUDGET_MS))
        previous: tuple[int, int] | None = None
        stable = 0
        elapsed = 0
        while elapsed < budget:
            try:
                raw = await page.evaluate(
                    "() => [(document.body && document.body.innerText || '').trim().length,"
                    "document.querySelectorAll('*').length]"
                )
                signature = (int(raw[0]), int(raw[1]))
            except Exception:  # noqa: BLE001 - a page that navigated away is simply not stable
                return False
            if signature == previous and signature[0] >= 100:
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
            previous = signature
            await page.wait_for_timeout(250)
            elapsed += 250
        return False


__all__ = [
    "DEFAULT_BLOCKED_TYPES",
    "DESKTOP_VIEWPORT",
    "MOBILE_DPR",
    "MOBILE_UA",
    "MOBILE_VIEWPORT",
    "PageSession",
    "PlaywrightError",
    "RenderResult",
    "RenderUnavailable",
    "Renderer",
    "playwright_installed",
]
