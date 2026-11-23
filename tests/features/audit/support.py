"""The e2e environment of the audit tests: the demo site behind a FakeHttp, no browser, no network."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.core.models import CheckOutput
from seoprobe.core.session import Session
from tests import fakes
from tests.demo_site import BASE, build

ERRORS = {
    "https://nowhere.test/": "unreachable",
    "http://nowhere.test/": "unreachable",
    "https://nxdomain.test/": "dns-failed",
}
CONFIG = Config(render=False, visibility=False, request_delay_s=0.0)


def no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS needs a real host (covered by the site feature's tests): the transport checks find nothing."""

    async def empty(url: str, page: Any, http: Any) -> CheckOutput:
        return CheckOutput()

    monkeypatch.setattr(importlib.import_module("seoprobe.features.site.transport"), "run_transport_checks", empty)


def demo_session(config: Config = CONFIG, **kwargs: Any) -> Session:
    return fakes.session(build(), errors=ERRORS, config=config, **kwargs)


__all__ = ["BASE", "CONFIG", "ERRORS", "demo_session", "no_transport"]
