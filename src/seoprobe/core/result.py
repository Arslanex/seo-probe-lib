"""The envelope every operation returns."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, PrivateAttr

from seoprobe.core.errors import ErrorCode, ErrorInfo, SeoprobeError
from seoprobe.core.models import Finding, NotChecked

T = TypeVar("T")


class Status(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"


class RequestLog(BaseModel):
    url: str  # secrets masked
    method: str = "GET"
    status: int | None = None
    duration_ms: int = 0
    size_bytes: int = 0
    error: str | None = None


class Meta(BaseModel):
    trace_id: str
    schema_version: str
    package_version: str
    rule_version: str
    started_at: datetime
    duration_ms: int = 0
    requests: list[RequestLog] = Field(default_factory=list)


class Event(BaseModel):
    kind: str  # "started" | "finished" | "progress"
    operation: str
    trace_id: str
    at: datetime
    status: Status | None = None
    duration_ms: int | None = None
    requests: int | None = None
    size_bytes: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Result(BaseModel, Generic[T]):
    operation: str
    feature: str
    status: Status
    data: T | None = None
    findings: list[Finding] = Field(default_factory=list)
    not_checked: list[NotChecked] = Field(default_factory=list)
    error: ErrorInfo | None = None
    meta: Meta

    _exception: BaseException | None = PrivateAttr(default=None)

    @property
    def ok(self) -> bool:
        return self.status != Status.ERROR

    def unwrap(self) -> T:
        if self.status == Status.ERROR or self.data is None:
            err = self.error or ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="no data")
            raise SeoprobeError(err.code, err.message, self) from self._exception
        return self.data

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def __getstate__(self) -> dict[Any, Any]:
        """
        Pickling (process pools, task queues) drops the kept exception: it may not pickle and
        only explains a traceback in the process that raised it.
        """
        state = super().__getstate__()
        state["__pydantic_private__"] = {**(state.get("__pydantic_private__") or {}), "_exception": None}
        return state

    def to_json(self, indent: int | None = None) -> str:
        return self.model_dump_json(indent=indent)
