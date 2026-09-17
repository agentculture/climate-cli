"""MongoDB-backed :class:`climate.weather.store.WeatherStore`, connection
choke-point, and tracker lease.

This module is standard-library only at import time: ``pymongo`` (and its
bundled ``bson``) are lazy-imported *inside* the functions that actually
need a live driver, never at module scope. That keeps three things true
simultaneously:

* this module (and anything that imports it) works in an environment where
  ``pymongo`` is not installed — including this repository's own test
  suite, which never installs it (see ``tests/weather/test_mongo.py``);
* a caller that never actually opens a connection never pays for or needs
  the dependency;
* the one place that *does* need ``pymongo`` — :func:`connect` — turns a
  missing/incompatible install into a clean, actionable
  :class:`climate.cli._errors.CliError` (exit code 2) instead of a raw
  ``ImportError`` traceback.

:func:`connect` is the single choke point for reading
``WEATHER_MONGO_URI``. It rejects ``mongodb+srv://`` URIs (they resolve
their real host/port via a DNS SRV lookup the URI text never carries, so
the guard below cannot see through them) and rejects a *loopback* address
(``localhost``, ``127.0.0.1``, ``::1``, ``0.0.0.0``) or the Docker
host-gateway alias (``host.docker.internal``) on one of the three MongoDB
ports other services on this machine already own — 27017, 27018, 27019.
The service's own default URI, ``mongodb://weather-mongodb:27017/weather``,
names the dedicated ``weather-mongodb`` container by its Docker Compose
service name rather than a loopback/host address, so it is unaffected by
the guard even though it uses port 27017: the point of the guard is to
catch a URI that has been pointed at *somebody else's* Mongo reachable via
the loopback/host interface, not to reserve the port number itself.

Modelled on ``jlab.mongo``'s legacy-port guard (read, not imported, per the
task brief) — see ``jetson-ai-lab-cli/jlab/mongo.py`` — but adapted: jlab
forbids two fixed ports outright everywhere; this module only forbids the
loopback/host-gateway *addresses* on the three ports, since
``weather-mongodb`` legitimately uses the default Mongo port on its own
hostname.

:class:`MongoWeatherStore` stores each fetch record's raw ``body`` as BSON
binary. The wrapping happens through a lazy ``from bson import Binary``
inside :func:`_wrap_body` — the same lazy-import discipline as
:func:`connect` — so the same store class works unmodified against the
hand-written fake collection the test suite injects (which never has
``bson`` available, and so stores plain ``bytes``) and against a real
``pymongo`` collection (which does, and gets an explicit
:class:`bson.Binary`).

BSON's ``Date`` type only carries millisecond precision; a Python
``datetime`` carries microseconds. :func:`_truncate_to_millis` truncates
(never rounds) every stored datetime to whole milliseconds *before* it is
written, so what :meth:`MongoWeatherStore.get_fetch` (etc.) returns after a
save matches what a real server would hand back after a genuine BSON
round-trip, rather than silently disagreeing only when pymongo is actually
in play.

The tracker lease (:func:`acquire_lease`, :func:`renew_lease`,
:func:`release_lease`) is a single document, keyed by :data:`LEASE_DOC_ID`,
holding ``holder`` and ``expires_at``. Acquisition is a single atomic
``update_one(..., upsert=True)`` whose filter matches either the current
holder (a renewal-shaped acquire) or an already-expired lease; when neither
holds, an upsert races into MongoDB's own unique ``_id`` index and raises a
duplicate-key error, which :func:`acquire_lease` turns into "not acquired"
rather than letting a second holder create a second, competing lease
document.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from climate.cli._errors import EXIT_ENV_ERROR, CliError
from climate.weather.store import (
    FetchRecord,
    Reading,
    SeriesPoint,
    UnknownFetchError,
    heartbeat_provider_snapshot,
)

__all__ = [
    "LEASE_DOC_ID",
    "WEATHER_MONGO_URI_ENV",
    "DEFAULT_MONGO_URI",
    "MongoWeatherStore",
    "acquire_lease",
    "connect",
    "release_lease",
    "renew_lease",
    "resolve_uri",
]

#: Env var read by :func:`connect` / :func:`resolve_uri`. No other module
#: should read this directly — go through this module.
WEATHER_MONGO_URI_ENV = "WEATHER_MONGO_URI"

#: Used when the env var is unset. Names the dedicated ``weather-mongodb``
#: Docker Compose service by hostname, not a loopback address, so the
#: legacy-port guard (which only forbids loopback/host addresses) never
#: rejects it even on the default Mongo port.
DEFAULT_MONGO_URI = "mongodb://weather-mongodb:27017/weather"

# Mongo ports other services on this machine already own: 27017 (a legacy
# instance elsewhere in this workspace), 27018 (the shared eidetic memory
# store), 27019 (jlab's own dedicated instance). None of them is this
# service's database.
_FORBIDDEN_PORTS = {27017, 27018, 27019}

# Addresses that mean "this machine" rather than a named service/container.
# A URI naming one of these on a forbidden port is almost certainly pointed
# at somebody else's Mongo reachable over the loopback/host-gateway
# interface, not at weather-mongodb's own container.
# nosec B104 - this is a set of addresses being *matched against* in a URI
# guard, not a bind/listen call; "0.0.0.0" here means "this machine", the
# thing being rejected, never something this module binds to.
_LOOPBACK_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}  # nosec B104
)

# MongoDB's own default port when a URI omits one explicitly.
_MONGO_DEFAULT_PORT = 27017

#: Fixed key for the single tracker-lease document.
LEASE_DOC_ID = "tracker"

# Database used when a URI names no default database.
_DEFAULT_DB_NAME = "weather"

FETCHES_COLLECTION = "fetches"
READINGS_COLLECTION = "readings"
LEASES_COLLECTION = "leases"
META_COLLECTION = "meta"
HEARTBEAT_DOC_ID = "tracker-heartbeat"


# --- URI guard --------------------------------------------------------------


def _hosts_and_ports(uri: str) -> list[tuple[str, int]]:
    """Extract ``(host, port)`` pairs from a ``mongodb://`` URI.

    ``mongodb+srv://`` URIs resolve their real hosts/ports via a DNS SRV
    lookup the URI text does not carry, so no pairs are returned for that
    scheme — :func:`_reject_srv_uri` rejects it outright before this
    matters.
    """
    if uri.startswith("mongodb+srv://"):
        return []
    if uri.startswith("mongodb://"):
        rest = uri[len("mongodb://") :]
    else:
        rest = uri

    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]

    pairs: list[tuple[str, int]] = []
    for host_port in rest.split(","):
        host_port = host_port.strip()
        if not host_port:
            continue
        if ":" in host_port:
            host, _, port_s = host_port.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = _MONGO_DEFAULT_PORT
        else:
            host, port = host_port, _MONGO_DEFAULT_PORT
        pairs.append((host, port))
    return pairs


def _reject_srv_uri(uri: str) -> None:
    if uri.startswith("mongodb+srv://"):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"{WEATHER_MONGO_URI_ENV} uses mongodb+srv://, which resolves its real "
                "host and port via a DNS SRV lookup this guard cannot verify"
            ),
            remediation=(
                f"set {WEATHER_MONGO_URI_ENV} to a plain mongodb:// URI naming the "
                "weather-mongodb host:port explicitly, not mongodb+srv://"
            ),
        )


def _reject_loopback_legacy_ports(uri: str) -> None:
    for host, port in _hosts_and_ports(uri):
        if port in _FORBIDDEN_PORTS and host.lower() in _LOOPBACK_HOSTS:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"{WEATHER_MONGO_URI_ENV} resolves to {host!r} on port {port}, a "
                    "loopback/host address on a port another service already owns"
                ),
                remediation=(
                    f"point {WEATHER_MONGO_URI_ENV} at the dedicated weather-mongodb "
                    "container by its service hostname, e.g. "
                    f"{DEFAULT_MONGO_URI!r}, not a loopback address on port {port}"
                ),
            )


def resolve_uri(uri: str | None = None) -> str:
    """Return the weather-mongodb URI, applying the guard.

    Reads :data:`WEATHER_MONGO_URI_ENV` (default :data:`DEFAULT_MONGO_URI`)
    when *uri* is ``None``. Raises :class:`CliError` (exit 2) for a
    ``mongodb+srv://`` URI or a loopback/host address on a forbidden port.
    This function never imports ``pymongo`` — it is pure URI-text
    validation, exercised directly by tests without the driver installed.
    """
    resolved = uri if uri is not None else os.environ.get(WEATHER_MONGO_URI_ENV, DEFAULT_MONGO_URI)
    _reject_srv_uri(resolved)
    _reject_loopback_legacy_ports(resolved)
    return resolved


def _pymongo() -> Any:
    """Return the ``pymongo`` module, lazily imported.

    Raises :class:`CliError` (exit 2) — never a bare ``ImportError`` — when
    pymongo is not installed, naming the exact install command.
    """
    try:
        import pymongo  # noqa: F401
    except ImportError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="pymongo is not installed",
            remediation='pip install "climate-cli[weather]"',
        ) from exc
    return pymongo


def connect(uri: str | None = None) -> Any:
    """Open a ``MongoClient`` to weather-mongodb; the one connection choke point.

    Applies the URI guard (:func:`resolve_uri`) and lazily imports
    ``pymongo`` (:func:`_pymongo`) before connecting. ``tz_aware=True``:
    pymongo decodes BSON datetimes as naive by default, which would drop
    the UTC tzinfo every stored ``requested_at``/``observed_at`` carries
    going in.
    """
    pymongo = _pymongo()
    resolved = resolve_uri(uri)
    return pymongo.MongoClient(resolved, tz_aware=True)


def database(client: Any) -> Any:
    """Return the default database for *client* (``weather`` when unnamed)."""
    return client.get_default_database(default=_DEFAULT_DB_NAME)


# --- BSON body wrapping and millisecond truncation ---------------------------


def _wrap_body(body: bytes) -> Any:
    """Wrap *body* as :class:`bson.Binary` when ``bson`` is importable.

    Lazily imports ``bson`` — never at module scope — so this still works
    against the hand-written fake collection (no ``bson`` available in this
    environment: falls through to plain ``bytes``, which is exactly what
    the fake needs) and wraps explicitly when talking to a real driver.
    """
    try:
        from bson import Binary
    except ImportError:
        return bytes(body)
    return Binary(bytes(body))


def _truncate_to_millis(value: datetime) -> datetime:
    """Truncate *value* to millisecond precision (BSON's ``Date`` resolution)."""
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


# --- filter helpers (shared by the store and reused conceptually by Mongo) ---


def _range_filter(since: datetime | None, until: datetime | None) -> dict[str, Any]:
    range_: dict[str, Any] = {}
    if since is not None:
        range_["$gte"] = since
    if until is not None:
        range_["$lte"] = until
    return range_


def _fetch_query(
    *,
    provider: str | None,
    location: str | None,
    since: datetime | None,
    until: datetime | None,
    errors_only: bool,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if provider is not None:
        query["provider"] = provider
    if location is not None:
        query["location"] = location
    range_ = _range_filter(since, until)
    if range_:
        query["requested_at"] = range_
    if errors_only:
        query["$or"] = [{"error": {"$ne": None}}, {"status": {"$gte": 400}}]
    return query


def _reading_query(
    *,
    provider: str | None,
    location: str | None,
    kind: str | None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if provider is not None:
        query["provider"] = provider
    if location is not None:
        query["location"] = location
    if kind is not None:
        query["kind"] = kind
    range_ = _range_filter(since, until)
    if range_:
        query["observed_at"] = range_
    return query


# --- the store ----------------------------------------------------------------


class MongoWeatherStore:
    """A :class:`climate.weather.store.WeatherStore` backed by two Mongo collections.

    Takes collection handles directly (not a URI) so it can be pointed at a
    real ``pymongo`` collection or at a hand-written fake collection that
    implements only the subset of the collection API this class actually
    calls (``insert_one``, ``find_one``, ``find``, ``count_documents``,
    ``delete_many``, ``create_index``) — see
    ``tests/weather/test_mongo.py``. Both collections must live in the same
    database; opening them is :func:`connect`'s job, not this class's.
    """

    def __init__(self, fetches: Any, readings: Any, *, meta: Any = None, db: Any = None) -> None:
        self._fetches = fetches
        self._readings = readings
        self._meta = meta
        self._db = db
        # (provider, location, requested_at desc): the shape every filtered
        # iter_fetches/latest_fetch/count_fetches query above narrows by.
        self._fetches.create_index([("provider", 1), ("location", 1), ("requested_at", -1)])
        self._readings.create_index([("fetch_id", 1)])

    # --- optional service extensions (see InMemoryWeatherStore) -----------

    def save_heartbeat(
        self,
        version: str,
        at: datetime,
        providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Upsert the single tracker-heartbeat document.

        ``providers`` is the tracker's per-provider availability snapshot,
        sanitized by
        :func:`climate.weather.store.heartbeat_provider_snapshot` so the
        stored document can never carry a credential value.
        """
        if self._meta is None:
            return
        self._meta.update_one(
            {"_id": HEARTBEAT_DOC_ID},
            {
                "$set": {
                    "version": version,
                    "at": _truncate_to_millis(at),
                    "providers": heartbeat_provider_snapshot(providers),
                }
            },
            upsert=True,
        )

    def latest_heartbeat(self) -> dict[str, Any] | None:
        if self._meta is None:
            return None
        document = self._meta.find_one({"_id": HEARTBEAT_DOC_ID})
        if not document:
            return None
        # ``providers`` is absent from every document written before the
        # availability snapshot existed, and reads back as ``None``.
        return {
            "version": document.get("version"),
            "at": document.get("at"),
            "providers": document.get("providers"),
        }

    def size_bytes(self) -> int | None:
        """Database size on disk from ``dbstats``; ``None`` when unavailable."""
        if self._db is None:
            return None
        try:
            stats = self._db.command("dbstats")
        except Exception:  # noqa: BLE001 - health must never fail on stats
            return None
        size = stats.get("storageSize") or stats.get("dataSize")
        return int(size) if size is not None else None

    # --- raw fetch records ----------------------------------------------

    def save_fetch(self, record: FetchRecord) -> str:
        new_id = uuid.uuid4().hex
        document = record.to_document()
        document.pop("id", None)
        document["_id"] = new_id
        document["requested_at"] = _truncate_to_millis(document["requested_at"])
        document["body"] = _wrap_body(document["body"])
        self._fetches.insert_one(document)
        return new_id

    @staticmethod
    def _fetch_from_document(document: Any) -> FetchRecord:
        document = dict(document)
        document["body"] = bytes(document.get("body") or b"")
        return FetchRecord.from_document(document)

    def get_fetch(self, fetch_id: str) -> FetchRecord | None:
        document = self._fetches.find_one({"_id": fetch_id})
        return None if document is None else self._fetch_from_document(document)

    def iter_fetches(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        errors_only: bool = False,
        limit: int | None = None,
    ) -> Iterator[FetchRecord]:
        query = _fetch_query(
            provider=provider, location=location, since=since, until=until, errors_only=errors_only
        )
        cursor = self._fetches.find(query).sort("requested_at", -1)
        if limit is not None:
            cursor = cursor.limit(limit)
        return (self._fetch_from_document(document) for document in cursor)

    def latest_fetch(
        self, *, provider: str | None = None, location: str | None = None
    ) -> FetchRecord | None:
        return next(self.iter_fetches(provider=provider, location=location, limit=1), None)

    def count_fetches(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        errors_only: bool = False,
    ) -> int:
        query = _fetch_query(
            provider=provider, location=location, since=since, until=until, errors_only=errors_only
        )
        return self._fetches.count_documents(query)

    # --- normalized readings ----------------------------------------------

    def _reading_document(self, fetch_id: str, reading: Reading) -> dict[str, Any]:
        new_id = uuid.uuid4().hex
        document = reading.to_document()
        document.pop("id", None)
        document["_id"] = new_id
        document["fetch_id"] = fetch_id
        document["observed_at"] = _truncate_to_millis(document["observed_at"])
        document["requested_at"] = _truncate_to_millis(document["requested_at"])
        if document.get("model_run_at") is not None:
            document["model_run_at"] = _truncate_to_millis(document["model_run_at"])
        return document

    def _prepare_reading_documents(
        self, fetch_id: str, readings: Sequence[Reading]
    ) -> list[dict[str, Any]]:
        """Validate *readings* against *fetch_id* and build their documents.

        Every check that can refuse the write happens here, before anything
        is written or deleted.
        """
        if self._fetches.find_one({"_id": fetch_id}) is None:
            raise UnknownFetchError(
                f"no fetch record {fetch_id!r}: save the raw response before its readings"
            )
        for reading in readings:
            if reading.fetch_id and reading.fetch_id != fetch_id:
                raise ValueError(
                    f"reading is bound to fetch {reading.fetch_id!r}, not {fetch_id!r}"
                )
        return [self._reading_document(fetch_id, reading) for reading in readings]

    def _insert_readings(self, documents: Sequence[dict[str, Any]]) -> list[str]:
        """Insert *documents*, removing any already inserted if one fails.

        A partial insert would otherwise leave a half-written replacement
        set behind next to the old one.
        """
        inserted: list[str] = []
        try:
            for document in documents:
                self._readings.insert_one(document)
                inserted.append(document["_id"])
        except Exception:
            if inserted:
                self._readings.delete_many({"_id": {"$in": inserted}})
            raise
        return inserted

    def save_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        return self._insert_readings(self._prepare_reading_documents(fetch_id, readings))

    def replace_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        # All-or-nothing without relying on a transaction (this store runs
        # against a standalone mongod, where transactions are unavailable):
        # validate the whole replacement set, capture the ids of the
        # readings being replaced, insert the replacements, and only then
        # delete the captured ids. A refused validation or a failed insert
        # therefore leaves the previous set intact and re-derivable.
        documents = self._prepare_reading_documents(fetch_id, readings)
        stale_ids = [document["_id"] for document in self._readings.find({"fetch_id": fetch_id})]
        new_ids = self._insert_readings(documents)
        if stale_ids:
            self._readings.delete_many({"_id": {"$in": stale_ids}})
        return new_ids

    def readings_for_fetch(self, fetch_id: str) -> list[Reading]:
        documents = self._readings.find({"fetch_id": fetch_id})
        return [Reading.from_document(document) for document in documents]

    def latest_reading(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: str | None = None,
    ) -> Reading | None:
        query = _reading_query(provider=provider, location=location, kind=kind)
        cursor = (
            self._readings.find(query).sort([("observed_at", -1), ("requested_at", -1)]).limit(1)
        )
        documents = list(cursor)
        return Reading.from_document(documents[0]) if documents else None

    def series(
        self,
        variable: str,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[SeriesPoint]:
        query = _reading_query(
            provider=provider, location=location, kind=kind, since=since, until=until
        )
        query[f"values.{variable}"] = {"$exists": True}
        cursor = self._readings.find(query).sort("observed_at", 1)
        if limit is not None:
            cursor = cursor.limit(limit)
        points: list[SeriesPoint] = []
        for document in cursor:
            reading = Reading.from_document(document)
            measurement = reading.values[variable]
            points.append(
                SeriesPoint(
                    observed_at=reading.observed_at,
                    value=measurement.value,
                    unit=measurement.unit,
                    provider=reading.provider,
                    source=reading.source,
                    model=reading.model,
                    kind=reading.kind,
                    fetch_id=reading.fetch_id,
                    model_run_at=reading.model_run_at,
                )
            )
        return points

    def count_readings(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        query = _reading_query(
            provider=provider, location=location, kind=kind, since=since, until=until
        )
        return self._readings.count_documents(query)


# --- tracker lease -------------------------------------------------------------


def build_store(uri: str | None = None) -> MongoWeatherStore:
    """Connect (through the one choke-point) and return the service's store.

    Used by the tracker and the web service. The URI comes from
    ``WEATHER_MONGO_URI`` unless given; the guard in :func:`resolve_uri`
    still applies.
    """
    db = database(connect(uri))
    return MongoWeatherStore(
        db[FETCHES_COLLECTION], db[READINGS_COLLECTION], meta=db[META_COLLECTION], db=db
    )


def lease_collection(uri: str | None = None) -> Any:
    """The collection holding the single-tracker lease document."""
    return database(connect(uri))[LEASES_COLLECTION]


def _is_duplicate_key_error(exc: Exception) -> bool:
    """True when *exc* is a Mongo duplicate-key error (E11000), real or faked.

    Matches ``pymongo.errors.DuplicateKeyError`` by attribute/name rather
    than importing it, so :func:`acquire_lease` works the same way whether
    *collection* is a real ``pymongo`` collection or the hand-written fake
    used in tests (which raises its own ``DuplicateKeyError`` with the same
    shape, never importing pymongo either).
    """
    return getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError"


def acquire_lease(
    collection: Any, holder_id: str, ttl_seconds: float, *, now: datetime | None = None
) -> bool:
    """Try to atomically acquire (or renew, if already held) the tracker lease.

    Single-document lease keyed by :data:`LEASE_DOC_ID`. The filter matches
    either the current holder (so the holder's own repeated acquire calls
    just extend it) or an already-expired lease; ``upsert=True`` creates the
    document the first time. When neither condition holds, the upsert
    attempts to insert a second document with the same ``_id`` and Mongo's
    unique index on ``_id`` raises a duplicate-key error — caught here and
    turned into ``False`` rather than propagated, since "someone else holds
    a live lease" is an expected, not exceptional, outcome.
    """
    now = now if now is not None else datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    query = {
        "_id": LEASE_DOC_ID,
        "$or": [{"holder": holder_id}, {"expires_at": {"$lte": now}}],
    }
    update = {"$set": {"holder": holder_id, "expires_at": expires_at}}
    try:
        collection.update_one(query, update, upsert=True)
    except Exception as exc:  # noqa: BLE001 - re-raised unless it's a duplicate key
        if _is_duplicate_key_error(exc):
            return False
        raise
    return True


def renew_lease(
    collection: Any, holder_id: str, ttl_seconds: float, *, now: datetime | None = None
) -> bool:
    """Extend the lease's expiry, only if *holder_id* currently holds it."""
    now = now if now is not None else datetime.now(UTC)
    query = {"_id": LEASE_DOC_ID, "holder": holder_id}
    update = {"$set": {"expires_at": now + timedelta(seconds=ttl_seconds)}}
    result = collection.update_one(query, update)
    return result.matched_count > 0


def release_lease(collection: Any, holder_id: str) -> bool:
    """Release the lease, only if *holder_id* currently holds it."""
    result = collection.delete_one({"_id": LEASE_DOC_ID, "holder": holder_id})
    return result.deleted_count > 0
