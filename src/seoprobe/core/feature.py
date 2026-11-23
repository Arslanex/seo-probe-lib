"""
Features and operations: the one place that holds the behaviour every operation shares.

``@operation`` turns ``async def op(ctx, ...) -> Outcome[T]`` into
``async def op(..., *, session=None, timeout_s=None) -> Result[T]``. The stages run in a fixed
order, each in its own small function below: validate input, take a session, check
requirements, set up context (trace id, deadline, request log), run under the deadline, map
exceptions to ``ErrorCode``, observe (log and events), build the ``Result``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar, get_type_hints

from pydantic import ConfigDict, ValidationError, validate_call

from seoprobe.core.config import Config, package_version
from seoprobe.core.errors import RETRYABLE, ErrorCode, ErrorInfo, OperationError, fetch_error_code
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.client import recording
from seoprobe.core.result import Event, Meta, Result, Status
from seoprobe.core.rules import catalog
from seoprobe.core.rules.catalog import Catalog

if TYPE_CHECKING:
    from seoprobe.core.net.browser import Renderer
    from seoprobe.core.net.client import HttpClient
    from seoprobe.core.page import Page
    from seoprobe.core.session import Session

T = TypeVar("T")
log = logging.getLogger("seoprobe")

# extra name -> module that proves it is installed
EXTRAS = {"browser": "playwright", "dns": "dns"}
# a budgeted stage leaves the rest of its operation this share of the remaining time (at least the floor)
BUDGET_RESERVE_SHARE, BUDGET_RESERVE_MIN_S = 0.2, 5.0
TIME_BUDGET = "time-budget"  # the reason code of what a budget cut short


def missing_requirements(req: Requirements, session: Session, *, oauth: bool = True) -> list[str]:
    """
    What ``session`` lacks to run an operation that needs ``req`` (no network access): tokens
    such as ``extra:browser``, ``credential:google_api_key``, ``config:render``.

    ``seoprobe.preflight`` returns this list; a run that cannot start returns the same tokens,
    joined with ", ", in ``error.details["missing"]`` (details are always strings). The run itself
    leaves OAuth out (``oauth=False``): the Google operations answer it with their own codes
    (``missing-credential``, ``not-connected``).
    """
    missing = [f"extra:{x}" for x in req.extras if importlib.util.find_spec(EXTRAS.get(x, x)) is None]
    missing += [f"credential:{c}" for c in req.credentials if not getattr(session.config, c, None)]
    if req.browser and not session.config.render:
        missing.append("config:render")
    if oauth and req.oauth:
        if session.config.google_app is None:
            missing.append("credential:google_app")
        if session.token_store is None:
            missing.append("config:token_store")
    return missing


@dataclass
class Outcome(Generic[T]):
    """What an operation body returns; ``@operation`` wraps it into a ``Result``."""

    data: T | None = None
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[NotChecked] = field(default_factory=list)
    partial: bool = False


@dataclass
class _Frame:
    context: Context
    deadline: float | None  # event loop time


_current: ContextVar[_Frame | None] = ContextVar("seoprobe_operation", default=None)


class Context:
    """What an operation body gets: the session's resources and helpers bound to this run."""

    def __init__(self, session: Session, op: Operation[Any], trace_id: str) -> None:
        self.session = session
        self.config: Config = session.config
        self.http: HttpClient = session.http
        self.operation = op.name
        self.feature = op.feature
        self.trace_id = trace_id
        self.lang = self.config.lang
        self.log = logging.getLogger(f"seoprobe.{op.feature}.{op.name}")

    async def page(self, target: str | Page, *, sample_kind: str = "core") -> Page:
        """
        ``target`` itself when it is a Page, else the fetched page (fails the operation when
        no response was received).

        An unusable URL (``invalid-url``, ``unsupported-scheme``) fails before any request. Every
        other refusal is the HTTP client's (``ssrf-blocked``), which judges each redirect hop too.
        """
        from seoprobe.core.net.safety import URLSafetyError, check_url
        from seoprobe.core.page import Page

        if isinstance(target, Page):
            await target.parse()
            return target
        try:
            check_url(target)
        except URLSafetyError as exc:
            if exc.code is not ErrorCode.SSRF_BLOCKED:
                self.fail(exc.code, url=target)
        fr = await self.http.fetch(target)
        if fr.error:
            self.log.debug("fetch %s failed: %s %s", target, fr.error, fr.error_detail)
            self.fail(fetch_error_code(fr.error), url=target)
        page = Page.from_fetch(fr, sample_kind=sample_kind)  # type: ignore[arg-type]
        await page.parse()
        return page

    async def browser(self) -> Renderer:
        """The session's browser; fails the operation when it cannot start."""
        from seoprobe.core.net.browser import playwright_installed

        if not playwright_installed():
            self.fail(ErrorCode.MISSING_EXTRA, missing="browser")
        renderer = await self.session.browser()
        if renderer.launch_error:
            self.log.debug("browser unavailable: %s", renderer.launch_error)
            self.fail(ErrorCode.BROWSER_UNAVAILABLE)
        return renderer

    def finding(self, check_id: str, **kwargs: Any) -> Finding:
        return catalog.make_finding(check_id, lang=self.lang, **kwargs)

    def not_checked(self, check_id: str, reason_code: str, url: str | None = None) -> NotChecked:
        return NotChecked(check_id=check_id, reason_code=reason_code, url=url)

    def remaining_s(self) -> float | None:
        frame = _current.get()
        if frame is None or frame.deadline is None:
            return None
        return max(0.0, frame.deadline - asyncio.get_running_loop().time())

    def budget_deadline(self, budget_s: float | None) -> float | None:
        """
        Event-loop time a budgeted stage (link probes, public lookups) stops at: ``budget_s``
        seconds from now (``None`` or ``math.inf``: no limit of its own), and never later than this
        operation's deadline minus a reserve for the stages that follow it. A caller who wants to
        wait longer raises the budget and, if needed, the operation's ``timeout_s``.
        """
        limits = [] if budget_s is None or budget_s == math.inf else [max(0.0, budget_s)]
        remaining = self.remaining_s()
        if remaining is not None:
            limits.append(max(0.0, remaining - max(BUDGET_RESERVE_MIN_S, remaining * BUDGET_RESERVE_SHARE)))
        return asyncio.get_running_loop().time() + min(limits) if limits else None

    async def emit(self, kind: str = "progress", **data: Any) -> None:
        await self.session.emit(
            Event(kind=kind, operation=self.operation, trace_id=self.trace_id, at=datetime.now(UTC), data=data)
        )

    def fail(self, code: ErrorCode, **details: Any) -> NoReturn:
        raise OperationError(code, details=details)


class Operation(Generic[T]):
    """A public operation. Call it like the function it wraps, plus ``session=`` / ``timeout_s=``."""

    def __init__(
        self,
        fn: Callable[..., Awaitable[Outcome[T]]],
        *,
        name: str,
        output: type[Any],
        requires: Requirements,
        timeout_s: float,
        schema_version: int,
    ) -> None:
        self.fn = fn
        self.name = name
        self.output = output
        self.requires = requires
        self.timeout_s = timeout_s
        self.schema_version = schema_version
        module = fn.__module__.split(".")
        self.feature = module[2] if len(module) > 2 and module[1] == "features" else "core"
        self.__name__ = name
        self.__qualname__ = name
        self.__doc__ = fn.__doc__
        self.__module__ = fn.__module__
        self.__wrapped__ = fn
        self._validator = _make_validator(fn)
        params = list(inspect.signature(fn).parameters.values())[1:]
        extra = [
            inspect.Parameter("session", inspect.Parameter.KEYWORD_ONLY, default=None),
            inspect.Parameter("timeout_s", inspect.Parameter.KEYWORD_ONLY, default=None),
        ]
        self.__signature__ = inspect.Signature([*params, *extra])

    def __repr__(self) -> str:
        return f"<operation {self.feature}.{self.name}>"

    async def __call__(
        self, *args: Any, session: Session | None = None, timeout_s: float | None = None, **kwargs: Any
    ) -> Result[T]:
        self._validate(args, kwargs)
        parent = _current.get()
        if session is None and parent is not None:
            session = parent.context.session
        if session is None:
            from seoprobe.core.session import Session

            async with Session() as temporary:
                return await self._run(temporary, parent, args, kwargs, timeout_s)
        return await self._run(session, parent, args, kwargs, timeout_s)

    # ---------------------------------------------------------------- stages

    def _validate(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if self._validator is not None:
            self._validator(*args, **kwargs)  # ValidationError (a ValueError) = programming error

    def _deadline(self, parent: _Frame | None, timeout_s: float | None) -> float:
        loop = asyncio.get_running_loop()
        own = loop.time() + (timeout_s if timeout_s is not None else self.timeout_s)
        if parent is not None and parent.deadline is not None:
            return min(own, parent.deadline)
        return own

    async def _run(
        self,
        session: Session,
        parent: _Frame | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout_s: float | None,
    ) -> Result[T]:
        trace_id = parent.context.trace_id if parent is not None else uuid.uuid4().hex
        ctx = Context(session, self, trace_id)
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        await self._event(ctx, "started")
        outcome: Outcome[T] | None = None
        error: ErrorInfo | None = None
        exc_kept: BaseException | None = None
        with recording(session.config.max_total_bytes) as rec:
            missing = missing_requirements(self.requires, session, oauth=False)
            if missing:
                first = missing[0]
                code = (
                    ErrorCode.MISSING_EXTRA
                    if first.startswith("extra:")
                    else ErrorCode.BROWSER_UNAVAILABLE
                    if first.startswith("config:")
                    else ErrorCode.MISSING_CREDENTIAL
                )
                error = self._error(ctx, code, {"missing": ", ".join(missing)})
            else:
                deadline = self._deadline(parent, timeout_s)
                token = _current.set(_Frame(ctx, deadline))
                try:
                    async with asyncio.timeout_at(deadline):
                        outcome = await self.fn(ctx, *args, **kwargs)
                except OperationError as exc:
                    error = self._error(ctx, exc.code, exc.details)
                except TimeoutError:
                    error = self._error(ctx, ErrorCode.TIMEOUT, {"operation": self.name})
                except ImportError as exc:
                    ctx.log.debug("missing optional dependency: %s", exc)
                    error = self._error(ctx, ErrorCode.MISSING_EXTRA, {"missing": exc.name or ""})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # an unexpected bug becomes internal-error
                    ctx.log.exception("operation %s failed", self.name)
                    exc_kept = exc
                    error = self._error(ctx, ErrorCode.INTERNAL_ERROR, {"exception": type(exc).__name__})
                finally:
                    _current.reset(token)
        result = self._result(ctx, outcome, error, started_at, t0, rec.requests)
        result._exception = exc_kept
        await self._event(ctx, "finished", result=result, size_bytes=rec.size_bytes)
        return result

    def _error(self, ctx: Context, code: ErrorCode, details: dict[str, str]) -> ErrorInfo:
        return ErrorInfo(
            code=code,
            message=catalog.t(ctx.lang, f"error.{code.value}"),
            retryable=code in RETRYABLE,
            details={k: str(v) for k, v in details.items()},
        )

    def _result(
        self,
        ctx: Context,
        outcome: Outcome[T] | None,
        error: ErrorInfo | None,
        started_at: datetime,
        t0: float,
        requests: list[Any],
    ) -> Result[T]:
        meta = Meta(
            trace_id=ctx.trace_id,
            schema_version=f"{self.name}/{self.schema_version}",
            package_version=package_version(),
            rule_version=catalog.RULE_VERSION,
            started_at=started_at,
            duration_ms=int((time.monotonic() - t0) * 1000),
            requests=requests,
        )
        if error is not None or outcome is None:
            err = error or self._error(ctx, ErrorCode.INTERNAL_ERROR, {"exception": "NoOutcome"})
            return Result(operation=self.name, feature=self.feature, status=Status.ERROR, error=err, meta=meta)
        status = Status.PARTIAL if (outcome.partial or outcome.not_checked) else Status.OK
        return Result(
            operation=self.name,
            feature=self.feature,
            status=status,
            data=outcome.data,
            findings=[catalog.localize(f, ctx.lang) for f in outcome.findings],
            not_checked=outcome.not_checked,
            meta=meta,
        )

    async def _event(self, ctx: Context, kind: str, *, result: Result[Any] | None = None, size_bytes: int = 0) -> None:
        if result is None:
            ctx.log.debug("%s started", self.name)
            await ctx.session.emit(Event(kind=kind, operation=self.name, trace_id=ctx.trace_id, at=datetime.now(UTC)))
            return
        ctx.log.debug(
            "%s finished: %s in %d ms, %d requests",
            self.name,
            result.status,
            result.meta.duration_ms,
            len(result.meta.requests),
        )
        await ctx.session.emit(
            Event(
                kind=kind,
                operation=self.name,
                trace_id=ctx.trace_id,
                at=datetime.now(UTC),
                status=result.status,
                duration_ms=result.meta.duration_ms,
                requests=len(result.meta.requests),
                size_bytes=size_bytes,
            )
        )


def _make_validator(fn: Callable[..., Any]) -> Callable[..., Any] | None:
    """A validator for the caller-supplied arguments (everything after ``ctx``)."""
    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001 - unresolvable annotations: skip validation, never block
        return None
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]

    def stub(*args: Any, **kwargs: Any) -> None:
        return None

    stub.__signature__ = sig.replace(parameters=params, return_annotation=None)  # type: ignore[attr-defined]
    stub.__annotations__ = {p.name: hints[p.name] for p in params if p.name in hints}
    validated: Callable[..., Any] = validate_call(config=ConfigDict(arbitrary_types_allowed=True))(stub)
    return validated


def operation(
    name: str,
    *,
    output: type[Any],
    requires: Requirements | None = None,
    timeout_s: float = 60.0,
    schema_version: int = 1,
) -> Callable[[Callable[..., Awaitable[Outcome[T]]]], Operation[T]]:
    def wrap(fn: Callable[..., Awaitable[Outcome[T]]]) -> Operation[T]:
        return Operation(
            fn,
            name=name,
            output=output,
            requires=requires or Requirements(),
            timeout_s=timeout_s,
            schema_version=schema_version,
        )

    return wrap


AccessCheck = Callable[["Session"], Awaitable[tuple[bool, str]]]


@dataclass
class Feature:
    """A feature package: its operations, its declared dependencies and its catalog."""

    name: str
    package: str
    operations: list[Operation[Any]]
    depends_on: list[str] = field(default_factory=list)
    access_checks: dict[str, AccessCheck] = field(default_factory=dict)
    catalog: Catalog = field(init=False)

    def __post_init__(self) -> None:
        self.catalog = Catalog(self.package, feature=self.name)


__all__ = [
    "Context",
    "Feature",
    "Operation",
    "Outcome",
    "TIME_BUDGET",
    "ValidationError",
    "missing_requirements",
    "operation",
]
