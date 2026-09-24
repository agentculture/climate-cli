"""Tests for climate.weather.mongo: the URI guard, lazy pymongo import,
the Mongo-backed store (against a hand-written fake collection, never a
live server and never mongomock), and the tracker lease.
"""

from __future__ import annotations

import copy
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import pytest

import climate.weather.mongo as weather_mongo
from climate.cli._errors import EXIT_ENV_ERROR, CliError
from climate.weather.mongo import (
    DEFAULT_MONGO_URI,
    LEASE_DOC_ID,
    WEATHER_MONGO_URI_ENV,
    MongoWeatherStore,
    acquire_lease,
    connect,
    release_lease,
    renew_lease,
    resolve_uri,
)
from climate.weather.store import Measurement
from tests.weather.neutral import fake_secret
from tests.weather.store_contract import StoreContractTests, make_fetch, make_reading

# --- a hand-written fake of the pymongo collection API -----------------------
#
# Only the methods MongoWeatherStore and the lease functions actually call:
# insert_one, find_one, find (+ cursor .sort()/.limit()), count_documents,
# delete_many, delete_one, update_one, create_index. No mongomock, no
# network, no docker.


class DuplicateKeyError(Exception):
    """Stand-in for pymongo.errors.DuplicateKeyError (code 11000)."""

    def __init__(self, message: str = "E11000 duplicate key error") -> None:
        super().__init__(message)
        self.code = 11000


class _UpdateResult:
    def __init__(self, matched_count: int, upserted_id: Any = None) -> None:
        self.matched_count = matched_count
        self.upserted_id = upserted_id


class _DeleteResult:
    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


def _get_path(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _path_exists(document: dict[str, Any], path: str) -> bool:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, condition in query.items():
        if key == "$or":
            if not any(_matches(document, sub) for sub in condition):
                return False
            continue
        if isinstance(condition, dict) and any(str(k).startswith("$") for k in condition):
            for op, operand in condition.items():
                if op == "$exists":
                    if _path_exists(document, key) != operand:
                        return False
                elif op == "$ne":
                    if _get_path(document, key) == operand:
                        return False
                elif op == "$in":
                    if _get_path(document, key) not in operand:
                        return False
                elif op == "$gte":
                    value = _get_path(document, key)
                    if value is None or not (value >= operand):
                        return False
                elif op == "$lte":
                    value = _get_path(document, key)
                    if value is None or not (value <= operand):
                        return False
                else:  # pragma: no cover - defensive, not exercised by this suite
                    raise NotImplementedError(f"unsupported operator {op!r}")
        else:
            if _get_path(document, key) != condition:
                return False
    return True


class _FakeCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    def sort(self, key: str | list[tuple[str, int]], direction: int | None = None) -> "_FakeCursor":
        keys = [(key, direction)] if isinstance(key, str) else list(key)
        # Stable multi-key sort respecting each key's own direction: sort by
        # the least-significant key first, most-significant last, reversing
        # only the keys whose direction is descending (mirrors Mongo's
        # compound-sort semantics without needing a composite sort key that
        # would otherwise have to fight mixed asc/desc directions).
        documents = list(self._documents)
        for field, direction_ in reversed(keys):
            documents.sort(key=lambda document, field=field: _get_path(document, field))
            if direction_ == -1:
                documents.reverse()
        self._documents = documents
        return self

    def limit(self, count: int) -> "_FakeCursor":
        self._documents = self._documents[:count]
        return self

    def __iter__(self) -> Iterable[dict[str, Any]]:
        return iter(self._documents)


class FakeCollection:
    """A hand-written fake of the pymongo Collection API, in-memory only."""

    def __init__(self) -> None:
        self.documents: dict[Any, dict[str, Any]] = {}
        self.created_indexes: list[Any] = []

    def create_index(self, spec: Any, **_kwargs: Any) -> str:
        self.created_indexes.append(spec)
        return "index"

    def insert_one(self, document: dict[str, Any]) -> Any:
        document = copy.deepcopy(document)
        key = document["_id"]
        if key in self.documents:
            raise DuplicateKeyError()
        self.documents[key] = document
        return document

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for document in self.documents.values():
            if _matches(document, query):
                return copy.deepcopy(document)
        return None

    def find(self, query: dict[str, Any] | None = None) -> _FakeCursor:
        query = query or {}
        matched = [
            copy.deepcopy(document)
            for document in self.documents.values()
            if _matches(document, query)
        ]
        return _FakeCursor(matched)

    def count_documents(self, query: dict[str, Any] | None = None) -> int:
        query = query or {}
        return sum(1 for document in self.documents.values() if _matches(document, query))

    def delete_many(self, query: dict[str, Any]) -> _DeleteResult:
        keys = [key for key, document in self.documents.items() if _matches(document, query)]
        for key in keys:
            del self.documents[key]
        return _DeleteResult(len(keys))

    def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for key, document in self.documents.items():
            if _matches(document, query):
                del self.documents[key]
                return _DeleteResult(1)
        return _DeleteResult(0)

    def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _UpdateResult:
        set_fields = update.get("$set", {})
        for key, document in self.documents.items():
            if _matches(document, query):
                document.update(set_fields)
                return _UpdateResult(matched_count=1)
        if not upsert:
            return _UpdateResult(matched_count=0)
        new_id = query.get("_id")
        if new_id in self.documents:
            # A document with this _id exists but did not match the query
            # (e.g. it is held by someone else) - a real unique-index
            # upsert conflict, exactly as pymongo would report it.
            raise DuplicateKeyError()
        new_document = {
            key: value for key, value in query.items() if not key.startswith("$") and key != "_id"
        }
        new_document["_id"] = new_id
        new_document.update(set_fields)
        self.documents[new_id] = new_document
        return _UpdateResult(matched_count=0, upserted_id=new_id)


# --- lazy import / URI guard --------------------------------------------------


def test_pymongo_is_not_installed_in_this_environment() -> None:
    """Sanity check: the environment this suite runs in has no pymongo.

    If this ever fails, the other tests in this module (which rely on
    pymongo being absent to exercise the lazy-import failure path and the
    plain-bytes body path) need to be revisited.
    """
    with pytest.raises(ImportError):
        import pymongo  # noqa: F401


def test_module_imports_without_pymongo() -> None:
    """Importing (or re-importing) the module must succeed without pymongo."""
    import importlib

    importlib.reload(weather_mongo)


def test_connect_without_pymongo_raises_cli_error_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEATHER_MONGO_URI_ENV, raising=False)
    with pytest.raises(CliError) as excinfo:
        connect()
    err = excinfo.value
    assert err.code == EXIT_ENV_ERROR
    assert err.remediation == 'pip install "climate-cli[weather]"'
    assert "Traceback" not in str(err)


def test_default_uri_is_the_weather_mongodb_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEATHER_MONGO_URI_ENV, raising=False)
    assert resolve_uri() == DEFAULT_MONGO_URI
    assert resolve_uri() == "mongodb://weather-mongodb:27017/weather"


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEATHER_MONGO_URI_ENV, "mongodb://weather-mongodb:27020/weather")
    assert resolve_uri() == "mongodb://weather-mongodb:27020/weather"


def test_explicit_uri_argument_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEATHER_MONGO_URI_ENV, "mongodb://weather-mongodb:27020/weather")
    assert resolve_uri("mongodb://weather-mongodb:27099/weather") == (
        "mongodb://weather-mongodb:27099/weather"
    )


@pytest.mark.parametrize("port", [27017, 27018, 27019])
@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"]
)
def test_rejects_loopback_and_host_addresses_on_forbidden_ports(host: str, port: int) -> None:
    with pytest.raises(CliError) as excinfo:
        resolve_uri(f"mongodb://{host}:{port}/weather")
    assert excinfo.value.code == EXIT_ENV_ERROR


def test_rejects_omitted_port_defaulting_to_27017_on_loopback() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve_uri("mongodb://localhost/weather")
    assert excinfo.value.code == EXIT_ENV_ERROR


def test_allows_dedicated_hostname_on_default_port() -> None:
    # weather-mongodb is not a loopback/host address, so port 27017 is fine.
    assert resolve_uri("mongodb://weather-mongodb:27017/weather") == (
        "mongodb://weather-mongodb:27017/weather"
    )


def test_rejects_srv_uri() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve_uri("mongodb+srv://weather-mongodb.example.net/weather")
    assert excinfo.value.code == EXIT_ENV_ERROR


def test_rejects_srv_uri_even_on_a_safe_looking_host() -> None:
    with pytest.raises(CliError):
        resolve_uri("mongodb+srv://weather-mongodb/weather")


# --- store contract, run against the fake collection --------------------------


class TestMongoWeatherStore(StoreContractTests):
    """MongoWeatherStore must satisfy the whole store contract, via the fake."""

    def make_store(self) -> MongoWeatherStore:
        return MongoWeatherStore(FakeCollection(), FakeCollection())


def test_indexes_created_for_provider_location_requested_at_desc() -> None:
    fetches = FakeCollection()
    readings = FakeCollection()
    MongoWeatherStore(fetches, readings)
    assert [("provider", 1), ("location", 1), ("requested_at", -1)] in fetches.created_indexes


def test_readings_compound_index_pins_the_exact_esr_spec() -> None:
    fetches = FakeCollection()
    readings = FakeCollection()
    MongoWeatherStore(fetches, readings)
    assert readings.created_indexes == [
        [("fetch_id", 1)],
        [("location", 1), ("provider", 1), ("kind", 1), ("observed_at", 1), ("requested_at", 1)],
    ]


def test_ensure_indexes_false_issues_no_create_index_calls() -> None:
    fetches = FakeCollection()
    readings = FakeCollection()
    MongoWeatherStore(fetches, readings, ensure_indexes=False)
    assert fetches.created_indexes == []
    assert readings.created_indexes == []


def test_build_store_forwards_ensure_indexes_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_store must pass ensure_indexes through to MongoWeatherStore."""
    fetches = FakeCollection()
    readings = FakeCollection()
    fake_db = {
        weather_mongo.FETCHES_COLLECTION: fetches,
        weather_mongo.READINGS_COLLECTION: readings,
        weather_mongo.META_COLLECTION: FakeCollection(),
    }
    monkeypatch.setattr(weather_mongo, "connect", lambda uri=None: object())
    monkeypatch.setattr(weather_mongo, "database", lambda client: fake_db)

    weather_mongo.build_store(ensure_indexes=False)

    assert fetches.created_indexes == []
    assert readings.created_indexes == []


def test_build_store_defaults_to_ensure_indexes_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches = FakeCollection()
    readings = FakeCollection()
    fake_db = {
        weather_mongo.FETCHES_COLLECTION: fetches,
        weather_mongo.READINGS_COLLECTION: readings,
        weather_mongo.META_COLLECTION: FakeCollection(),
    }
    monkeypatch.setattr(weather_mongo, "connect", lambda uri=None: object())
    monkeypatch.setattr(weather_mongo, "database", lambda client: fake_db)

    weather_mongo.build_store()

    assert [
        ("location", 1),
        ("provider", 1),
        ("kind", 1),
        ("observed_at", 1),
        ("requested_at", 1),
    ] in (readings.created_indexes)


def test_body_stored_as_plain_bytes_with_the_fake_collection() -> None:
    fetches = FakeCollection()
    store = MongoWeatherStore(fetches, FakeCollection())
    fetch_id = store.save_fetch(make_fetch())
    stored_document = fetches.documents[fetch_id]
    assert type(stored_document["body"]) is bytes


def test_body_wrapped_as_bson_binary_when_bson_is_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the lazy bson.Binary path without a real pymongo install.

    ``pymongo``/``bson`` are not installed in this environment, so this
    injects a minimal fake ``bson`` module into ``sys.modules`` for the
    duration of the test - enough for ``from bson import Binary`` to
    succeed inside :func:`climate.weather.mongo._wrap_body` - and checks
    the store actually wraps with it rather than silently storing plain
    bytes whenever the module happens to be importable.
    """

    class FakeBinary(bytes):
        pass

    fake_bson = type(sys)("bson")
    fake_bson.Binary = FakeBinary
    monkeypatch.setitem(sys.modules, "bson", fake_bson)

    fetches = FakeCollection()
    store = MongoWeatherStore(fetches, FakeCollection())
    fetch_id = store.save_fetch(make_fetch())
    stored_document = fetches.documents[fetch_id]
    assert isinstance(stored_document["body"], FakeBinary)


def test_heartbeat_round_trips_the_provider_availability_snapshot() -> None:
    """The tracker's snapshot is what the web API and doctor read back."""
    meta = FakeCollection()
    store = MongoWeatherStore(FakeCollection(), FakeCollection(), meta=meta)
    store.save_heartbeat(
        "9.9.9",
        datetime(2026, 9, 17, 9, 0, 0, tzinfo=UTC),
        {"openweather": {"enabled": True, "reason": None, "credential_present": True}},
    )
    heartbeat = store.latest_heartbeat()
    assert heartbeat is not None
    assert heartbeat["providers"] == {
        "openweather": {"enabled": True, "reason": None, "credential_present": True}
    }


def test_a_heartbeat_document_without_providers_still_loads() -> None:
    """Backward compatible: a heartbeat written before the snapshot existed."""
    meta = FakeCollection()
    meta.insert_one(
        {
            "_id": weather_mongo.HEARTBEAT_DOC_ID,
            "version": "0.1.0",
            "at": datetime(2026, 9, 17, 9, 0, 0, tzinfo=UTC),
        }
    )
    store = MongoWeatherStore(FakeCollection(), FakeCollection(), meta=meta)
    heartbeat = store.latest_heartbeat()
    assert heartbeat is not None
    assert heartbeat["version"] == "0.1.0"
    assert heartbeat["providers"] is None


def test_a_heartbeat_document_never_carries_the_credential_value() -> None:
    """Only the three documented keys are stored, whatever a caller passes."""
    meta = FakeCollection()
    store = MongoWeatherStore(FakeCollection(), FakeCollection(), meta=meta)
    secret = fake_secret("openweather-key")
    store.save_heartbeat(
        "9.9.9",
        datetime(2026, 9, 17, 9, 0, 0, tzinfo=UTC),
        {
            "openweather": {
                "enabled": True,
                "reason": None,
                "credential_present": True,
                "credential": secret,  # a caller must not be able to smuggle this in
            }
        },
    )
    stored = meta.documents[weather_mongo.HEARTBEAT_DOC_ID]
    assert secret not in str(stored)
    assert set(stored["providers"]["openweather"]) == {"enabled", "reason", "credential_present"}


def test_requested_at_truncated_to_milliseconds_on_save() -> None:
    store = MongoWeatherStore(FakeCollection(), FakeCollection())
    sub_ms = datetime(2026, 9, 17, 9, 0, 0, 123456, tzinfo=UTC)
    fetch_id = store.save_fetch(make_fetch(requested_at=sub_ms))
    stored = store.get_fetch(fetch_id)
    assert stored is not None
    assert stored.requested_at.microsecond == 123000


def test_reading_times_truncated_to_milliseconds_on_save() -> None:
    store = MongoWeatherStore(FakeCollection(), FakeCollection())
    fetch_id = store.save_fetch(make_fetch())
    sub_ms = datetime(2026, 9, 17, 9, 0, 0, 987654, tzinfo=UTC)
    store.save_readings(fetch_id, [make_reading(observed_at=sub_ms, requested_at=sub_ms)])
    (reading,) = store.readings_for_fetch(fetch_id)
    assert reading.observed_at.microsecond == 987000
    assert reading.requested_at.microsecond == 987000


def test_model_run_at_truncated_to_milliseconds_on_save() -> None:
    store = MongoWeatherStore(FakeCollection(), FakeCollection())
    fetch_id = store.save_fetch(make_fetch())
    sub_ms = datetime(2026, 9, 17, 6, 0, 0, 654321, tzinfo=UTC)
    store.save_readings(fetch_id, [make_reading(kind="forecast", model_run_at=sub_ms)])
    (reading,) = store.readings_for_fetch(fetch_id)
    assert reading.model_run_at is not None
    assert reading.model_run_at.microsecond == 654000


def test_a_reading_document_without_model_run_at_still_loads() -> None:
    """Documents written before the field existed must keep loading."""
    readings = FakeCollection()
    store = MongoWeatherStore(FakeCollection(), readings)
    fetch_id = store.save_fetch(make_fetch())
    store.save_readings(fetch_id, [make_reading()])
    for document in readings.documents.values():
        del document["model_run_at"]
    (reading,) = store.readings_for_fetch(fetch_id)
    assert reading.model_run_at is None


class _FlakyInsertCollection(FakeCollection):
    """A collection whose ``insert_one`` starts failing at a chosen call.

    Stands in for a Mongo write failure part-way through persisting a
    replacement set.
    """

    def __init__(self) -> None:
        super().__init__()
        self.inserts = 0
        self.fail_from_insert: int | None = None

    def insert_one(self, document: dict[str, Any]) -> Any:
        self.inserts += 1
        if self.fail_from_insert is not None and self.inserts >= self.fail_from_insert:
            raise RuntimeError("mongo write failed")
        return super().insert_one(document)


def test_a_failed_insert_during_replace_leaves_the_previous_readings_intact() -> None:
    """The swap inserts before deleting, so a write failure loses nothing.

    The replacement's first document is written and its second fails; the
    previously stored readings must still be there afterwards, and the
    half-written replacement must not be.
    """
    readings = _FlakyInsertCollection()
    store = MongoWeatherStore(FakeCollection(), readings)
    fetch_id = store.save_fetch(make_fetch())
    (kept_id,) = store.save_readings(fetch_id, [make_reading()])

    replacements = [
        make_reading(values={"temperature": Measurement(27.5, "degC")}),
        make_reading(values={"temperature": Measurement(27.6, "degC")}),
    ]
    readings.fail_from_insert = readings.inserts + 2  # second replacement insert
    with pytest.raises(RuntimeError):
        store.replace_readings(fetch_id, replacements)

    survivors = store.readings_for_fetch(fetch_id)
    assert [reading.id for reading in survivors] == [kept_id]
    assert survivors[0].values["temperature"].value == 27.4


# --- tracker lease -------------------------------------------------------------


def test_acquire_lease_succeeds_when_free() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert acquire_lease(leases, "agent-a", 30, now=now) is True
    document = leases.documents[LEASE_DOC_ID]
    assert document["holder"] == "agent-a"
    assert document["expires_at"] == now + timedelta(seconds=30)


def test_second_holder_cannot_acquire_a_live_lease() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert acquire_lease(leases, "agent-a", 30, now=now) is True
    assert acquire_lease(leases, "agent-b", 30, now=now) is False
    assert leases.documents[LEASE_DOC_ID]["holder"] == "agent-a"


def test_holder_can_reacquire_extending_its_own_lease() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert acquire_lease(leases, "agent-a", 30, now=now) is True
    later = now + timedelta(seconds=10)
    assert acquire_lease(leases, "agent-a", 30, now=later) is True
    assert leases.documents[LEASE_DOC_ID]["expires_at"] == later + timedelta(seconds=30)


def test_a_new_holder_can_acquire_after_expiry() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert acquire_lease(leases, "agent-a", 30, now=now) is True
    expired = now + timedelta(seconds=31)
    assert acquire_lease(leases, "agent-b", 30, now=expired) is True
    assert leases.documents[LEASE_DOC_ID]["holder"] == "agent-b"


def test_renew_lease_extends_only_for_the_current_holder() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    acquire_lease(leases, "agent-a", 30, now=now)
    later = now + timedelta(seconds=5)
    assert renew_lease(leases, "agent-a", 60, now=later) is True
    assert leases.documents[LEASE_DOC_ID]["expires_at"] == later + timedelta(seconds=60)
    assert renew_lease(leases, "agent-b", 60, now=later) is False


def test_release_lease_only_for_the_current_holder_then_frees_it() -> None:
    leases = FakeCollection()
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    acquire_lease(leases, "agent-a", 30, now=now)
    assert release_lease(leases, "agent-b") is False
    assert LEASE_DOC_ID in leases.documents
    assert release_lease(leases, "agent-a") is True
    assert LEASE_DOC_ID not in leases.documents
    # freed: someone else can now acquire it.
    assert acquire_lease(leases, "agent-b", 30, now=now) is True
