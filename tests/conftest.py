"""Shared pytest configuration for the climate-cli test suite.

This module installs a whole-suite guard that blocks real outbound socket
connections. The weather-tracking-service work talks to several real-world
HTTP providers; every one of those interactions must be tested against
captured fixtures (see tests/fixtures/) through an injected fake transport,
never against the live network. Blocking sockets here, once, means every
test file inherits the guard automatically instead of each task remembering
to mock its own transport.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

#: Message shown when a test (directly or indirectly) tries to open a real
#: network connection. Kept short and actionable so a failure points straight
#: at the fix: inject a fake opener/transport instead of hitting the network.
BLOCKED_CONNECT_MESSAGE = (
    "Real network access is disabled in the climate-cli test suite. "
    "Inject a fake HTTP opener/transport (see climate/weather/http.py) or "
    "use a captured fixture from tests/fixtures/ instead of connecting to "
    "a real socket."
)


class NetworkDisabledError(RuntimeError):
    """Raised when test code attempts a real network connection."""


def _blocked_connect(*_args: Any, **_kwargs: Any) -> None:
    raise NetworkDisabledError(BLOCKED_CONNECT_MESSAGE)


def _blocked_create_connection(*_args: Any, **_kwargs: Any) -> None:
    raise NetworkDisabledError(BLOCKED_CONNECT_MESSAGE)


@pytest.fixture(autouse=True)
def _block_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to open a real socket connection, with a clear message.

    Patches both the low-level ``socket.socket.connect``/``connect_ex`` and
    the higher-level ``socket.create_connection`` helper that
    ``urllib.request`` and ``http.client`` build on, so no code path in the
    suite can reach the real network regardless of which layer it uses.
    """
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
