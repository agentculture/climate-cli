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

* ``provenance.model_run_at``, ``provenance.station`` and
  ``provenance.interval_seconds`` — :class:`~climate.weather.store.Reading`
  and :class:`~climate.weather.store.Measurement` have no fields for these.
* ``value.quality`` — :class:`~climate.weather.store.Measurement` has no
  quality field.
* ``providers[].capabilities.variables`` and
  ``.forecast_horizon_hours`` — :class:`~climate.weather.providers.base.Capability`
  is kind-level, not variable-level, and the provider contract declares no
  forecast horizon.
* ``providers[].freshness.expected_update_seconds`` and ``.notes`` — no such
  fields on the provider contract.
* ``health.tracker_version`` — no fetch record or reading carries a
  ``climate`` version; the store has no heartbeat API either.
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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from climate import __version__ as PACKAGE_VERSION
from climate.weather.config import WeatherConfig
from climate.weather.providers import Capability, FreshnessStrategy
from climate.weather.providers.base import WeatherProvider
from climate.weather.store import SCHEMA_VERSION, Reading, WeatherStore

__all__ = [
    "API_VERSION",
    "ApiError",
    "WeatherRequestHandler",
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

DEFAULT_FORECAST_HORIZON_HOURS = 48
MAX_FORECAST_HORIZON_HOURS = 168

AGG_VALUES = frozenset({"last", "first", "mean", "min", "max"})
KIND_VALUES = frozenset({"observation", "model", "forecast"})

#: Canonical unit id per variable (contract section 4).
VARIABLE_UNITS: dict[str, str] = {
    "temperature": "degC",
    "apparent_temperature": "degC",
    "dew_point": "degC",
    "relative_humidity": "percent",
    "pressure_msl": "hPa",
    "pressure_surface": "hPa",
    "wind_speed": "m_s",
    "wind_gust": "m_s",
    "wind_direction": "deg",
    "precipitation": "mm",
    "rain": "mm",
    "precipitation_probability": "percent",
    "cloud_cover": "percent",
    "visibility": "m",
    "shortwave_radiation": "w_m2",
    "direct_radiation": "w_m2",
    "diffuse_radiation": "w_m2",
    "uv_index": "index",
    "weather_code": "code",
}

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
    for name in names:
        if name not in VARIABLE_UNITS:
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


def _enabled_provider_ids(providers: Sequence[WeatherProvider], config: WeatherConfig) -> list[str]:
    return [
        provider.id
        for provider in providers
        if provider.availability(config.providers.get(provider.id), os.environ).enabled
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
        "model_run_at": None,  # GAP: no separate model-run timestamp on Reading
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


@_guarded
def health(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    reachable, latency_ms = _probe_store(store)
    fetch_count: int | None = None
    newest_fetch = {"requested_at": None, "age_seconds": None, "provider": None}
    warnings: list[dict[str, Any]] = []

    if reachable:
        try:
            fetch_count = store.count_fetches()
            newest_overall = store.latest_fetch()
        except Exception:  # noqa: BLE001
            reachable = False
            newest_overall = None
    else:
        newest_overall = None

    if newest_overall is not None:
        newest_fetch = {
            "requested_at": _format_time(newest_overall.requested_at),
            "age_seconds": _age_seconds(now, newest_overall.requested_at),
            "provider": newest_overall.provider,
        }

    provider_rows = []
    any_enabled_stale = False
    for provider in providers:
        settings = config.providers.get(provider.id)
        availability = provider.availability(settings, os.environ)
        row: dict[str, Any] = {
            "provider": provider.id,
            "enabled": availability.enabled,
            "newest_fetch_at": None,
            "newest_fetch_age_seconds": None,
            "newest_success_at": None,
            "stale": False,
        }
        if not availability.enabled and availability.reason:
            warnings.append(
                _warning(
                    "provider_disabled",
                    f"{provider.id} is disabled: {availability.reason}.",
                    provider=provider.id,
                )
            )
        if reachable:
            try:
                newest = store.latest_fetch(provider=provider.id)
            except Exception:  # noqa: BLE001
                newest = None
            if newest is not None:
                age = _age_seconds(now, newest.requested_at)
                threshold = provider.interval_seconds(settings) * DEFAULT_STALE_FACTOR
                stale = age is not None and age > threshold
                row["newest_fetch_at"] = _format_time(newest.requested_at)
                row["newest_fetch_age_seconds"] = age
                row["stale"] = stale
                if availability.enabled and stale:
                    any_enabled_stale = True
                try:
                    success = next(
                        (
                            record
                            for record in store.iter_fetches(provider=provider.id)
                            if record.status is not None and 200 <= record.status < 300
                        ),
                        None,
                    )
                except Exception:  # noqa: BLE001
                    success = None
                if success is not None:
                    row["newest_success_at"] = _format_time(success.requested_at)
        provider_rows.append(row)

    if not reachable:
        status = "down"
    elif not fetch_count or any_enabled_stale:
        status = "degraded"
    else:
        status = "ok"

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

    rows = []
    for provider in providers:
        if requested_ids and provider.id not in requested_ids:
            continue
        settings = config.providers.get(provider.id)
        availability = provider.availability(settings, os.environ)
        if enabled_filter is not None and availability.enabled != enabled_filter:
            continue

        kinds = sorted(
            {_CAPABILITY_TO_KIND[c] for c in provider.capabilities if c in _CAPABILITY_TO_KIND}
        )
        primary_kind = kinds[0] if kinds else "model"

        newest_fetch_at = newest_success_at = last_status = None
        newest = _store_call(store.latest_fetch, provider=provider.id)
        if newest is not None:
            newest_fetch_at = _format_time(newest.requested_at)
            last_status = newest.status
        success = next(
            (
                record
                for record in _store_call(store.iter_fetches, provider=provider.id)
                if record.status is not None and 200 <= record.status < 300
            ),
            None,
        )
        if success is not None:
            newest_success_at = _format_time(success.requested_at)

        env_var = provider.auth.env_var
        credential_present = True if not env_var else bool(os.environ.get(env_var))

        rows.append(
            {
                "provider": provider.id,
                # GAP: no display title on the provider contract; derived from id.
                "title": provider.id.replace("-", " ").title(),
                "kind": primary_kind,
                "enabled": availability.enabled,
                "enabled_reason": availability.reason,
                "auth_required": provider.auth.required,
                "credential_present": credential_present,
                "capabilities": {
                    "variables": [],  # GAP: capabilities are kind-level, not variable-level
                    "kinds": kinds,
                    "forecast_horizon_hours": None,  # GAP: not declared on the contract
                    # ASSUMPTION: config has no per-provider location matrix,
                    # so every provider is treated as targeting every
                    # configured location.
                    "locations": sorted(config.locations),
                },
                "freshness": {
                    "strategy": str(provider.freshness) if provider.freshness else None,
                    "interval_seconds": provider.interval_seconds(settings),
                    "expected_update_seconds": None,  # GAP
                    "notes": None,  # GAP
                },
                "quota": {
                    "calls_per_day": provider.quota.calls_per_day if provider.quota else None,
                    "calls_per_minute": provider.quota.calls_per_minute if provider.quota else None,
                    "weight_per_call": provider.quota.call_weight if provider.quota else None,
                    "source": provider.quota.source if provider.quota else "",
                },
                "attribution": (
                    provider.attribution.as_dict()
                    if provider.attribution
                    else {"text": "", "url": "", "licence": None}
                ),
                "state": {
                    "newest_fetch_at": newest_fetch_at,
                    "newest_success_at": newest_success_at,
                    "last_status": last_status,
                },
            }
        )

    return 200, {"generated_at": _format_time(now), "providers": rows, "warnings": []}


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
    rows = []
    for label in sorted(config.locations):
        reading_count = _store_call(store.count_readings, location=label)
        newest = _store_call(store.latest_reading, location=label)
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


@_guarded
def latest(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    requested_locations = get_repeatable(query_params, "location")
    if requested_locations:
        _validate_locations(requested_locations, config)
    else:
        requested_locations = sorted(config.locations)

    requested_providers = get_repeatable(query_params, "provider")
    if requested_providers:
        _validate_providers(requested_providers, providers)
        provider_ids = requested_providers
    else:
        provider_ids = _enabled_provider_ids(providers, config)

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
            candidates = [
                reading
                for reading in (
                    _store_call(store.latest_reading, provider=pid, location=label, kind=kind)
                    for kind in requested_kinds
                )
                if reading is not None
            ]
            reading = max(candidates, key=lambda r: r.observed_at) if candidates else None
            if reading is None:
                availability = provider.availability(settings, os.environ)
                if not availability.enabled:
                    reason = "provider_disabled"
                else:
                    has_fetches = _store_call(store.count_fetches, provider=pid, location=label) > 0
                    reason = "no_fresh_reading" if has_fetches else "no_data"
                missing.append({"provider": pid, "location": label, "reason": reason})
                continue
            reading_dict, stale = _build_reading(
                reading, provider, settings, max_age, now, requested_variables
            )
            if stale:
                any_stale = True
            readings_out.append(reading_dict)

    readings_out.sort(key=lambda row: (row["provider"], row["location"]))
    warnings: list[dict[str, Any]] = []
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


def _build_grid(from_dt: datetime, to_dt: datetime, step: int) -> list[datetime]:
    from_epoch = int(from_dt.timestamp())
    to_epoch = int(to_dt.timestamp())
    first = ((from_epoch + step - 1) // step) * step
    grid = []
    t = first
    while t < to_epoch:
        grid.append(datetime.fromtimestamp(t, tz=UTC))
        t += step
    return grid


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

    requested_locations = get_repeatable(query_params, "location")
    if requested_locations:
        _validate_locations(requested_locations, config)
    else:
        requested_locations = sorted(config.locations)

    requested_providers = get_repeatable(query_params, "provider")
    if requested_providers:
        _validate_providers(requested_providers, providers)
    else:
        requested_providers = _enabled_provider_ids(providers, config)

    to_raw = get_str(query_params, "to")
    to_dt = _parse_time_param("to", to_raw) if to_raw else now
    from_raw = get_str(query_params, "from")
    from_dt = _parse_time_param("from", from_raw) if from_raw else to_dt - timedelta(hours=24)
    if from_dt >= to_dt:
        raise ApiError(
            "invalid_parameter",
            "from must be earlier than to",
            400,
            {"parameter": "from", "value": from_raw},
        )

    requested_kinds = get_repeatable(query_params, "kind") or ["observation", "model"]
    _validate_kinds(requested_kinds, allow_forecast=True)

    step = get_int(
        query_params, "step", default=DEFAULT_SERIES_STEP_SECONDS, minimum=MIN_SERIES_STEP_SECONDS
    )
    agg = get_str(query_params, "agg") or "last"
    if agg not in AGG_VALUES:
        raise ApiError(
            "invalid_parameter",
            "agg must be one of last, first, mean, min, max",
            400,
            {"parameter": "agg", "value": agg},
        )
    max_points = get_int(query_params, "max_points", default=DEFAULT_SERIES_POINTS, minimum=1)
    max_points = min(max_points, MAX_SERIES_POINTS)

    grid = _build_grid(from_dt, to_dt, step)
    truncated = False
    if len(grid) > max_points:
        grid = grid[:max_points]
        truncated = True

    entries = []
    for pid in requested_providers:
        for label in requested_locations:
            for kind in requested_kinds:
                points = _store_call(
                    store.series,
                    variable,
                    provider=pid,
                    location=label,
                    kind=kind,
                    since=from_dt,
                    until=to_dt,
                )
                points = [point for point in points if point.observed_at < to_dt]
                if not points:
                    continue
                entries.append(
                    _build_series_entry(variable, pid, label, kind, grid, step, agg, points)
                )

    warnings: list[dict[str, Any]] = []
    if not entries:
        warnings.append(
            _warning("no_data", f"No stored fetch covers the requested window for {variable}.")
        )
    if truncated:
        warnings.append(_warning("truncated", "The result was truncated to max_points."))

    return 200, {
        "generated_at": _format_time(now),
        "variable": variable,
        "unit": VARIABLE_UNITS[variable],
        "from": _format_time(grid[0]) if grid else _format_time(from_dt),
        "to": _format_time(to_dt),
        "step_seconds": step,
        "agg": agg,
        "point_count": len(grid),
        "series": entries,
        "warnings": warnings,
    }


# --- GET /forecast ---------------------------------------------------------------


def _select_forecast_issue(
    store: WeatherStore,
    pid: str,
    label: str,
    variables: Sequence[str],
    issued_before: datetime | None,
) -> dict[str, Any] | None:
    by_fetch: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for variable in variables:
        for point in _store_call(
            store.series, variable, provider=pid, location=label, kind="forecast"
        ):
            by_fetch[point.fetch_id][variable].append(point)
    if not by_fetch:
        return None

    candidates = []
    for fetch_id, points_by_variable in by_fetch.items():
        record = _store_call(store.get_fetch, fetch_id)
        if record is None:
            continue
        # ASSUMPTION / GAP: Reading has no distinct model-run/issue timestamp,
        # so the fetch's own requested_at stands in for issued_at. This
        # conflates "when the tracker asked" with "when the model was run".
        issued_at = record.requested_at
        if issued_before is not None and issued_at > issued_before:
            continue
        candidates.append((issued_at, fetch_id, record, points_by_variable))
    if not candidates:
        return None
    issued_at, fetch_id, record, points_by_variable = max(candidates, key=lambda item: item[0])
    return {
        "provider": pid,
        "location": label,
        "issued_at": issued_at,
        "requested_at": record.requested_at,
        "fetch_id": fetch_id,
        "schema_version": record.schema_version,
        "points_by_variable": points_by_variable,
    }


def _build_forecast_entry(
    group: dict[str, Any], horizon_hours: int, step_hours: int
) -> dict[str, Any]:
    issued_at = group["issued_at"]
    horizon_end = issued_at + timedelta(hours=horizon_hours)
    step_seconds = step_hours * 3600

    lookup: dict[str, dict[datetime, Any]] = {}
    valid_ats: set[datetime] = set()
    sources: set[str] = set()
    models: set[Any] = set()
    for variable, points in group["points_by_variable"].items():
        lookup[variable] = {}
        for point in points:
            if not (issued_at <= point.observed_at <= horizon_end):
                continue
            lead = int((point.observed_at - issued_at).total_seconds())
            if lead % step_seconds != 0:
                continue
            lookup[variable][point.observed_at] = point.value
            valid_ats.add(point.observed_at)
            sources.add(point.source)
            models.add(point.model)

    present_variables = sorted(variable for variable, values in lookup.items() if values)
    points_out = []
    for valid_at in sorted(valid_ats):
        points_out.append(
            {
                "valid_at": _format_time(valid_at),
                "lead_seconds": int((valid_at - issued_at).total_seconds()),
                "values": {
                    variable: lookup[variable].get(valid_at) for variable in present_variables
                },
            }
        )

    return {
        "provider": group["provider"],
        "location": group["location"],
        "kind": "forecast",
        "issued_at": _format_time(issued_at),
        "requested_at": _format_time(group["requested_at"]),
        "fetch_id": group["fetch_id"],
        "provenance": {
            "provider": group["provider"],
            "source": next(iter(sources)) if len(sources) == 1 else (next(iter(sources), "") or ""),
            "model": next(iter(models)) if len(models) == 1 else None,
            "model_run_at": None,  # GAP
            "station": None,  # GAP
            "interval_seconds": None,  # GAP
            "fetch_id": group["fetch_id"],
            "schema_version": group["schema_version"],
        },
        "variables": present_variables,
        "units": {variable: VARIABLE_UNITS[variable] for variable in present_variables},
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
    requested_locations = get_repeatable(query_params, "location")
    if requested_locations:
        _validate_locations(requested_locations, config)
    else:
        requested_locations = sorted(config.locations)

    requested_providers = get_repeatable(query_params, "provider")
    if requested_providers:
        _validate_providers(requested_providers, providers)
    else:
        requested_providers = [
            provider.id
            for provider in providers
            if Capability.FORECAST in provider.capabilities
            and provider.availability(config.providers.get(provider.id), os.environ).enabled
        ]

    requested_variables = get_repeatable(query_params, "variables")
    _validate_variables(requested_variables)
    variables_to_query = requested_variables or sorted(VARIABLE_UNITS)

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
            group = _select_forecast_issue(store, pid, label, variables_to_query, issued_before)
            if group is None:
                continue
            forecasts_out.append(_build_forecast_entry(group, horizon_hours, step_hours))

    warnings: list[dict[str, Any]] = []
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
    due_per_bucket = int(bucket_seconds // interval_seconds) if interval_seconds else 0
    buckets = []
    t = from_dt
    while t < to_dt:
        bucket_end = min(t + timedelta(seconds=bucket_seconds), to_dt)
        in_bucket = [record for record in records if t <= record.requested_at < bucket_end]
        buckets.append(
            {
                "t": _format_time(t),
                "due_count": due_per_bucket,
                "stored_count": len(in_bucket),
                "ok_count": sum(
                    1
                    for record in in_bucket
                    if record.status is not None and 200 <= record.status < 300
                ),
            }
        )
        t = bucket_end
    return buckets


@_guarded
def stats(
    store: WeatherStore,
    config: WeatherConfig,
    providers: Sequence[WeatherProvider],
    query_params: Mapping[str, Sequence[str]],
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    to_raw = get_str(query_params, "to")
    from_raw = get_str(query_params, "from")
    to_dt = _parse_time_param("to", to_raw) if to_raw else now
    if from_raw:
        from_dt = _parse_time_param("from", from_raw)
    else:
        window_text = get_str(query_params, "window") or "24h"
        from_dt = to_dt - timedelta(seconds=_parse_window(window_text))
    if from_dt >= to_dt:
        raise ApiError(
            "invalid_parameter",
            "from must be earlier than to",
            400,
            {"parameter": "from", "value": from_raw},
        )
    window_seconds = int((to_dt - from_dt).total_seconds())

    requested_providers = get_repeatable(query_params, "provider")
    if requested_providers:
        _validate_providers(requested_providers, providers)
    else:
        requested_providers = [provider.id for provider in providers]

    requested_locations = get_repeatable(query_params, "location")
    if requested_locations:
        _validate_locations(requested_locations, config)
    else:
        requested_locations = sorted(config.locations) or [None]

    bucket = get_int(query_params, "bucket", minimum=300)
    if bucket is not None and window_seconds / bucket > 1000:
        raise ApiError(
            "invalid_parameter",
            "window / bucket must not exceed 1000",
            400,
            {"parameter": "bucket", "value": str(bucket)},
        )

    provider_by_id = {provider.id: provider for provider in providers}
    rows = []
    warnings: list[dict[str, Any]] = []
    total_due = 0
    total_stored = 0
    total_bytes = 0

    for pid in requested_providers:
        provider = provider_by_id[pid]
        settings = config.providers.get(pid)
        for label in requested_locations:
            records = list(
                _store_call(
                    store.iter_fetches, provider=pid, location=label, since=from_dt, until=to_dt
                )
            )
            reading_count = _store_call(
                store.count_readings, provider=pid, location=label, since=from_dt, until=to_dt
            )

            interval_seconds = provider.interval_seconds(settings)
            due_estimated = provider.freshness != FreshnessStrategy.INTERVAL
            due_count = int(window_seconds // interval_seconds) if interval_seconds else 0
            stored_count = len(records)
            ok_count = sum(
                1 for record in records if record.status is not None and 200 <= record.status < 300
            )
            not_modified_count = sum(1 for record in records if record.status == 304)
            client_error_count = sum(
                1
                for record in records
                if record.status is not None and 400 <= record.status < 500 and record.status != 429
            )
            rate_limited_count = sum(1 for record in records if record.status == 429)
            server_error_count = sum(
                1 for record in records if record.status is not None and record.status >= 500
            )
            transport_error_count = sum(1 for record in records if record.status is None)
            bytes_stored = sum(len(record.body) for record in records)
            completeness = round(stored_count / due_count, 4) if due_count else None
            ordered = sorted(record.requested_at for record in records)
            first_fetch_at = ordered[0] if ordered else None
            newest_fetch_at = ordered[-1] if ordered else None
            gaps = [(ordered[i] - ordered[i - 1]).total_seconds() for i in range(1, len(ordered))]
            mean_interval = round(sum(gaps) / len(gaps), 1) if gaps else None
            longest_gap = int(max(gaps)) if gaps else None
            enabled = provider.availability(settings, os.environ).enabled

            rows.append(
                {
                    "provider": pid,
                    "location": label,
                    "enabled": enabled,
                    "freshness_strategy": str(provider.freshness) if provider.freshness else None,
                    "interval_seconds": interval_seconds,
                    "due_count": due_count,
                    "due_estimated": due_estimated,
                    "stored_count": stored_count,
                    "completeness": completeness,
                    "ok_count": ok_count,
                    "not_modified_count": not_modified_count,
                    "client_error_count": client_error_count,
                    "rate_limited_count": rate_limited_count,
                    "server_error_count": server_error_count,
                    "transport_error_count": transport_error_count,
                    "reading_count": reading_count,
                    "bytes_stored": bytes_stored,
                    "first_fetch_at": _format_time(first_fetch_at),
                    "newest_fetch_at": _format_time(newest_fetch_at),
                    "newest_fetch_age_seconds": _age_seconds(now, newest_fetch_at),
                    "mean_interval_seconds": mean_interval,
                    "longest_gap_seconds": longest_gap,
                    "buckets": (
                        _build_stat_buckets(records, from_dt, to_dt, bucket, interval_seconds)
                        if bucket
                        else None
                    ),
                }
            )
            total_due += due_count
            total_stored += stored_count
            total_bytes += bytes_stored
            if due_estimated:
                warnings.append(
                    _warning(
                        "due_estimated",
                        f"{pid} due counts are estimated.",
                        provider=pid,
                        location=label,
                    )
                )
            if not enabled:
                warnings.append(
                    _warning(
                        "provider_disabled", f"{pid} is disabled.", provider=pid, location=label
                    )
                )

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
