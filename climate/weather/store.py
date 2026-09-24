"""Storage model and store interface for the weather tracking service.

This module is the *contract* the rest of the weather service builds on.
It is deliberately small, fully typed, and dependency-free (standard
library only); the Mongo-backed store lives elsewhere and lazy-imports
``pymongo`` inside its own functions.

Two record types make up the storage model:

``FetchRecord``
    The **system of record**: the verbatim bytes a provider returned for
    one HTTP request, together with the metadata needed to interpret and
    audit them (content type, declared charset, provider, redacted
    endpoint, location label, ``requested_at`` in UTC, HTTP status, cache
    headers, the sha256 of the bytes, error detail and a schema version).
    Raw bytes are stored *as received* — never re-serialized JSON, never
    a BSON-converted document — because re-encoding would alter float
    text and key order, and IMS XML arrives as ISO-8859-8 which a JSON
    round-trip cannot hold.  A failed fetch (timeout, 4xx/5xx, 429) and a
    conditional-GET 304 are fetch records too, so a gap in the *data* is
    always distinguishable from a gap in *collection*.

``Reading``
    A normalized observation derived from exactly one fetch record.  It
    carries provenance (provider / source / model), keeps ``observed_at``
    distinct from ``requested_at``, records each value with its unit and
    the provider's original value and unit, and references the fetch
    record it came from — so every reading is re-derivable and traceable.

The ordering constraint is structural, not a convention:
:meth:`WeatherStore.save_fetch` returns the id that
:meth:`WeatherStore.save_readings` requires, and saving readings against
an unknown id raises :class:`UnknownFetchError`.  A reading therefore
cannot exist before the raw bytes it was derived from have been stored.

All times held in this module are timezone-aware UTC; constructors
reject naive datetimes and convert other offsets to UTC.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "HEARTBEAT_PROVIDER_FIELDS",
    "REDACTED",
    "SCHEMA_VERSION",
    "SECRET_QUERY_PARAMS",
    "FetchError",
    "FetchRecord",
    "InMemoryWeatherStore",
    "Measurement",
    "Reading",
    "ReadingKind",
    "SecretLeakError",
    "SeriesPoint",
    "StoreError",
    "UnknownFetchError",
    "WeatherStore",
    "heartbeat_provider_snapshot",
    "redact_url",
]

#: Version of the stored document shapes.  Bumped when the *raw* record
#: shape changes; normalized readings are re-derived rather than migrated.
SCHEMA_VERSION = 1

#: Placeholder written in place of a secret in a stored endpoint.
REDACTED = "REDACTED"

#: Query parameters known to carry provider credentials (compared
#: case-insensitively).  A stored endpoint may not contain a live value
#: for any of them.
SECRET_QUERY_PARAMS: frozenset[str] = frozenset(
    {"apikey", "api_key", "apitoken", "api_token", "appid", "access_token", "key", "token"}
)

#: The only keys one provider entry of a heartbeat's availability snapshot
#: may carry: whether the provider may run, why not when it may not, and
#: whether its credential was present *in the tracker's own environment*.
#: A credential value — or anything derived from one, such as its length or
#: a prefix of it — is deliberately not among them and is never stored.
HEARTBEAT_PROVIDER_FIELDS: tuple[str, ...] = ("enabled", "reason", "credential_present")


def heartbeat_provider_snapshot(
    providers: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Normalize a per-provider availability snapshot for a heartbeat.

    Only :data:`HEARTBEAT_PROVIDER_FIELDS` survive, and each is coerced to
    its documented type, so a caller cannot smuggle a credential (or any
    other unexpected key) into the stored document by accident:

    * ``enabled`` — ``bool``.
    * ``reason`` — the disabled reason, ``None`` when enabled or unstated.
    * ``credential_present`` — ``bool``, or ``None`` when the provider needs
      no credential at all.

    ``None`` in gives ``None`` out, which is what a store with no snapshot
    (an old heartbeat document) reads back as.
    """
    if providers is None:
        return None
    snapshot: dict[str, dict[str, Any]] = {}
    for provider_id, entry in providers.items():
        reason = entry.get("reason")
        credential_present = entry.get("credential_present")
        snapshot[str(provider_id)] = {
            "enabled": bool(entry.get("enabled")),
            "reason": str(reason) if reason else None,
            "credential_present": (
                None if credential_present is None else bool(credential_present)
            ),
        }
    return snapshot


#: What a normalized reading describes: a measurement, an analysis/model
#: value for the present, or a value for a future time.
ReadingKind = Literal["observation", "model", "forecast"]

READING_KINDS: frozenset[str] = frozenset({"observation", "model", "forecast"})


class StoreError(Exception):
    """Base class for storage-contract violations."""


class UnknownFetchError(StoreError):
    """Raised when readings are saved against a fetch id that does not exist."""


class SecretLeakError(ValueError):
    """Raised when a value about to be stored still contains a credential."""


def _to_utc(value: datetime, field_name: str) -> datetime:
    """Return ``value`` as timezone-aware UTC, rejecting naive datetimes."""
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        raise TypeError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC, got a naive datetime")
    return value.astimezone(UTC)


def _require(text: str, field_name: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def redact_url(
    url: str,
    *,
    extra_params: Sequence[str] = (),
    secret_values: Sequence[str] = (),
) -> str:
    """Return ``url`` with credentials replaced by :data:`REDACTED`.

    Redacts the values of :data:`SECRET_QUERY_PARAMS` plus any
    ``extra_params`` (case-insensitive), any literal string in
    ``secret_values`` wherever it appears, and URL userinfo.  Everything
    else — path, ordering, other query parameters — is preserved, so the
    stored endpoint stays a faithful record of what was requested.

    Sibling: :func:`climate.weather.http.redact` is the *transport* layer's
    redactor. Both exist on purpose and neither replaces the other — this
    one parses a URL properly (userinfo, caller-supplied secret literals,
    stable re-encoding) because what it returns is persisted as
    :attr:`FetchRecord.endpoint` and validated against
    :class:`SecretLeakError`; ``http.redact`` is a cheap regex over
    arbitrary text (a header value, an exception message, a partial URL)
    that must never fail on unparseable input. Use this one for anything
    stored, that one for anything logged.
    """
    secret_names = SECRET_QUERY_PARAMS | {name.lower() for name in extra_params}
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if query:
        query = [
            (name, REDACTED if name.lower() in secret_names and value else value)
            for name, value in query
        ]
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
    cleaned = urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
    )
    for secret in secret_values:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def _assert_no_secret(url: str) -> None:
    parts = urlsplit(url)
    if "@" in parts.netloc and parts.netloc.rsplit("@", 1)[0] != REDACTED:
        raise SecretLeakError("endpoint carries userinfo; redact it with redact_url()")
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() in SECRET_QUERY_PARAMS and value and value != REDACTED:
            raise SecretLeakError(
                f"endpoint query parameter {name!r} is not redacted; use redact_url()"
            )


@dataclass(frozen=True, slots=True)
class FetchError:
    """Why a fetch failed, kept alongside the (possibly empty) body.

    ``kind`` is a short machine-readable token. The tokens actually written
    are the scheduler's four module constants (see
    :mod:`climate.weather.scheduler`): ``timeout`` (``ERROR_TIMEOUT``),
    ``transport`` (``ERROR_TRANSPORT`` — connection refused, DNS, TLS),
    ``rate_limited`` (``ERROR_RATE_LIMITED``, HTTP 429) and ``http_error``
    (``ERROR_HTTP``, any other non-2xx status). The field is a free string,
    so another writer may add its own token, but those four are the
    vocabulary a reader should expect.

    ``message`` is a human-readable detail that must never contain a
    credential — pass anything URL-shaped through :func:`redact_url` (for a
    stored value) or :func:`climate.weather.http.redact` (for free text)
    first.
    """

    kind: str
    message: str = ""

    def __post_init__(self) -> None:
        _require(self.kind, "FetchError.kind")

    def to_document(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> FetchError:
        return cls(kind=str(document["kind"]), message=str(document.get("message", "")))


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchRecord:
    """One provider response, stored verbatim, with its fetch metadata.

    :param id: store-assigned identifier; empty until the record is saved.
    :param provider: provider id (``ims``, ``open-meteo``, ``met-no``, …).
    :param endpoint: the requested URL **with secrets redacted**
        (:func:`redact_url`); an unredacted credential raises
        :class:`SecretLeakError`.
    :param location: the user's location *label*, never coordinates.
    :param requested_at: when the request was issued, UTC.
    :param status: HTTP status (``304`` and ``429`` included), or ``None``
        when no response arrived (timeout, connection failure).
    :param body: the exact bytes received, unmodified.
    :param content_type: the response ``Content-Type``, if any.
    :param charset: the declared charset (e.g. ``iso-8859-8``), if any.
    :param cache_headers: cache-relevant response headers
        (``Expires``, ``Last-Modified``, ``ETag``, …), copied on construction.
    :param sha256: hex digest of ``body``; computed when omitted and
        verified when supplied, so a corrupted round-trip is loud.
    :param error: failure detail; ``None`` for a response that arrived.
    :param schema_version: shape version of the stored document.
    """

    id: str = ""
    provider: str
    endpoint: str
    location: str
    requested_at: datetime
    status: int | None
    body: bytes = b""
    content_type: str | None = None
    charset: str | None = None
    cache_headers: Mapping[str, str] = field(default_factory=dict)
    sha256: str = ""
    error: FetchError | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require(self.provider, "FetchRecord.provider")
        _require(self.endpoint, "FetchRecord.endpoint")
        _require(self.location, "FetchRecord.location")
        _assert_no_secret(self.endpoint)
        if not isinstance(self.body, (bytes, bytearray)):
            raise TypeError("FetchRecord.body must be bytes: store the response as received")
        if self.status is None and self.error is None:
            raise ValueError("FetchRecord needs an HTTP status or an error detail")
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "requested_at", _to_utc(self.requested_at, "requested_at"))
        object.__setattr__(self, "cache_headers", MappingProxyType(dict(self.cache_headers)))
        digest = hashlib.sha256(self.body).hexdigest()
        if self.sha256 and self.sha256 != digest:
            raise ValueError(
                f"FetchRecord.sha256 {self.sha256!r} does not match the body digest {digest!r}"
            )
        object.__setattr__(self, "sha256", digest)

    @property
    def is_error(self) -> bool:
        """True when this fetch did not deliver a usable response body.

        A ``304 Not Modified`` is *not* an error: it is a successful
        conditional GET whose body is legitimately empty.
        """
        return self.error is not None or (self.status is not None and self.status >= 400)

    def text(self, *, errors: str = "strict") -> str:
        """Decode :attr:`body` using the declared charset (default UTF-8)."""
        return self.body.decode(self.charset or "utf-8", errors=errors)

    def to_document(self) -> dict[str, Any]:
        """Return a plain mapping suitable for a document store.

        ``body`` stays ``bytes`` (BSON binary) — never re-encoded text.
        The Mongo store maps ``id`` to ``_id``.
        """
        document: dict[str, Any] = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "location": self.location,
            "requested_at": self.requested_at,
            "status": self.status,
            "body": self.body,
            "content_type": self.content_type,
            "charset": self.charset,
            "cache_headers": dict(self.cache_headers),
            "sha256": self.sha256,
            "error": self.error.to_document() if self.error else None,
            "schema_version": self.schema_version,
        }
        if self.id:
            document["id"] = self.id
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> FetchRecord:
        """Rebuild a record from :meth:`to_document` output (or a Mongo document)."""
        raw_id = document.get("id", document.get("_id", ""))
        error = document.get("error")
        return cls(
            id=str(raw_id) if raw_id else "",
            provider=str(document["provider"]),
            endpoint=str(document["endpoint"]),
            location=str(document["location"]),
            requested_at=document["requested_at"],
            status=document.get("status"),
            body=bytes(document.get("body", b"")),
            content_type=document.get("content_type"),
            charset=document.get("charset"),
            cache_headers=dict(document.get("cache_headers") or {}),
            sha256=str(document.get("sha256") or ""),
            error=FetchError.from_document(error) if error else None,
            schema_version=int(document.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class Measurement:
    """One normalized value, with the provider's original alongside it.

    ``value``/``unit`` are the normalized pair the query surface uses;
    ``original_value``/``original_unit`` keep what the provider actually
    said (``None`` when no conversion was applied), so a unit-conversion
    bug is always visible from the stored reading.
    """

    value: float
    unit: str
    original_value: float | str | None = None
    original_unit: str | None = None

    def __post_init__(self) -> None:
        _require(self.unit, "Measurement.unit")

    def to_document(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "original_value": self.original_value,
            "original_unit": self.original_unit,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Measurement:
        return cls(
            value=document["value"],
            unit=str(document["unit"]),
            original_value=document.get("original_value"),
            original_unit=document.get("original_unit"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    """A normalized reading derived from exactly one :class:`FetchRecord`.

    :param id: store-assigned identifier; empty until saved.
    :param fetch_id: the raw record this was derived from.  Leave empty
        and the store binds it on save; set it to a *different* fetch and
        the save is refused.
    :param provider: provider id (who served it).
    :param source: what produced the numbers — a station id, an endpoint
        path, ``model``.  Distinct from ``provider`` on purpose.
    :param model: model/run identifier when the provider names one.
    :param location: the user's location label.
    :param observed_at: the time the values *apply to* (measurement time
        for observations, target time for forecasts), UTC.
    :param requested_at: when the underlying fetch was issued, UTC.  Kept
        distinct from ``observed_at`` so staleness is measurable.
    :param model_run_at: the **provider's** model issue / run time, UTC —
        the instant the provider says the model run this reading comes from
        was issued.  It is *not* ``requested_at`` (when we downloaded the
        response) and *not* ``observed_at`` (what time the values apply
        to).  ``None`` means the provider stated no issue time: adapters
        set this only when the response actually carries one, and never
        substitute the fetch time for it.  Documents written before this
        field existed (``schema_version`` 1) have no ``model_run_at`` key
        and read back as ``None``.
    :param kind: ``observation`` | ``model`` | ``forecast``.
    :param values: variable name -> :class:`Measurement`, copied on
        construction.
    :param schema_version: shape version of the stored document.
    """

    id: str = ""
    fetch_id: str = ""
    provider: str
    source: str
    model: str | None = None
    location: str
    observed_at: datetime
    requested_at: datetime
    model_run_at: datetime | None = None
    kind: ReadingKind = "observation"
    values: Mapping[str, Measurement]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require(self.provider, "Reading.provider")
        _require(self.source, "Reading.source")
        _require(self.location, "Reading.location")
        if self.kind not in READING_KINDS:
            raise ValueError(
                f"Reading.kind must be one of {sorted(READING_KINDS)}, got {self.kind!r}"
            )
        if not self.values:
            raise ValueError("Reading.values must not be empty")
        object.__setattr__(self, "observed_at", _to_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "requested_at", _to_utc(self.requested_at, "requested_at"))
        if self.model_run_at is not None:
            object.__setattr__(self, "model_run_at", _to_utc(self.model_run_at, "model_run_at"))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def age(self, *, now: datetime | None = None) -> timedelta:
        """How old this reading's observation is at ``now`` (default: wall clock UTC)."""
        reference = _to_utc(now, "now") if now is not None else datetime.now(UTC)
        return reference - self.observed_at

    def to_document(self) -> dict[str, Any]:
        """Return a plain mapping suitable for a document store."""
        document: dict[str, Any] = {
            "fetch_id": self.fetch_id,
            "provider": self.provider,
            "source": self.source,
            "model": self.model,
            "location": self.location,
            "observed_at": self.observed_at,
            "requested_at": self.requested_at,
            "model_run_at": self.model_run_at,
            "kind": self.kind,
            "values": {name: value.to_document() for name, value in self.values.items()},
            "schema_version": self.schema_version,
        }
        if self.id:
            document["id"] = self.id
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Reading:
        """Rebuild a reading from :meth:`to_document` output (or a Mongo document)."""
        raw_id = document.get("id", document.get("_id", ""))
        return cls(
            id=str(raw_id) if raw_id else "",
            fetch_id=str(document.get("fetch_id") or ""),
            provider=str(document["provider"]),
            source=str(document["source"]),
            model=document.get("model"),
            location=str(document["location"]),
            observed_at=document["observed_at"],
            requested_at=document["requested_at"],
            # Absent in every document written before this field existed.
            model_run_at=document.get("model_run_at"),
            kind=document.get("kind", "observation"),
            values={
                name: Measurement.from_document(value) for name, value in document["values"].items()
            },
            schema_version=int(document.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One point of a variable's time series, carrying its provenance.

    ``model_run_at`` is the provider's own model issue / run time for the
    reading this point came from (see :attr:`Reading.model_run_at`), or
    ``None`` when the provider stated none.  It is a *trailing* field with
    a default so existing positional construction keeps working.
    """

    observed_at: datetime
    value: float
    unit: str
    provider: str
    source: str
    model: str | None
    kind: ReadingKind
    fetch_id: str
    model_run_at: datetime | None = None


@runtime_checkable
class WeatherStore(Protocol):
    """Everything the weather service needs from persistent storage.

    Implementations: :class:`InMemoryWeatherStore` (the fake every test
    uses) and the Mongo-backed store.  Both must pass the conformance
    suite in ``tests/weather/store_contract.py``.

    Contract notes shared by all implementations:

    * Raw bytes are stored verbatim; ``sha256`` of a stored record equals
      the digest of the bytes that were fetched.
    * Failed fetches and 304s are stored like any other fetch.
    * Readings can only be attached to an existing fetch record.
    * Filters are ``None`` = "no filter"; ``since`` is inclusive and
      ``until`` is inclusive.
    * Every datetime in and out is timezone-aware UTC.
    """

    def save_fetch(self, record: FetchRecord) -> str:
        """Store one raw provider response; return its new id."""

    def get_fetch(self, fetch_id: str) -> FetchRecord | None:
        """Return the stored record, or ``None`` if there is no such id."""

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
        """Iterate stored records, newest ``requested_at`` first.

        This is the re-derivation entry point: readings are rebuilt by
        replaying these raw bodies.
        """

    def latest_fetch(
        self, *, provider: str | None = None, location: str | None = None
    ) -> FetchRecord | None:
        """Return the newest matching record by ``requested_at``."""

    def count_fetches(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        errors_only: bool = False,
    ) -> int:
        """Count matching fetch records (the measured basis for 24 h figures)."""

    def save_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        """Store readings derived from ``fetch_id``; return their new ids.

        Raises :class:`UnknownFetchError` when ``fetch_id`` is unknown —
        which is what makes it impossible to store a reading before the
        raw bytes it came from.  Raises :class:`ValueError` when a
        reading is already bound to a different fetch.
        """

    def replace_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        """Re-derivation: swap all readings of ``fetch_id`` for ``readings``.

        The raw record is never modified.  The swap is **all-or-nothing**:
        every replacement is validated (the fetch exists, no reading is
        bound to a different fetch) and persisted before the previous
        readings are removed, so a refused or failed replacement leaves the
        existing set intact rather than destroying re-derivable history on
        the way to discovering the problem.  The same exceptions as
        :meth:`save_readings` are raised, and nothing is stored when one is.
        """

    def readings_for_fetch(self, fetch_id: str) -> list[Reading]:
        """Return the readings derived from one fetch record, in save order."""

    def latest_reading(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
    ) -> Reading | None:
        """Return the newest matching reading by ``observed_at``.

        The first-class query: the AC-control agent controls from it, so
        callers compare :meth:`Reading.age` against their own max age.
        """

    def series(
        self,
        variable: str,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[SeriesPoint]:
        """Return one variable's points, oldest first (charts, comparisons).

        ``limit`` keeps the oldest ``limit`` matching points.
        """

    def count_readings(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Count matching readings."""


def _within(moment: datetime, since: datetime | None, until: datetime | None) -> bool:
    if since is not None and moment < _to_utc(since, "since"):
        return False
    if until is not None and moment > _to_utc(until, "until"):
        return False
    return True


class InMemoryWeatherStore:
    """A :class:`WeatherStore` kept in process memory.

    This is the fake every test in the service uses: it enforces the same
    invariants as the Mongo store (fetch-before-reading ordering, UTC
    times, verbatim bytes) without a database, so the suite passes with
    no network and no docker.  Not thread-safe and not persistent.
    """

    def __init__(self) -> None:
        self._fetches: dict[str, FetchRecord] = {}
        self._readings: dict[str, Reading] = {}
        self._heartbeat: dict[str, Any] | None = None

    # --- optional service extensions (not part of the WeatherStore protocol) --
    #
    # The tracker records a heartbeat carrying its package version so doctor
    # can detect a stale image, and a per-provider availability snapshot
    # because the tracker is the only process that sees the credentials;
    # callers reach these with ``getattr`` and tolerate their absence.

    def save_heartbeat(
        self,
        version: str,
        at: datetime,
        providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Record that a tracker running ``version`` was alive at ``at`` (UTC).

        ``providers`` is the optional per-provider availability snapshot
        described by :func:`heartbeat_provider_snapshot`, which sanitizes it
        before it is stored. Omitting it keeps the two-argument call shape
        this method shipped with.
        """
        self._heartbeat = {
            "version": version,
            "at": at,
            "providers": heartbeat_provider_snapshot(providers),
        }

    def latest_heartbeat(self) -> dict[str, Any] | None:
        """The newest heartbeat as ``{"version", "at", "providers"}``, or ``None``.

        ``providers`` is ``None`` when the tracker wrote no snapshot.
        """
        return dict(self._heartbeat) if self._heartbeat else None

    def size_bytes(self) -> int | None:
        """Bytes of stored response bodies (the Mongo store reports dbstats)."""
        return sum(len(record.body) for record in self._fetches.values())

    # --- lifecycle ------------------------------------------------------

    def clear(self) -> None:
        """Drop everything (test helper; not part of the protocol)."""
        self._fetches.clear()
        self._readings.clear()

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    # --- raw fetch records ----------------------------------------------

    def save_fetch(self, record: FetchRecord) -> str:
        fetch_id = self._new_id()
        self._fetches[fetch_id] = replace(record, id=fetch_id)
        return fetch_id

    def get_fetch(self, fetch_id: str) -> FetchRecord | None:
        return self._fetches.get(fetch_id)

    def _matching_fetches(
        self,
        provider: str | None,
        location: str | None,
        since: datetime | None,
        until: datetime | None,
        errors_only: bool,
    ) -> list[FetchRecord]:
        return [
            record
            for record in self._fetches.values()
            if (provider is None or record.provider == provider)
            and (location is None or record.location == location)
            and (not errors_only or record.is_error)
            and _within(record.requested_at, since, until)
        ]

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
        records = self._matching_fetches(provider, location, since, until, errors_only)
        records.sort(key=lambda record: record.requested_at, reverse=True)
        if limit is not None:
            records = records[:limit]
        return iter(records)

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
        return len(self._matching_fetches(provider, location, since, until, errors_only))

    # --- normalized readings ---------------------------------------------

    def _prepare_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[Reading]:
        """Validate *readings* against *fetch_id* and stamp ids on them.

        Every check that can refuse the write happens here, before any
        caller mutates stored state — which is what lets
        :meth:`replace_readings` validate the whole replacement set before
        it drops the existing one.
        """
        if fetch_id not in self._fetches:
            raise UnknownFetchError(
                f"no fetch record {fetch_id!r}: save the raw response before its readings"
            )
        prepared: list[Reading] = []
        for reading in readings:
            if reading.fetch_id and reading.fetch_id != fetch_id:
                raise ValueError(
                    f"reading is bound to fetch {reading.fetch_id!r}, not {fetch_id!r}"
                )
            prepared.append(replace(reading, id=self._new_id(), fetch_id=fetch_id))
        return prepared

    def save_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        prepared = self._prepare_readings(fetch_id, readings)
        for reading in prepared:
            self._readings[reading.id] = reading
        return [reading.id for reading in prepared]

    def replace_readings(self, fetch_id: str, readings: Sequence[Reading]) -> list[str]:
        # All-or-nothing: the complete replacement set is validated and
        # prepared *first*, so a refused replacement (unknown fetch, a
        # reading bound to another fetch) leaves the existing readings
        # exactly as they were rather than deleting them on the way to
        # discovering the problem.
        prepared = self._prepare_readings(fetch_id, readings)
        stale_ids = [
            reading.id for reading in self._readings.values() if reading.fetch_id == fetch_id
        ]
        for reading_id in stale_ids:
            del self._readings[reading_id]
        for reading in prepared:
            self._readings[reading.id] = reading
        return [reading.id for reading in prepared]

    def readings_for_fetch(self, fetch_id: str) -> list[Reading]:
        return [reading for reading in self._readings.values() if reading.fetch_id == fetch_id]

    def _matching_readings(
        self,
        provider: str | None,
        location: str | None,
        kind: ReadingKind | None,
        since: datetime | None,
        until: datetime | None,
    ) -> list[Reading]:
        return [
            reading
            for reading in self._readings.values()
            if (provider is None or reading.provider == provider)
            and (location is None or reading.location == location)
            and (kind is None or reading.kind == kind)
            and _within(reading.observed_at, since, until)
        ]

    def latest_reading(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
    ) -> Reading | None:
        matches = self._matching_readings(provider, location, kind, None, None)
        if not matches:
            return None
        return max(matches, key=lambda reading: (reading.observed_at, reading.requested_at))

    def series(
        self,
        variable: str,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[SeriesPoint]:
        matches = [
            reading
            for reading in self._matching_readings(provider, location, kind, since, until)
            if variable in reading.values
        ]
        # requested_at breaks observed_at ties, matching the Mongo store.
        matches.sort(key=lambda reading: (reading.observed_at, reading.requested_at))
        if limit is not None:
            matches = matches[:limit]
        return [
            SeriesPoint(
                observed_at=reading.observed_at,
                value=reading.values[variable].value,
                unit=reading.values[variable].unit,
                provider=reading.provider,
                source=reading.source,
                model=reading.model,
                kind=reading.kind,
                fetch_id=reading.fetch_id,
                model_run_at=reading.model_run_at,
            )
            for reading in matches
        ]

    def count_readings(
        self,
        *,
        provider: str | None = None,
        location: str | None = None,
        kind: ReadingKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return len(self._matching_readings(provider, location, kind, since, until))
