"""Shared test setup: real network access is off unless a test talks to a local server."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import seoprobe  # noqa: F401  (installs every feature catalog)

FIXTURES = Path(__file__).parent / "fixtures"
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto


def _local(address: Any) -> bool:
    if isinstance(address, str):  # AF_UNIX
        return True
    host = address[0] if isinstance(address, tuple) else address
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if not _local(address):
        raise RuntimeError(f"network access is disabled in tests: {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if not _local(address):
        raise RuntimeError(f"network access is disabled in tests: {address!r}")
    return _real_connect_ex(self, address)


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> Any:
    address = args[-1]  # sendto(data, address) or sendto(data, flags, address)
    if not _local(address):
        raise RuntimeError(f"network access is disabled in tests: {address!r}")
    return _real_sendto(self, data, *args)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", _guarded_sendto)
    yield


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
