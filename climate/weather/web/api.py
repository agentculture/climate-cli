"""The weather HTTP API: pure route handlers plus a thin server wrapper.

This module implements the contract in ``docs/weather-api.md``. It is split
into two layers on purpose:

* **Pure handlers** — ``health``, ``list_providers``, ``list_locations``,
  ``latest``, ``series``, ``forecast`` and ``stats`` — each with the shape
  ``(store, config, providers, query_params, now) -> (status, json_dict)``.
  They perform no I/O beyond calling the injected ``store``, so tests call
  them directly with :class:`~climate.weather.store.InMemoryWeatherStore`
  and never open a socket.
* A **thin** ``http.server`` layer (:func:`dispatch`, ``WeatherRequestHandler``,
  :func:`create_server`) that turns a real request into the arguments above,
  turns the pure result into bytes on the wire, and serves the static
  dashboard directory with a path-traversal guard. It contains no business
  logic of its own.

Known contract gaps
--------------------
Several fields the API contract (``docs/weather-api.md``) describes are not
derivable from the merged ``store``/``providers``/``config`` contracts as
written. Each is called out at the point it is produced, with a ``GAP``
comment, and always resolves to the documented null rather than being
guessed or fabricated:

* ``provenance.station`` and ``provenance.interval_seconds`` —
  :class:`~climate.weather.store.Reading` and
  :class:`~climate.weather.store.Measurement` have no fields for these.
  ``provenance.model_run_at`` is no longer a gap:
  :attr:`~climate.weather.store.Reading.model_run_at` carries the provider's
  own model-run time and every route reports it, ``null`` only when the
  provider stated none.
* ``value.quality`` — :class:`~climate.weather.store.Measurement` has no
  quality field.
* ``providers[].capabilities.variables`` and
  ``.forecast_horizon_hours`` — :class:`~climate.weather.providers.base.Capability`
  is kind-level, not variable-level, and the provider contract declares no
  forecast horizon.
* ``providers[].freshness.expected_update_seconds`` and ``.notes`` — no such
  fields on the provider contract.
* ``health.tracker_version`` — no fetch record or reading carries a
  ``climate`` version; it comes from the tracker's optional heartbeat (with
  its per-provider availability snapshot) and is ``null`` until one is
  written.
* ``store.size_bytes`` and ``store.backend`` (best-effort guess from the
  store's module/class name; the ``WeatherStore`` protocol declares neither).

A few fields are filled with a documented *assumption* rather than a gap
(marked ``ASSUMPTION``) because the merged contracts do not tie two
concepts together explicitly (e.g. "which providers serve which location").
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

from climate import __version__ as PACKAGE_VERSION
from climate.weather import vocabulary
from climate.weather.config import WeatherConfig
from climate.weather.providers import Capability, FreshnessStrategy
from climate.weather.providers.base import WeatherProvider
from climate.weather.scheduler import DEFAULT_BASE_TICK_SECONDS
from climate.weather.store import READING_KINDS, SCHEMA_VERSION, Reading, WeatherStore

__all__ = [
    "API_VERSION",
    "ApiError",
    "ProviderAvailability",
    "WeatherRequestHandler",
    "availability_map",
    "create_server",
    "dispatch",
    "forecast",
    "health",
    "latest",
    "list_locations",
    "list_providers",
    "series",
    "stats",
]

API_VERSION = "v1"
API_PREFIX = "/api/v1"

DEFAULT_STALE_FACTOR = 2

DEFAULT_SERIES_STEP_SECONDS = 300
MIN_SERIES_STEP_SECONDS = 60
DEFAULT_SERIES_POINTS = 2000
MAX_SERIES_POINTS = 10000

#: Hard cap on ``to - from`` for every windowed route — ``/series`` and
#: ``/stats`` (contract section 2.7). Without it a caller names an unbounded
#: window: ``/series``'s ``step`` has no maximum and ``/stats`` without
#: ``bucket`` has no grid at all, so bounding the response alone still leaves
#: the store query unbounded.
MAX_WINDOW_SPAN_SECONDS = 366 * 86400

#: Hard cap on how many stored points one ``/series`` entry may materialize,
#: passed to the store as ``limit`` so a dense window cannot pull an
#: unbounded cursor into memory behind a 10 000-point response.
MAX_SERIES_STORE_POINTS = 100000

DEFAULT_FORECAST_HORIZON_HOURS = 48
MAX_FORECAST_HORIZON_HOURS = 168

MIN_STATS_BUCKET_SECONDS = 300
MAX_STATS_BUCKETS = 1000

#: Hard cap on how many fetch records one ``/stats`` row may materialize.
#: Lower than the ``/series`` cap on purpose: a ``FetchRecord`` carries the
#: provider's raw response body, so these are far from free.
MAX_STATS_FETCH_RECORDS = 20000

#: The HTTP status boundaries ``/stats`` and ``/health`` classify against.
HTTP_NOT_MODIFIED = 304
HTTP_CLIENT_ERROR = 400
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 500

AGG_VALUES = frozenset({"last", "first", "mean", "min", "max"})
KIND_VALUES = frozenset({"observation", "model", "forecast"})

#: Canonical unit id per variable (contract section 4). The table itself
#: lives in :mod:`climate.weather.vocabulary`, the single source shared with
#: the adapters; this name is kept as the API-local spelling of it.
VARIABLE_UNITS: dict[str, str] = vocabulary.VARIABLES

_CAPABILITY_TO_KIND = {
    Capability.CURRENT_OBSERVATION: "observation",
    Capability.CURRENT_MODEL: "model",
    Capability.FORECAST: "forecast",
}

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: Process start, used for ``/health``'s ``started_at`` / ``uptime_seconds``.
#: The handler layer has no other place to keep process-lifetime state, since
#: its signature is pure ``(store, config, providers, query_params, now)``.
_STARTED_AT = datetime.now(UTC)


# --- errors and envelopes --------------------------------------------------


class ApiError(Exception):
    """One error-envelope error (see contract section 3.4)."""

    def __init__(
        self, code: str, message: str, status: int, detail: Mapping[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "status": self.status,
                "detail": self.detail,
            }
        }


def _error_dict(
    code: str, message: str, status: int, detail: Mapping[str, Any] | None = None
) -> dict:
    return ApiError(code, message, status, detail).to_dict()


def _warning(
    code: str, message: str, *, provider: str | None = None, location: str | None = None
) -> dict:
    return {"code": code, "message": message, "provider": provider, "location": location}


def _guarded(handler):
    """Wrap a pure handler so a raised :class:`ApiError` (or bug) becomes a
    ``(status, json_dict)`` result instead of propagating — the outward
    signature every handler promises stays exception-free."""

    @wraps(handler)
    def wrapped(store, config, providers, query_params, now):
        try:
            return handler(store, config, providers, query_params, now)
        except ApiError as exc:
            return exc.status, exc.to_dict()
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            return 500, _error_dict("internal_error", "unexpected server fault", 500, {})

    return wrapped


def _store_call(fn, *args, **kwargs):
    """Call a store method, turning any failure into ``store_unavailable``.

    The merged ``WeatherStore`` protocol has no dedicated "am I reachable"
    signal beyond letting a call raise, so every data-route store access
    goes through here.
    """
    try:
        return fn(*args, **kwargs)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError("store_unavailable", "the weather store is unreachable", 503, {}) from exc


# --- time helpers ------------------------------------------------------------


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(now: datetime, observed_at: datetime | None) -> int | None:
    if observed_at is None:
        return None
    return int((now - observed_at).total_seconds())


def _validate_span(from_dt: datetime, to_dt: datetime, from_raw: str | None) -> int:
    """Order and span-cap one ``from``/``to`` window; return its length.

    Shared by every windowed route (``/series``, ``/stats``): an untrusted,
    unbounded span is what let a single request iterate the whole store.
    """
    if from_dt >= to_dt:
        raise ApiError(
            "invalid_parameter",
            "from must be earlier than to",
            400,
            {"parameter": "from", "value": from_raw},
        )
    span_seconds = int((to_dt - from_dt).total_seconds())
    if span_seconds > MAX_WINDOW_SPAN_SECONDS:
        raise ApiError(
            "invalid_parameter",
            f"to - from must not exceed {MAX_WINDOW_SPAN_SECONDS} seconds "
            f"({MAX_WINDOW_SPAN_SECONDS // 86400} days)",
            400,
            {
                "parameter": "from",
                "value": from_raw if from_raw else _format_time(from_dt),
                "span_seconds": span_seconds,
                "max_span_seconds": MAX_WINDOW_SPAN_SECONDS,
            },
        )
    return span_seconds


def _parse_time_param(name: str, value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(
            "invalid_parameter",
            f"{name} must be an ISO-8601 UTC timestamp",
            400,
            {"parameter": name, "value": value},
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ApiError(
            "invalid_parameter",
            f"{name} must be UTC ('Z' or '+00:00')",
            400,
            {"parameter": name, "value": value},
        )
    return parsed.astimezone(UTC)


# --- query-parameter helpers -------------------------------------------------


def _raw_values(query_params: Mapping[str, Sequence[str]], name: str) -> list[str]:
    return list(query_params.get(name, []))


def get_repeatable(query_params: Mapping[str, Sequence[str]], name: str) -> list[str]:
    """A repeatable parameter, accepting both repeated keys and comma joins."""
    out: list[str] = []
    for raw in _raw_values(query_params, name):
        out.extend(part.strip() for part in raw.split(",") if part.strip())
    return out


def get_str(query_params: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = _raw_values(query_params, name)
    return values[-1] if values else None


def get_bool(query_params: Mapping[str, Sequence[str]], name: str) -> bool | None:
    raw = get_str(query_params, name)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ApiError(
        "invalid_parameter", f"{name} must be a boolean", 400, {"parameter": name, "value": raw}
    )


def get_int(
    query_params: Mapping[str, Sequence[str]],
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    required: bool = False,
) -> int | None:
    raw = get_str(query_params, name)
    if raw is None:
        if required:
            raise ApiError("missing_parameter", f"{name} is required", 400, {"parameter": name})
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(
            "invalid_parameter",
            f"{name} must be an integer",
            400,
            {"parameter": name, "value": raw},
        ) from exc
    if minimum is not None and value < minimum:
        raise ApiError(
            "invalid_parameter",
            f"{name} must be >= {minimum}",
            400,
            {"parameter": name, "value": raw},
        )
    if maximum is not None and value > maximum:
        raise ApiError(
            "invalid_parameter",
            f"{name} must be <= {maximum}",
            400,
            {"parameter": name, "value": raw},
        )
    return value


def _validate_providers(names: Iterable[str], providers: Sequence[WeatherProvider]) -> None:
    known = {provider.id for provider in providers}
    for name in names:
        if name not in known:
            raise ApiError(
                "unknown_provider",
                f"unknown provider {name!r}",
                400,
                {"parameter": "provider", "value": name},
            )


def _validate_locations(names: Iterable[str], config: WeatherConfig) -> None:
    known = set(config.locations)
    for name in names:
        if name not in known:
            raise ApiError(
                "unknown_location",
                f"unknown location {name!r}",
                400,
                {"parameter": "location", "value": name},
            )


def _validate_variables(names: Iterable[str]) -> None:
    """Reject ids that are neither vocabulary rows nor ``x_`` extensions.

    Contract section 4: the vocabulary is shared, not a filter. A provider
    value with no row is stored as ``x_<provider_field>``, so an explicit
    request for one must be honoured — with the unit reported as stored,
    since the table assigns extensions none.
    """
    for name in names:
        if not vocabulary.is_known(name):
            raise ApiError(
                "unknown_variable",
                f"unknown variable {name!r}",
                400,
                {"parameter": "variables", "value": name},
            )


def _validate_kinds(names: Iterable[str], *, allow_forecast: bool) -> None:
    allowed = KIND_VALUES if allow_forecast else KIND_VALUES - {"forecast"}
    for name in names:
        if name not in allowed:
            raise ApiError(
                "unknown_kind", f"unknown kind {name!r}", 400, {"parameter": "kind", "value": name}
            )


def _enabled_provider_ids(
    providers: Sequence[WeatherProvider], availability: Mapping[str, ProviderAvailability]
) -> list[str]:
    return [provider.id for provider in providers if availability[provider.id].enabled]


def _selected_locations(
    query_params: Mapping[str, Sequence[str]], config: WeatherConfig
) -> list[str]:
    """The ``location`` selection: explicit and validated, else every label."""
    requested = get_repeatable(query_params, "location")
    if requested:
        _validate_locations(requested, config)
        return requested
    return sorted(config.locations)


def _selected_providers(
    query_params: Mapping[str, Sequence[str]],
    providers: Sequence[WeatherProvider],
    default_ids: Sequence[str],
) -> list[str]:
    """The ``provider`` selection: explicit and validated, else ``default_ids``."""
    requested = get_repeatable(query_params, "provider")
    if requested:
        _validate_providers(requested, providers)
        return requested
    return list(default_ids)


# --- provider availability ----------------------------------------------------
#
# The web container is deliberately NOT given the provider credentials: only
# the tracker reads ``docker/weather.env``. Evaluating ``provider.availability``
# against *this* process's environment therefore reports a provider that is
# collecting happily as "disabled, no credential" — which is exactly what the
# dashboard showed once the credentials were removed from this service.
#
# The tracker publishes what it sees in its heartbeat, so that snapshot is the
# answer whenever there is one; this process's own evaluation is the fallback,
# and every availability-bearing response says which of the two it used.


class ProviderAvailability(NamedTuple):
    """One provider's effective availability, and where the answer came from.

    ``source`` is :data:`AVAILABILITY_SOURCE_TRACKER` when it came from the
    tracker's heartbeat snapshot (the process that actually holds the
    credentials) and :data:`AVAILABILITY_SOURCE_WEB` when this process had to
    evaluate it against its own environment because no snapshot exists.
    """

    enabled: bool
    reason: str | None
    credential_present: bool
    source: str


#: ``availability_source`` values (contract section 5.2.2).
AVAILABILITY_SOURCE_TRACKER = "tracker"
AVAILABILITY_SOURCE_WEB = "web"

#: A heartbeat older than this is still used — availability changes only on a
#: tracker restart today, so a stale snapshot is far better than this
#: process's credential-blind guess — but its age is surfaced as a warning.
HEARTBEAT_STALE_TICKS = 3
HEARTBEAT_STALE_SECONDS = HEARTBEAT_STALE_TICKS * DEFAULT_BASE_TICK_SECONDS


def _latest_heartbeat(store: WeatherStore) -> Mapping[str, Any] | None:
    """The newest tracker heartbeat, or ``None``.

    Optional store extension (not on the ``WeatherStore`` protocol), and a
    store that is merely unreachable must not fail a route that can still
    answer, so both absence and failure resolve to ``None``.
    """
    latest = getattr(store, "latest_heartbeat", None)
    if not callable(latest):
        return None
    try:
        heartbeat = latest()
    except Exception:  # noqa: BLE001 - availability must never fail a route
        return None
    return heartbeat if isinstance(heartbeat, Mapping) else None


def _heartbeat_age_seconds(heartbeat: Mapping[str, Any] | None, now: datetime) -> int | None:
    at = heartbeat.get("at") if heartbeat else None
    if not isinstance(at, datetime):
        return None
    return _age_seconds(now, at if at.tzinfo is not None else at.replace(tzinfo=UTC))


def _tracker_availability(entry: Any) -> ProviderAvailability | None:
    """One snapshot entry as a :class:`ProviderAvailability`, or ``None``."""
    if not isinstance(entry, Mapping):
        return None
    reason = entry.get("reason")
    present = entry.get("credential_present")
    return ProviderAvailability(
        enabled=bool(entry.get("enabled")),
        reason=str(reason) if reason else None,
        # ``None`` in the snapshot means "this provider needs no credential",
        # which the contract's non-null ``credential_present`` spells ``true``.
        credential_present=True if present is None else bool(present),
        source=AVAILABILITY_SOURCE_TRACKER,
    )


def _web_availability(provider: WeatherProvider, settings: Any) -> ProviderAvailability:
    availability = provider.availability(settings, os.environ)
    env_var = provider.auth.env_var
    return ProviderAvailability(
        enabled=availability.enabled,
        reason=availability.reason,
        credential_present=bool(os.environ.get(env_var)) if env_var else True,
        source=AVAILABILITY_SOURCE_WEB,
    )


def availability_map(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    now: datetime,
) -> tuple[dict[str, ProviderAvailability], int | None]:
    """``({provider_id: ProviderAvailability}, heartbeat_age_seconds)``.

    The tracker's snapshot wins per provider; a provider missing from it (or
    no heartbeat at all) falls back to this process's own evaluation. The age
    is ``None`` when no snapshot was used.
    """
    heartbeat = _latest_heartbeat(store)
    raw = heartbeat.get("providers") if heartbeat else None
    snapshot: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    resolved: dict[str, ProviderAvailability] = {}
    used_snapshot = False
    for provider in providers:
        from_tracker = _tracker_availability(snapshot.get(provider.id))
        used_snapshot = used_snapshot or from_tracker is not None
        resolved[provider.id] = from_tracker or _web_availability(
            provider, config.providers.get(provider.id)
        )
    age = _heartbeat_age_seconds(heartbeat, now) if used_snapshot else None
    return resolved, age


def _heartbeat_warnings(age_seconds: int | None) -> list[dict[str, Any]]:
    """A warning when the availability snapshot in use is older than expected."""
    if age_seconds is None or age_seconds <= HEARTBEAT_STALE_SECONDS:
        return []
    return [
        _warning(
            "store_degraded",
            f"The tracker's availability snapshot is {age_seconds}s old "
            f"(expected within {HEARTBEAT_STALE_SECONDS}s); provider availability "
            "may be out of date.",
        )
    ]


# --- shared reading/provenance builders -------------------------------------


def _threshold_seconds(provider: WeatherProvider, settings: Any, max_age: int | None) -> int:
    if max_age is not None:
        return max_age
    # GAP: Reading carries no provenance.interval_seconds, so the "reading's
    # own interval" half of section 4.3's threshold formula is unavailable;
    # this always falls through to the provider's own configured interval.
    return int(provider.interval_seconds(settings) * DEFAULT_STALE_FACTOR)


def _provenance(reading: Reading) -> dict[str, Any]:
    return {
        "provider": reading.provider,
        "source": reading.source,
        "model": reading.model,
        "model_run_at": _format_time(reading.model_run_at),
        "station": None,  # GAP: no station field on Reading
        "interval_seconds": None,  # GAP: no interval_seconds field on Reading
        "fetch_id": reading.fetch_id,
        "schema_version": reading.schema_version,
    }


def _build_reading(
    reading: Reading,
    provider: WeatherProvider,
    settings: Any,
    max_age: int | None,
    now: datetime,
    variable_filter: Sequence[str] | None,
) -> tuple[dict[str, Any], bool]:
    threshold = _threshold_seconds(provider, settings, max_age)
    age = _age_seconds(now, reading.observed_at)
    stale = reading.observed_at is None or (age is not None and age > threshold)
    provenance = _provenance(reading)

    names = variable_filter if variable_filter else list(reading.values)
    values: dict[str, Any] = {}
    for name in names:
        measurement = reading.values.get(name)
        if measurement is None:
            continue
        values[name] = {
            "value": measurement.value,
            "unit": measurement.unit,
            "original_value": measurement.original_value,
            "original_unit": measurement.original_unit,
            "observed_at": _format_time(reading.observed_at),
            "requested_at": _format_time(reading.requested_at),
            "age_seconds": age,
            "kind": reading.kind,
            "stale": stale,
            "stale_after_seconds": threshold,
            "quality": None,  # GAP: Measurement has no quality field
            "provenance": provenance,
        }

    reading_dict = {
        "provider": reading.provider,
        "location": reading.location,
        "kind": reading.kind,
        "observed_at": _format_time(reading.observed_at),
        "requested_at": _format_time(reading.requested_at),
        "age_seconds": age,
        "stale": stale,
        "stale_after_seconds": threshold,
        "fetch_id": reading.fetch_id,
        "provenance": provenance,
        "values": values,
    }
    return reading_dict, stale


# --- GET /health -------------------------------------------------------------


def _is_ok(status: int | None) -> bool:
    """HTTP 2xx — the one definition of "this fetch succeeded"."""
    return status is not None and 200 <= status < 300


def _probe_store(store: WeatherStore) -> tuple[bool, int | None]:
    start = time.monotonic()
    try:
        store.count_fetches()
    except Exception:  # noqa: BLE001
        return False, None
    return True, int((time.monotonic() - start) * 1000)


def _store_backend(store: WeatherStore) -> str:
    # GAP: WeatherStore declares no "backend" identifier; guessed from the
    # implementation's module/class name.
    module_name = type(store).__module__.lower()
    class_name = type(store).__name__.lower()
    if "mongo" in module_name or "mongo" in class_name:
        return "mongo"
    if "memory" in module_name or "memory" in class_name:
        return "memory"
    return "mongo"


def _store_size_bytes(store: WeatherStore) -> int | None:
    # Optional store extension (not on the WeatherStore protocol).
    size_bytes = getattr(store, "size_bytes", None)
    return size_bytes() if callable(size_bytes) else None


def _tracker_version(store: WeatherStore) -> str | None:
    # The tracker writes a heartbeat carrying its package version; doctor
    # compares it with the host CLI's version to detect a stale image.
    latest_heartbeat = getattr(store, "latest_heartbeat", None)
    heartbeat = latest_heartbeat() if callable(latest_heartbeat) else None
    return str(heartbeat["version"]) if heartbeat and heartbeat.get("version") else None


def _health_store_snapshot(
    store: WeatherStore, now: datetime
) -> tuple[bool, int | None, int | None, dict[str, Any]]:
    """``(reachable, latency_ms, fetch_count, newest_fetch)`` for ``/health``.

    ``/health`` must answer even when the store is broken, so every call
    here is defensive: a raise downgrades ``reachable`` instead of
    propagating.
    """
    reachable, latency_ms = _probe_store(store)
    fetch_count: int | None = None
    newest_overall = None
    if reachable:
        try:
            fetch_count = store.count_fetches()
            newest_overall = store.latest_fetch()
        except Exception:  # noqa: BLE001
            reachable, fetch_count = False, None

    newest_fetch: dict[str, Any] = {"requested_at": None, "age_seconds": None, "provider": None}
    if newest_overall is not None:
        newest_fetch = {
            "requested_at": _format_time(newest_overall.requested_at),
            "age_seconds": _age_seconds(now, newest_overall.requested_at),
            "provider": newest_overall.provider,
        }
    return reachable, latency_ms, fetch_count, newest_fetch


def _newest_success_at(store: WeatherStore, provider_id: str) -> str | None:
    try:
        success = next(
            (r for r in store.iter_fetches(provider=provider_id) if _is_ok(r.status)), None
        )
    except Exception:  # noqa: BLE001
        return None
    return _format_time(success.requested_at) if success is not None else None


def _health_provider_row(
    store: WeatherStore,
    provider: WeatherProvider,
    settings: Any,
    now: datetime,
    *,
    availability: ProviderAvailability,
    reachable: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": provider.id,
        "enabled": availability.enabled,
        "availability_source": availability.source,
        "newest_fetch_at": None,
        "newest_fetch_age_seconds": None,
        "newest_success_at": None,
        "stale": False,
    }
    if not reachable:
        return row
    try:
        newest = store.latest_fetch(provider=provider.id)
    except Exception:  # noqa: BLE001
        newest = None
    if newest is None:
        return row
    age = _age_seconds(now, newest.requested_at)
    threshold = provider.interval_seconds(settings) * DEFAULT_STALE_FACTOR
    row["newest_fetch_at"] = _format_time(newest.requested_at)
    row["newest_fetch_age_seconds"] = age
    row["stale"] = age is not None and age > threshold
    row["newest_success_at"] = _newest_success_at(store, provider.id)
    return row


def _health_status(reachable: bool, fetch_count: int | None, any_enabled_stale: bool) -> str:
    if not reachable:
        return "down"
    if not fetch_count or any_enabled_stale:
        return "degraded"
    return "ok"


@_guarded
def health(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    reachable, latency_ms, fetch_count, newest_fetch = _health_store_snapshot(store, now)
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)
    warnings: list[dict[str, Any]] = _heartbeat_warnings(heartbeat_age)
    provider_rows = []
    any_enabled_stale = False

    for provider in providers:
        settings = config.providers.get(provider.id)
        availability = availability_by_id[provider.id]
        if not availability.enabled and availability.reason:
            warnings.append(
                _warning(
                    "provider_disabled",
                    f"{provider.id} is disabled: {availability.reason}.",
                    provider=provider.id,
                )
            )
        row = _health_provider_row(
            store, provider, settings, now, availability=availability, reachable=reachable
        )
        if availability.enabled and row["stale"]:
            any_enabled_stale = True
        provider_rows.append(row)

    status = _health_status(reachable, fetch_count, any_enabled_stale)

    body = {
        "status": status,
        "generated_at": _format_time(now),
        "api_version": API_VERSION,
        "version": PACKAGE_VERSION,
        "tracker_version": _tracker_version(store),
        "schema_version": SCHEMA_VERSION,
        "started_at": _format_time(_STARTED_AT),
        "uptime_seconds": max(0, int((now - _STARTED_AT).total_seconds())),
        "store": {
            "reachable": reachable,
            "backend": _store_backend(store),
            "latency_ms": latency_ms,
            "fetch_count": fetch_count,
            "size_bytes": _store_size_bytes(store),
        },
        "newest_fetch": newest_fetch,
        "providers": provider_rows,
        "warnings": warnings,
    }
    return 200, body


# --- GET /providers ----------------------------------------------------------


def _provider_fetch_state(store: WeatherStore, provider_id: str) -> dict[str, Any]:
    newest = _store_call(store.latest_fetch, provider=provider_id)
    success = next(
        (r for r in _store_call(store.iter_fetches, provider=provider_id) if _is_ok(r.status)),
        None,
    )
    return {
        "newest_fetch_at": _format_time(newest.requested_at) if newest is not None else None,
        "newest_success_at": _format_time(success.requested_at) if success is not None else None,
        "last_status": newest.status if newest is not None else None,
    }


def _provider_quota(provider: WeatherProvider) -> dict[str, Any]:
    quota = provider.quota
    return {
        "calls_per_day": quota.calls_per_day if quota else None,
        "calls_per_minute": quota.calls_per_minute if quota else None,
        "weight_per_call": quota.call_weight if quota else None,
        "source": quota.source if quota else "",
    }


def _provider_row(
    store: WeatherStore,
    config: WeatherConfig,
    provider: WeatherProvider,
    settings: Any,
    availability: ProviderAvailability,
) -> dict[str, Any]:
    kinds = sorted(
        {_CAPABILITY_TO_KIND[c] for c in provider.capabilities if c in _CAPABILITY_TO_KIND}
    )
    return {
        "provider": provider.id,
        # GAP: no display title on the provider contract; derived from id.
        "title": provider.id.replace("-", " ").title(),
        "kind": kinds[0] if kinds else "model",
        "enabled": availability.enabled,
        "enabled_reason": availability.reason,
        "availability_source": availability.source,
        "auth_required": provider.auth.required,
        "credential_present": availability.credential_present,
        "capabilities": {
            "variables": [],  # GAP: capabilities are kind-level, not variable-level
            "kinds": kinds,
            "forecast_horizon_hours": None,  # GAP: not declared on the contract
            # ASSUMPTION: config has no per-provider location matrix, so every
            # provider is treated as targeting every configured location.
            "locations": sorted(config.locations),
        },
        "freshness": {
            "strategy": str(provider.freshness) if provider.freshness else None,
            "interval_seconds": provider.interval_seconds(settings),
            "expected_update_seconds": None,  # GAP
            "notes": None,  # GAP
        },
        "quota": _provider_quota(provider),
        "attribution": (
            provider.attribution.as_dict()
            if provider.attribution
            else {"text": "", "url": "", "licence": None}
        ),
        "state": _provider_fetch_state(store, provider.id),
    }


@_guarded
def list_providers(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    requested_ids = get_repeatable(query_params, "provider")
    _validate_providers(requested_ids, providers)
    enabled_filter = get_bool(query_params, "enabled")
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)

    rows = []
    for provider in providers:
        if requested_ids and provider.id not in requested_ids:
            continue
        settings = config.providers.get(provider.id)
        availability = availability_by_id[provider.id]
        if enabled_filter is not None and availability.enabled != enabled_filter:
            continue
        rows.append(_provider_row(store, config, provider, settings, availability))

    return 200, {
        "generated_at": _format_time(now),
        "providers": rows,
        "warnings": _heartbeat_warnings(heartbeat_age),
    }


# --- GET /locations -----------------------------------------------------------


@_guarded
def list_locations(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    all_provider_ids = sorted(provider.id for provider in providers)
    kinds = sorted(READING_KINDS)
    rows = []
    for label in sorted(config.locations):
        reading_count = _store_call(store.count_readings, location=label)
        # Per (provider, kind) so each lookup is an index-ordered read of the
        # readings compound index; a location-only latest_reading would sort
        # the label's whole history in memory.
        newest_per_provider = [
            reading
            for reading in (_newest_reading(store, pid, label, kinds) for pid in all_provider_ids)
            if reading is not None
        ]
        newest = max(newest_per_provider, key=lambda r: r.observed_at, default=None)
        rows.append(
            {
                "location": label,
                # ASSUMPTION: no per-location provider matrix in config; every
                # registered provider is reported as configured for every label.
                "providers": all_provider_ids,
                "newest_observed_at": _format_time(newest.observed_at) if newest else None,
                "reading_count": reading_count,
            }
        )
    return 200, {"generated_at": _format_time(now), "locations": rows, "warnings": []}


# --- GET /latest ---------------------------------------------------------------


def _newest_reading(
    store: WeatherStore, pid: str, label: str, kinds: Sequence[str]
) -> Reading | None:
    """The newest stored reading for one (provider, location) across ``kinds``."""
    candidates = [
        reading
        for reading in (
            _store_call(store.latest_reading, provider=pid, location=label, kind=kind)
            for kind in kinds
        )
        if reading is not None
    ]
    return max(candidates, key=lambda r: r.observed_at) if candidates else None


def _missing_reason(
    store: WeatherStore, provider: WeatherProvider, availability: ProviderAvailability, label: str
) -> str:
    if not availability.enabled:
        return "provider_disabled"
    has_fetches = _store_call(store.count_fetches, provider=provider.id, location=label) > 0
    return "no_fresh_reading" if has_fetches else "no_data"


@_guarded
def latest(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    requested_locations = _selected_locations(query_params, config)
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)
    provider_ids = _selected_providers(
        query_params, providers, _enabled_provider_ids(providers, availability_by_id)
    )

    requested_variables = get_repeatable(query_params, "variables")
    _validate_variables(requested_variables)

    requested_kinds = get_repeatable(query_params, "kind") or ["observation", "model"]
    _validate_kinds(requested_kinds, allow_forecast=False)

    max_age = get_int(query_params, "max_age", minimum=1)

    provider_by_id = {provider.id: provider for provider in providers}
    readings_out: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    any_stale = False

    for pid in provider_ids:
        provider = provider_by_id[pid]
        settings = config.providers.get(pid)
        for label in requested_locations:
            reading = _newest_reading(store, pid, label, requested_kinds)
            if reading is None:
                reason = _missing_reason(store, provider, availability_by_id[pid], label)
                missing.append({"provider": pid, "location": label, "reason": reason})
                continue
            reading_dict, stale = _build_reading(
                reading, provider, settings, max_age, now, requested_variables
            )
            any_stale = any_stale or stale
            readings_out.append(reading_dict)

    readings_out.sort(key=lambda row: (row["provider"], row["location"]))
    warnings: list[dict[str, Any]] = _heartbeat_warnings(heartbeat_age)
    if not readings_out:
        warnings.append(_warning("no_data", "No readings matched the request."))

    return 200, {
        "generated_at": _format_time(now),
        "stale": any_stale,
        "max_age_seconds": max_age,
        "readings": readings_out,
        "missing": missing,
        "warnings": warnings,
    }


# --- GET /series ---------------------------------------------------------------


def _grid_bounds(from_dt: datetime, to_dt: datetime, step: int) -> tuple[int, int]:
    """``(first_epoch, bucket_count)`` for the epoch-aligned grid — arithmetic only.

    Counting the buckets *before* materializing them is what keeps
    ``/series`` bounded: a multi-year window at the 60 s minimum step names
    millions of buckets, and the old loop allocated every one of them before
    ``max_points`` was applied (contract section 2.7's ceiling arrived far
    too late to protect the worker).
    """
    from_epoch = int(from_dt.timestamp())
    to_epoch = int(to_dt.timestamp())
    first = -(-from_epoch // step) * step  # first multiple of step at or after from
    count = max(0, -(-(to_epoch - first) // step))  # ceil((to - first) / step)
    return first, count


def _build_grid(
    from_dt: datetime, to_dt: datetime, step: int, *, max_points: int | None = None
) -> list[datetime]:
    """The grid instants, never more than ``max_points`` of them."""
    first, count = _grid_bounds(from_dt, to_dt, step)
    if max_points is not None:
        count = min(count, max_points)
    return [datetime.fromtimestamp(first + index * step, tz=UTC) for index in range(count)]


def _aggregate(bucket: list, agg: str):
    ordered = sorted(bucket, key=lambda point: point.observed_at)
    if agg == "first":
        point = ordered[0]
        return point.value, point.observed_at, point.fetch_id
    if agg == "last":
        point = ordered[-1]
        return point.value, point.observed_at, point.fetch_id
    values = [point.value for point in ordered]
    if agg == "mean":
        value = sum(values) / len(values)
    elif agg == "min":
        value = min(values)
    else:
        value = max(values)
    return value, ordered[-1].observed_at, None


def _build_series_entry(
    variable: str,
    provider: str,
    location: str,
    kind: str,
    grid: list[datetime],
    step: int,
    agg: str,
    points: list,
) -> dict[str, Any]:
    buckets: dict[int, list] = defaultdict(list)
    for point in points:
        buckets[int(point.observed_at.timestamp()) // step].append(point)

    grid_points = []
    for t in grid:
        bucket = buckets.get(int(t.timestamp()) // step, [])
        if not bucket:
            grid_points.append(
                {"t": _format_time(t), "value": None, "observed_at": None, "fetch_id": None}
            )
            continue
        value, observed_at, fetch_id = _aggregate(bucket, agg)
        grid_points.append(
            {
                "t": _format_time(t),
                "value": value,
                "observed_at": _format_time(observed_at),
                "fetch_id": fetch_id,
            }
        )

    non_null = [gp for gp in grid_points if gp["value"] is not None]
    numeric_values = [gp["value"] for gp in non_null if isinstance(gp["value"], (int, float))]
    sources = {point.source for point in points}
    models = {point.model for point in points}

    return {
        "provider": provider,
        "location": location,
        "kind": kind,
        "unit": points[0].unit if points else VARIABLE_UNITS.get(variable),
        "source": next(iter(sources)) if len(sources) == 1 else None,
        "model": next(iter(models)) if len(models) == 1 else None,
        "station": None,  # GAP: SeriesPoint has no station field
        "value_count": len(non_null),
        "null_count": len(grid_points) - len(non_null),
        "first_at": non_null[0]["t"] if non_null else None,
        "last_at": non_null[-1]["t"] if non_null else None,
        "min": min(numeric_values) if numeric_values else None,
        "max": max(numeric_values) if numeric_values else None,
        "points": grid_points,
    }


def _series_window(
    query_params: Mapping[str, Sequence[str]], now: datetime
) -> tuple[datetime, datetime]:
    """The requested ``from``/``to``, ordered and span-capped."""
    to_raw = get_str(query_params, "to")
    to_dt = _parse_time_param("to", to_raw) if to_raw else now
    from_raw = get_str(query_params, "from")
    from_dt = _parse_time_param("from", from_raw) if from_raw else to_dt - timedelta(hours=24)
    _validate_span(from_dt, to_dt, from_raw)
    return from_dt, to_dt


def _series_agg(query_params: Mapping[str, Sequence[str]]) -> str:
    agg = get_str(query_params, "agg") or "last"
    if agg not in AGG_VALUES:
        raise ApiError(
            "invalid_parameter",
            "agg must be one of last, first, mean, min, max",
            400,
            {"parameter": "agg", "value": agg},
        )
    return agg


def _series_entries(
    store: WeatherStore,
    variable: str,
    *,
    provider_ids: Sequence[str],
    locations: Sequence[str],
    kinds: Sequence[str],
    since: datetime,
    until: datetime,
    grid: list[datetime],
    step: int,
    agg: str,
) -> tuple[list[dict[str, Any]], bool]:
    """One entry per (provider, location, kind) with data, plus "hit the cap"."""
    entries: list[dict[str, Any]] = []
    capped = False
    for pid, label, kind in product(provider_ids, locations, kinds):
        points = _store_call(
            store.series,
            variable,
            provider=pid,
            location=label,
            kind=kind,
            since=since,
            until=until,
            limit=MAX_SERIES_STORE_POINTS,
        )
        capped = capped or len(points) >= MAX_SERIES_STORE_POINTS
        points = [point for point in points if point.observed_at < until]
        if not points:
            continue
        entries.append(_build_series_entry(variable, pid, label, kind, grid, step, agg, points))
    return entries, capped


def _series_unit(variable: str, entries: Sequence[Mapping[str, Any]]) -> str:
    """The response-level unit.

    A vocabulary variable has one by definition. An ``x_`` extension does
    not — the table assigns extensions no unit — so its unit is reported as
    stored, falling back to the documented ``other``.
    """
    canonical = vocabulary.unit_for(variable)
    if canonical is not None:
        return canonical
    stored = {entry["unit"] for entry in entries if entry["unit"]}
    return stored.pop() if len(stored) == 1 else vocabulary.FALLBACK_UNIT


@_guarded
def series(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    variable = get_str(query_params, "variable")
    if not variable:
        raise ApiError("missing_parameter", "variable is required", 400, {"parameter": "variable"})
    _validate_variables([variable])

    requested_locations = _selected_locations(query_params, config)
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)
    requested_providers = _selected_providers(
        query_params, providers, _enabled_provider_ids(providers, availability_by_id)
    )
    from_dt, to_dt = _series_window(query_params, now)

    requested_kinds = get_repeatable(query_params, "kind") or ["observation", "model"]
    _validate_kinds(requested_kinds, allow_forecast=True)

    step = get_int(
        query_params, "step", default=DEFAULT_SERIES_STEP_SECONDS, minimum=MIN_SERIES_STEP_SECONDS
    )
    agg = _series_agg(query_params)
    max_points = min(
        get_int(query_params, "max_points", default=DEFAULT_SERIES_POINTS, minimum=1),
        MAX_SERIES_POINTS,
    )

    # Size the grid arithmetically first, then build at most ``max_points``
    # of it: the caller's window is never allocated in full (contract 2.7).
    # Truncation keeps the EARLIEST buckets, so the effective window ends at
    # the last kept bucket and the store query is bounded to it too.
    bucket_count = _grid_bounds(from_dt, to_dt, step)[1]
    truncated = bucket_count > max_points
    grid = _build_grid(from_dt, to_dt, step, max_points=max_points)
    effective_to = grid[-1] + timedelta(seconds=step) if truncated and grid else to_dt

    entries, capped = _series_entries(
        store,
        variable,
        provider_ids=requested_providers,
        locations=requested_locations,
        kinds=requested_kinds,
        since=from_dt,
        until=effective_to,
        grid=grid,
        step=step,
        agg=agg,
    )

    warnings: list[dict[str, Any]] = _heartbeat_warnings(heartbeat_age)
    if not entries:
        warnings.append(
            _warning("no_data", f"No stored fetch covers the requested window for {variable}.")
        )
    if truncated:
        warnings.append(
            _warning(
                "truncated",
                f"The requested window holds {bucket_count} buckets at step={step}; "
                f"the earliest {max_points} were returned (max_points, ceiling "
                f"{MAX_SERIES_POINTS}).",
            )
        )
    if capped:
        warnings.append(
            _warning(
                "truncated",
                f"A series matched more than {MAX_SERIES_STORE_POINTS} stored points; "
                "the oldest were used. Narrow the window or raise step.",
            )
        )

    return 200, {
        "generated_at": _format_time(now),
        "variable": variable,
        "unit": _series_unit(variable, entries),
        "from": _format_time(grid[0]) if grid else _format_time(from_dt),
        "to": _format_time(effective_to),
        "step_seconds": step,
        "agg": agg,
        "point_count": len(grid),
        "series": entries,
        "warnings": warnings,
    }


# --- GET /forecast ---------------------------------------------------------------


def _issue_time(points_by_variable: Mapping[str, Sequence[Any]]) -> datetime | None:
    """The provider's own model run time for one fetch, or ``None``.

    Every point of a fetch comes from the same response, so they agree; the
    newest is taken defensively. ``None`` means the provider stated no issue
    time — adapters never substitute the fetch time for it.
    """
    runs = {
        point.model_run_at
        for points in points_by_variable.values()
        for point in points
        if point.model_run_at is not None
    }
    return max(runs) if runs else None


def _select_forecast_issue(
    store: WeatherStore,
    pid: str,
    label: str,
    variables: Sequence[str],
    issued_before: datetime | None,
    now: datetime,
) -> dict[str, Any] | None:
    by_fetch: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for variable in variables:
        # ``since=now``: a point before ``now`` is not a forecast and is
        # excluded from the response anyway, so there is no reason to read the
        # provider's whole stored history to throw it away.
        for point in _store_call(
            store.series, variable, provider=pid, location=label, kind="forecast", since=now
        ):
            by_fetch[point.fetch_id][variable].append(point)
    if not by_fetch:
        return None

    candidates = []
    for fetch_id, points_by_variable in by_fetch.items():
        record = _store_call(store.get_fetch, fetch_id)
        if record is None:
            continue
        # The provider's own model-run time is the issue time. Only when the
        # provider states none does the fetch time stand in for it, and the
        # entry then says so with ``issued_at_estimated``.
        model_run_at = _issue_time(points_by_variable)
        issued_at = model_run_at if model_run_at is not None else record.requested_at
        if issued_before is not None and issued_at > issued_before:
            continue
        candidates.append((issued_at, fetch_id, record, model_run_at, points_by_variable))
    if not candidates:
        return None
    issued_at, fetch_id, record, model_run_at, points_by_variable = max(
        candidates, key=lambda item: item[0]
    )
    return {
        "provider": pid,
        "location": label,
        "issued_at": issued_at,
        "model_run_at": model_run_at,
        "requested_at": record.requested_at,
        "fetch_id": fetch_id,
        "schema_version": record.schema_version,
        "points_by_variable": points_by_variable,
    }


def _thin_to_step(points: Sequence[Any], step_seconds: int) -> list[Any]:
    """Thin one variable's points to ``step_seconds`` on the WALL-CLOCK grid.

    Not relative to the issue time: a real fetch happens at e.g. 19:33:37, so
    no top-of-the-hour forecast point is ever a whole number of steps after it
    (found live: every forecast came back with 0 points). A series that sits
    off the wall-clock grid entirely is thinned from its own first point
    instead, so the filter can never silently empty it.
    """
    kept = [p for p in points if int(p.observed_at.timestamp()) % step_seconds == 0]
    if kept or not points:
        return kept
    anchor = min(p.observed_at for p in points)
    return [p for p in points if int((p.observed_at - anchor).total_seconds()) % step_seconds == 0]


def _build_forecast_entry(
    group: dict[str, Any], horizon_hours: int, step_hours: int, now: datetime
) -> dict[str, Any]:
    issued_at = group["issued_at"]
    # The horizon is measured from the REQUEST, not from the issue: an issue
    # fetched hours ago still carries valid future points, and measuring from
    # its own issue time threw them away. Points already in the past are not
    # a forecast, so they are excluded.
    horizon_end = now + timedelta(hours=horizon_hours)
    step_seconds = step_hours * 3600

    lookup: dict[str, dict[datetime, Any]] = {}
    units: dict[str, str] = {}
    valid_ats: set[datetime] = set()
    sources: set[str] = set()
    models: set[Any] = set()
    for variable, points in group["points_by_variable"].items():
        in_horizon = [p for p in points if now <= p.observed_at <= horizon_end]
        lookup[variable] = {}
        for point in _thin_to_step(in_horizon, step_seconds):
            lookup[variable][point.observed_at] = point.value
            units.setdefault(variable, point.unit)
            valid_ats.add(point.observed_at)
            sources.add(point.source)
            models.add(point.model)

    present_variables = sorted(variable for variable, values in lookup.items() if values)
    points_out = [
        {
            "valid_at": _format_time(valid_at),
            "lead_seconds": int((valid_at - issued_at).total_seconds()),
            "values": {variable: lookup[variable].get(valid_at) for variable in present_variables},
        }
        for valid_at in sorted(valid_ats)
    ]

    return {
        "provider": group["provider"],
        "location": group["location"],
        "kind": "forecast",
        "issued_at": _format_time(issued_at),
        "issued_at_estimated": group["model_run_at"] is None,
        "requested_at": _format_time(group["requested_at"]),
        "fetch_id": group["fetch_id"],
        "provenance": {
            "provider": group["provider"],
            "source": next(iter(sources)) if len(sources) == 1 else (next(iter(sources), "") or ""),
            "model": next(iter(models)) if len(models) == 1 else None,
            "model_run_at": _format_time(group["model_run_at"]),
            "station": None,  # GAP: no station field on Reading
            "interval_seconds": None,  # GAP: no interval_seconds field on Reading
            "fetch_id": group["fetch_id"],
            "schema_version": group["schema_version"],
        },
        "variables": present_variables,
        "units": {
            variable: vocabulary.unit_for(variable) or units.get(variable, vocabulary.FALLBACK_UNIT)
            for variable in present_variables
        },
        "point_count": len(points_out),
        "points": points_out,
    }


@_guarded
def forecast(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    requested_locations = _selected_locations(query_params, config)
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)
    requested_providers = _selected_providers(
        query_params,
        providers,
        [
            provider.id
            for provider in providers
            if Capability.FORECAST in provider.capabilities
            and availability_by_id[provider.id].enabled
        ],
    )

    requested_variables = get_repeatable(query_params, "variables")
    _validate_variables(requested_variables)
    # The default is the vocabulary: an ``x_`` extension has no enumerable id,
    # so it is returned when it is asked for by name.
    variables_to_query = requested_variables or sorted(vocabulary.VARIABLES)

    issued_raw = get_str(query_params, "issued_at")
    issued_before = _parse_time_param("issued_at", issued_raw) if issued_raw else None
    horizon_hours = get_int(
        query_params,
        "horizon_hours",
        default=DEFAULT_FORECAST_HORIZON_HOURS,
        minimum=1,
        maximum=MAX_FORECAST_HORIZON_HOURS,
    )
    step_hours = get_int(query_params, "step_hours", default=1, minimum=1)

    forecasts_out = []
    for pid in requested_providers:
        for label in requested_locations:
            group = _select_forecast_issue(
                store, pid, label, variables_to_query, issued_before, now
            )
            if group is None:
                continue
            forecasts_out.append(_build_forecast_entry(group, horizon_hours, step_hours, now))

    warnings: list[dict[str, Any]] = _heartbeat_warnings(heartbeat_age)
    if not forecasts_out:
        warnings.append(_warning("no_data", "No stored forecast matched the request."))

    return 200, {
        "generated_at": _format_time(now),
        "horizon_hours": horizon_hours,
        "step_hours": step_hours,
        "forecasts": forecasts_out,
        "warnings": warnings,
    }


# --- GET /stats ---------------------------------------------------------------


def _parse_window(text: str) -> int:
    match = _DURATION_RE.match(text)
    if not match:
        raise ApiError(
            "invalid_parameter",
            "window must look like 90m, 24h or 7d",
            400,
            {"parameter": "window", "value": text},
        )
    return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]


def _build_stat_buckets(
    records: list, from_dt: datetime, to_dt: datetime, bucket_seconds: int, interval_seconds: int
) -> list:
    """Bucket the window in ONE pass over ``records``.

    Scanning every record once per bucket was ``O(buckets x records)`` — with
    the documented ceilings that is 1000 x 20000 comparisons for a single
    row. Each record instead names its own bucket arithmetically.
    """
    due_per_bucket = int(bucket_seconds // interval_seconds) if interval_seconds else 0
    span = (to_dt - from_dt).total_seconds()
    bucket_count = max(0, -(-int(span) // bucket_seconds))  # ceil

    stored = [0] * bucket_count
    ok = [0] * bucket_count
    for record in records:
        offset = (record.requested_at - from_dt).total_seconds()
        if offset < 0 or offset >= span:
            continue
        index = int(offset) // bucket_seconds
        stored[index] += 1
        ok[index] += 1 if _is_ok(record.status) else 0

    return [
        {
            "t": _format_time(from_dt + timedelta(seconds=index * bucket_seconds)),
            "due_count": due_per_bucket,
            "stored_count": stored[index],
            "ok_count": ok[index],
        }
        for index in range(bucket_count)
    ]


def _stats_window(
    query_params: Mapping[str, Sequence[str]], now: datetime
) -> tuple[datetime, datetime, int]:
    to_raw = get_str(query_params, "to")
    from_raw = get_str(query_params, "from")
    to_dt = _parse_time_param("to", to_raw) if to_raw else now
    if from_raw:
        from_dt = _parse_time_param("from", from_raw)
    else:
        from_dt = to_dt - timedelta(seconds=_parse_window(get_str(query_params, "window") or "24h"))
    return from_dt, to_dt, _validate_span(from_dt, to_dt, from_raw)


def _stats_bucket(query_params: Mapping[str, Sequence[str]], window_seconds: int) -> int | None:
    bucket = get_int(query_params, "bucket", minimum=MIN_STATS_BUCKET_SECONDS)
    if bucket is not None and window_seconds / bucket > MAX_STATS_BUCKETS:
        raise ApiError(
            "invalid_parameter",
            f"window / bucket must not exceed {MAX_STATS_BUCKETS}",
            400,
            {"parameter": "bucket", "value": str(bucket)},
        )
    return bucket


def _status_counts(records: Sequence[Any]) -> dict[str, int]:
    """The per-status tallies of one (provider, location) window."""
    statuses = [record.status for record in records]
    return {
        "ok_count": sum(1 for status in statuses if _is_ok(status)),
        "not_modified_count": sum(1 for status in statuses if status == HTTP_NOT_MODIFIED),
        "client_error_count": sum(
            1
            for status in statuses
            if status is not None
            and HTTP_CLIENT_ERROR <= status < HTTP_SERVER_ERROR
            and status != HTTP_RATE_LIMITED
        ),
        "rate_limited_count": sum(1 for status in statuses if status == HTTP_RATE_LIMITED),
        "server_error_count": sum(
            1 for status in statuses if status is not None and status >= HTTP_SERVER_ERROR
        ),
        "transport_error_count": sum(1 for status in statuses if status is None),
    }


def _cadence(records: Sequence[Any], now: datetime) -> dict[str, Any]:
    """When the fetches in this window happened, and how evenly."""
    ordered = sorted(record.requested_at for record in records)
    gaps = [(ordered[i] - ordered[i - 1]).total_seconds() for i in range(1, len(ordered))]
    newest_fetch_at = ordered[-1] if ordered else None
    return {
        "first_fetch_at": _format_time(ordered[0] if ordered else None),
        "newest_fetch_at": _format_time(newest_fetch_at),
        "newest_fetch_age_seconds": _age_seconds(now, newest_fetch_at),
        "mean_interval_seconds": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "longest_gap_seconds": int(max(gaps)) if gaps else None,
    }


def _stats_row(
    store: WeatherStore,
    provider: WeatherProvider,
    settings: Any,
    label: str | None,
    *,
    availability: ProviderAvailability,
    from_dt: datetime,
    to_dt: datetime,
    window_seconds: int,
    bucket: int | None,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """One ``/stats`` row, plus whether the record read hit its cap.

    ``stored_count`` comes from a *count* query, so it stays exact however
    many records the window holds; only the fields that need the records
    themselves are bounded by ``MAX_STATS_FETCH_RECORDS``.
    """
    pid = provider.id
    stored_count = _store_call(
        store.count_fetches, provider=pid, location=label, since=from_dt, until=to_dt
    )
    records = list(
        _store_call(
            store.iter_fetches,
            provider=pid,
            location=label,
            since=from_dt,
            until=to_dt,
            limit=MAX_STATS_FETCH_RECORDS,
        )
    )
    capped = stored_count > len(records)
    interval_seconds = provider.interval_seconds(settings)
    due_count = int(window_seconds // interval_seconds) if interval_seconds else 0

    row: dict[str, Any] = {
        "provider": pid,
        "location": label,
        "enabled": availability.enabled,
        "freshness_strategy": str(provider.freshness) if provider.freshness else None,
        "interval_seconds": interval_seconds,
        "due_count": due_count,
        "due_estimated": provider.freshness != FreshnessStrategy.INTERVAL,
        "stored_count": stored_count,
        "completeness": round(stored_count / due_count, 4) if due_count else None,
    }
    row.update(_status_counts(records))
    row["reading_count"] = _store_call(
        store.count_readings, provider=pid, location=label, since=from_dt, until=to_dt
    )
    row["bytes_stored"] = sum(len(record.body) for record in records)
    row.update(_cadence(records, now))
    row["buckets"] = (
        _build_stat_buckets(records, from_dt, to_dt, bucket, interval_seconds) if bucket else None
    )
    return row, capped


def _stats_warnings(row: Mapping[str, Any], capped: bool) -> list[dict[str, Any]]:
    pid, label = row["provider"], row["location"]
    warnings = []
    if capped:
        warnings.append(
            _warning(
                "truncated",
                f"{pid} has {row['stored_count']} fetches in this window; the newest "
                f"{MAX_STATS_FETCH_RECORDS} were read. stored_count and completeness are "
                "exact; the status, bytes, cadence and bucket figures cover those records.",
                provider=pid,
                location=label,
            )
        )
    if row["due_estimated"]:
        warnings.append(
            _warning(
                "due_estimated", f"{pid} due counts are estimated.", provider=pid, location=label
            )
        )
    if not row["enabled"]:
        warnings.append(
            _warning("provider_disabled", f"{pid} is disabled.", provider=pid, location=label)
        )
    return warnings


@_guarded
def stats(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    from_dt, to_dt, window_seconds = _stats_window(query_params, now)
    requested_providers = _selected_providers(
        query_params, providers, [provider.id for provider in providers]
    )
    # Unlike the other routes, /stats reports fetch activity even with no
    # configured location at all: ``[None]`` means "any location".
    requested_locations: Sequence[str | None] = _selected_locations(query_params, config) or [None]
    bucket = _stats_bucket(query_params, window_seconds)
    availability_by_id, heartbeat_age = availability_map(store, config, providers, now)

    provider_by_id = {provider.id: provider for provider in providers}
    rows = []
    warnings: list[dict[str, Any]] = _heartbeat_warnings(heartbeat_age)

    for pid, label in product(requested_providers, requested_locations):
        row, capped = _stats_row(
            store,
            provider_by_id[pid],
            config.providers.get(pid),
            label,
            availability=availability_by_id[pid],
            from_dt=from_dt,
            to_dt=to_dt,
            window_seconds=window_seconds,
            bucket=bucket,
            now=now,
        )
        rows.append(row)
        warnings.extend(_stats_warnings(row, capped))

    total_due = sum(row["due_count"] for row in rows)
    total_stored = sum(row["stored_count"] for row in rows)
    total_bytes = sum(row["bytes_stored"] for row in rows)
    totals_completeness = round(total_stored / total_due, 4) if total_due else None

    return 200, {
        "generated_at": _format_time(now),
        "from": _format_time(from_dt),
        "to": _format_time(to_dt),
        "window_seconds": window_seconds,
        "bucket_seconds": bucket,
        "totals": {
            "due_count": total_due,
            "stored_count": total_stored,
            "completeness": totals_completeness,
            "bytes_stored": total_bytes,
        },
        "providers": rows,
        "warnings": warnings,
    }


# --- dispatch and the thin http.server layer ---------------------------------

ROUTES = {
    f"{API_PREFIX}/health": health,
    f"{API_PREFIX}/providers": list_providers,
    f"{API_PREFIX}/locations": list_locations,
    f"{API_PREFIX}/latest": latest,
    f"{API_PREFIX}/series": series,
    f"{API_PREFIX}/forecast": forecast,
    f"{API_PREFIX}/stats": stats,
}


def dispatch(
    method: str,
    path: str,
    query_params: Mapping[str, Sequence[str]],
    *,
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    """Route one API request to its pure handler.

    This is the only "smart" piece of the server layer: method check, route
    lookup, everything else is delegated to a pure handler above.
    """
    if method not in ("GET", "HEAD"):
        return 405, _error_dict(
            "method_not_allowed",
            "The weather API is read-only; only GET and HEAD are supported.",
            405,
            {"method": method, "allow": ["GET", "HEAD"]},
        )
    handler = ROUTES.get(path)
    if handler is None:
        return 404, _error_dict("not_found", f"unknown route {path!r}", 404, {})
    return handler(store, config, providers, query_params, now)


class WeatherRequestHandler(BaseHTTPRequestHandler):
    """Thin ``http.server`` wrapper: bytes on the wire in, ``dispatch`` result
    (or a static file) out. No routing or business logic of its own beyond
    telling API paths apart from static ones and guarding path traversal.

    Class attributes ``store``, ``config``, ``providers`` and ``static_root``
    are bound per-server by :func:`create_server`.
    """

    store: WeatherStore
    config: WeatherConfig
    providers: Sequence[WeatherProvider]
    static_root: Path
    server_version = "climate-weather/1"

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Climate-Api-Version", API_VERSION)
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _serve_static(self, url_path: str) -> None:
        relative = url_path.lstrip("/") or "index.html"
        root = self.static_root.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._write_json(404, _error_dict("not_found", "not found", 404, {}))
            return
        if not candidate.is_file():
            self._write_json(404, _error_dict("not_found", "not found", 404, {}))
            return
        data = candidate.read_bytes()
        content_type, _ = mimetypes.guess_type(str(candidate))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            query = parse_qs(parsed.query, keep_blank_values=False)
            status, body = dispatch(
                self.command,
                parsed.path,
                query,
                store=self.store,
                config=self.config,
                providers=self.providers,
                now=self._now(),
            )
            self._write_json(status, body)
            return
        if self.command not in ("GET", "HEAD"):
            self._write_json(
                405,
                _error_dict(
                    "method_not_allowed",
                    "The weather API is read-only; only GET and HEAD are supported.",
                    405,
                    {"method": self.command, "allow": ["GET", "HEAD"]},
                ),
            )
            return
        self._serve_static(parsed.path)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming convention
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # no per-request access log; nothing here ever carries a coordinate anyway


def create_server(
    *,
    bind: str,
    port: int,
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    static_root: Path,
) -> ThreadingHTTPServer:
    """Build a bound-but-not-started :class:`ThreadingHTTPServer`."""
    handler_cls = type(
        "_BoundWeatherRequestHandler",
        (WeatherRequestHandler,),
        {
            "store": store,
            "config": config,
            "providers": tuple(providers),
            "static_root": Path(static_root),
        },
    )
    return ThreadingHTTPServer((bind, port), handler_cls)
