"""Tests for climate.weather.tracker — the tracker service entry point.

Thin wiring: this module owns none of the decisions it tests. It only pins
that the pieces are wired together in the right order (config -> providers
-> validate -> eager secret check -> lease -> scheduler), that a startup
failure never issues a provider request, that a heartbeat is written on
start and after every tick, and that secrets never reach the log.

No socket is opened anywhere here: the fetch, store and lease collection are
all fakes; ``tests/conftest.py`` also blocks any real one.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from climate.cli._errors import EXIT_ENV_ERROR, EXIT_SUCCESS
from climate.weather import config as weather_config
from climate.weather import tracker
from climate.weather.http import FetchResult
from climate.weather.store import InMemoryWeatherStore
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT, fake_secret
from tests.weather.test_mongo import FakeCollection

FAKE_OPENWEATHER_KEY = fake_secret("openweather-key")

# Every provider except one keyless adapter is disabled in these fixtures so
# a test only has to reason about a single adapter's requests.
_TARGET_PROVIDER = "open-meteo"
_OTHER_PROVIDERS = ("met-no", "openweather", "ims", "metar", "ims-forecast")


def _write_config(path, *, enabled: str = _TARGET_PROVIDER) -> None:
    data: dict[str, Any] = {
        "locations": {
            NEUTRAL_LABEL: {"latitude": NEUTRAL_POINT[0], "longitude": NEUTRAL_POINT[1]},
        },
        "providers": {
            provider_id: {"enabled": provider_id == enabled}
            for provider_id in (_TARGET_PROVIDER, *_OTHER_PROVIDERS)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@dataclass
class FakeFetch:
    """A minimal ``climate.weather.http.fetch``-shaped stand-in."""

    calls: list[str] = field(default_factory=list)
    status: int = 200
    body: bytes = b"{}"

    def __call__(self, url: str, *, timeout: float = 10.0, headers=None, if_modified_since=None):
        self.calls.append(url)
        return FetchResult(url=url, status=self.status, headers={}, body=self.body)


def _counting_store(store: InMemoryWeatherStore) -> dict:
    """Instrument ``store.save_heartbeat`` with a call counter.

    A provider's own due-interval logic means a *fetch* only happens once
    within a fast-running test (the base tick is minutes long); the
    heartbeat, by contrast, is written on start and on literally every
    tick regardless of whether any provider had something to fetch, so it
    is the reliable signal a test can count ticks by.
    """
    counter = {"n": 0}
    original = store.save_heartbeat

    def _save_heartbeat(version: str, at: Any, providers: Any = None) -> None:
        counter["n"] += 1
        original(version, at, providers)

    store.save_heartbeat = _save_heartbeat  # type: ignore[method-assign]
    return counter


class StopAfterTicks:
    """A scheduler ``stop`` stand-in that reports set after ``n`` ticks ran.

    Counts heartbeats (see :func:`_counting_store`) from whatever the
    counter reads the first time ``is_set`` is polled — which, by
    construction, is after ``main()``'s own startup heartbeat and before
    the scheduler's first tick — so ``n`` counts ticks, not the startup
    heartbeat itself. Never calls the real ``sleep``/``wait``: the loop
    only needs to be told when to stop, not to actually pace itself in a
    test.
    """

    def __init__(self, counter: dict, n: int) -> None:
        self._counter = counter
        self._n = n
        self._baseline: int | None = None

    def is_set(self) -> bool:
        if self._baseline is None:
            self._baseline = self._counter["n"]
        return (self._counter["n"] - self._baseline) >= self._n

    def wait(self, timeout: float) -> bool:
        return self.is_set()


class HeartbeatFailingStore(InMemoryWeatherStore):
    """A store whose very first heartbeat write fails.

    Stands in for a Mongo that accepted the lease document and then became
    unwritable: the tracker must still hand the lease back.
    """

    def save_heartbeat(self, version: str, at: Any, providers: Any = None) -> None:
        raise RuntimeError("the store rejected the heartbeat")


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path, monkeypatch):
    """Point the tracker's config loader at a private path for every test."""
    monkeypatch.setenv(weather_config.CONFIG_PATH_ENV_VAR, str(tmp_path / "weather.json"))


def test_no_location_configured_exits_2_with_a_hint_and_makes_no_request(tmp_path, capsys):
    # The config file at the isolated path is never written: no locations.
    fetch = FakeFetch()

    code = tracker.main(fetch=fetch)

    assert code == EXIT_ENV_ERROR
    assert not fetch.calls
    err = capsys.readouterr().err
    assert "hint:" in err


def test_lease_already_held_exits_2_and_makes_no_request(tmp_path):
    _write_config(tmp_path / "weather.json")
    fetch = FakeFetch()
    collection = FakeCollection()
    # Someone else holds the lease, far from expiry.
    from datetime import UTC, datetime

    from climate.weather import mongo

    mongo.acquire_lease(collection, "someone-else", 10_000, now=datetime.now(UTC))

    code = tracker.main(
        store_factory=InMemoryWeatherStore,
        lease_collection_factory=lambda: collection,
        fetch=fetch,
    )

    assert code == EXIT_ENV_ERROR
    assert not fetch.calls


def test_disabled_providers_are_logged_not_fatal_when_one_is_available(tmp_path, caplog):
    _write_config(tmp_path / "weather.json")
    store = InMemoryWeatherStore()
    counter = _counting_store(store)
    fetch = FakeFetch()
    collection = FakeCollection()

    with caplog.at_level(logging.INFO, logger="climate.weather.tracker"):
        code = tracker.main(
            store_factory=lambda: store,
            lease_collection_factory=lambda: collection,
            fetch=fetch,
            stop=StopAfterTicks(counter, 1),
        )

    assert code == EXIT_SUCCESS
    disabled_lines = [r.message for r in caplog.records if "disabled" in r.message]
    assert any("openweather" in line for line in disabled_lines)
    assert any("ims" in line and "ims-forecast" not in line for line in disabled_lines)


def test_runs_a_tick_stores_a_heartbeat_on_start_and_after_every_tick(tmp_path):
    _write_config(tmp_path / "weather.json")
    store = InMemoryWeatherStore()
    counter = _counting_store(store)
    fetch = FakeFetch()
    collection = FakeCollection()

    assert store.latest_heartbeat() is None

    code = tracker.main(
        store_factory=lambda: store,
        lease_collection_factory=lambda: collection,
        fetch=fetch,
        stop=StopAfterTicks(counter, 2),
    )

    assert code == EXIT_SUCCESS
    assert fetch.calls  # a real provider request was issued this run
    heartbeat = store.latest_heartbeat()
    assert heartbeat is not None
    import climate

    assert heartbeat["version"] == climate.__version__
    # The startup heartbeat plus (at least) the two ticks that stopped the run.
    assert counter["n"] >= 3


def _recording_store(store: InMemoryWeatherStore) -> tuple[dict, list]:
    """``(tick counter, [providers snapshot per heartbeat write])``."""
    counter = _counting_store(store)
    counting = store.save_heartbeat
    writes: list = []

    def _save_heartbeat(version: str, at: Any, providers: Any = None) -> None:
        writes.append(providers)
        counting(version, at, providers)

    store.save_heartbeat = _save_heartbeat  # type: ignore[method-assign]
    return counter, writes


def test_every_heartbeat_publishes_the_provider_availability_snapshot(tmp_path):
    """The tracker is the only process that sees the credentials, so the
    availability it computes is written on start and on every tick — the web
    API and doctor have nothing else honest to read."""
    _write_config(tmp_path / "weather.json")
    store = InMemoryWeatherStore()
    counter, writes = _recording_store(store)

    code = tracker.main(
        store_factory=lambda: store,
        lease_collection_factory=lambda: FakeCollection(),
        fetch=FakeFetch(),
        stop=StopAfterTicks(counter, 2),
    )

    assert code == EXIT_SUCCESS
    # the startup write plus one per tick, each carrying the snapshot
    assert len(writes) >= 3
    assert all(snapshot and _TARGET_PROVIDER in snapshot for snapshot in writes)

    snapshot = store.latest_heartbeat()["providers"]
    assert snapshot[_TARGET_PROVIDER]["enabled"] is True
    assert snapshot[_TARGET_PROVIDER]["credential_present"] is None  # keyless provider
    assert snapshot["openweather"] == {
        "enabled": False,
        "reason": "disabled in configuration",
        "credential_present": False,
    }


def test_the_heartbeat_snapshot_never_carries_the_credential_value(tmp_path, monkeypatch):
    """``credential_present`` is a boolean: never the key, never its length."""
    monkeypatch.setenv("CLIMATE_OPENWEATHER_API_KEY", FAKE_OPENWEATHER_KEY)
    _write_config(tmp_path / "weather.json", enabled="openweather")
    store = InMemoryWeatherStore()
    counter, _ = _recording_store(store)

    code = tracker.main(
        store_factory=lambda: store,
        lease_collection_factory=lambda: FakeCollection(),
        fetch=FakeFetch(),
        stop=StopAfterTicks(counter, 1),
    )

    assert code == EXIT_SUCCESS
    heartbeat = store.latest_heartbeat()
    assert heartbeat["providers"]["openweather"] == {
        "enabled": True,
        "reason": None,
        "credential_present": True,
    }
    assert FAKE_OPENWEATHER_KEY not in json.dumps(heartbeat, default=str)


def test_lease_is_released_when_the_run_finishes(tmp_path):
    _write_config(tmp_path / "weather.json")
    store = InMemoryWeatherStore()
    counter = _counting_store(store)
    fetch = FakeFetch()
    collection = FakeCollection()

    tracker.main(
        store_factory=lambda: store,
        lease_collection_factory=lambda: collection,
        fetch=fetch,
        stop=StopAfterTicks(counter, 1),
    )

    from climate.weather.mongo import LEASE_DOC_ID

    assert LEASE_DOC_ID not in collection.documents


def test_a_failing_startup_heartbeat_releases_the_lease_and_exits_cleanly(tmp_path, capsys):
    """Regression: the lease must survive nothing — not even a dead store.

    ``main()`` acquires the lease before the scheduler exists, so a failure
    between the acquire and the scheduler's own release used to leave the
    lease document in place and lock every replacement tracker out until the
    TTL expired.
    """
    _write_config(tmp_path / "weather.json")
    fetch = FakeFetch()
    collection = FakeCollection()

    code = tracker.main(
        store_factory=HeartbeatFailingStore,
        lease_collection_factory=lambda: collection,
        fetch=fetch,
    )

    from datetime import UTC, datetime

    from climate.weather import mongo
    from climate.weather.mongo import LEASE_DOC_ID

    assert code == EXIT_ENV_ERROR
    assert not fetch.calls
    assert "hint:" in capsys.readouterr().err
    assert LEASE_DOC_ID not in collection.documents
    # A replacement tracker can take the lease straight away.
    assert mongo.acquire_lease(collection, "replacement", 10_000, now=datetime.now(UTC))


def test_log_line_never_carries_the_openweather_style_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLIMATE_OPENWEATHER_API_KEY", FAKE_OPENWEATHER_KEY)
    data = {
        "locations": {
            NEUTRAL_LABEL: {"latitude": NEUTRAL_POINT[0], "longitude": NEUTRAL_POINT[1]},
        },
        "providers": {
            provider_id: {"enabled": provider_id in ("openweather",)}
            for provider_id in (_TARGET_PROVIDER, "openweather", *_OTHER_PROVIDERS)
        },
    }
    cfg_path = tmp_path / "weather.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    store = InMemoryWeatherStore()
    counter = _counting_store(store)
    fetch = FakeFetch()
    collection = FakeCollection()

    with caplog.at_level(logging.INFO):
        code = tracker.main(
            store_factory=lambda: store,
            lease_collection_factory=lambda: collection,
            fetch=fetch,
            stop=StopAfterTicks(counter, 1),
        )

    assert code == EXIT_SUCCESS
    for record in caplog.records:
        assert FAKE_OPENWEATHER_KEY not in record.getMessage()


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM"), reason="SIGTERM is not available on this platform"
)
def test_sigterm_stops_the_run_cleanly_and_releases_the_lease(tmp_path):
    _write_config(tmp_path / "weather.json")
    store = InMemoryWeatherStore()
    fetch = FakeFetch()
    collection = FakeCollection()
    stop_event = threading.Event()

    def _send_sigterm_soon() -> None:
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=_send_sigterm_soon, daemon=True)
    sender.start()
    try:
        code = tracker.main(
            store_factory=lambda: store,
            lease_collection_factory=lambda: collection,
            fetch=fetch,
            stop=stop_event,
        )
    finally:
        sender.join(timeout=5)

    assert code == EXIT_SUCCESS
    assert stop_event.is_set()
    from climate.weather.mongo import LEASE_DOC_ID

    assert LEASE_DOC_ID not in collection.documents


def test_default_store_factory_asks_mongo_to_ensure_indexes(tmp_path, monkeypatch, capsys):
    """No ``store_factory`` injected: the tracker owns index creation, so it
    must call ``mongo.build_store(ensure_indexes=True)`` explicitly rather
    than relying on the default."""
    _write_config(tmp_path / "weather.json")
    fetch = FakeFetch()
    collection = FakeCollection()
    calls: list = []

    def _recording_build_store(*args, **kwargs):
        calls.append((args, kwargs))
        return HeartbeatFailingStore()

    monkeypatch.setattr(tracker.mongo, "build_store", _recording_build_store)
    monkeypatch.setattr(tracker.mongo, "lease_collection", lambda *a, **k: collection)

    code = tracker.main(fetch=fetch)

    assert code == EXIT_ENV_ERROR
    assert calls == [((), {"ensure_indexes": True})]
