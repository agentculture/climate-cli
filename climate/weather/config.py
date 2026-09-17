"""User configuration for the weather-tracking service.

Non-secret tracker configuration — labelled locations, per-provider enabled
flag, poll interval, request parameters and quota — lives in a JSON file
outside the repository, under the user's XDG config home by default. The
path is overridable via ``CLIMATE_WEATHER_CONFIG_PATH`` for tests and
alternate setups.

Secrets (API keys/tokens) are never read from this file: they come from
environment variables only (``CLIMATE_IMS_API_TOKEN``,
``CLIMATE_OPENWEATHER_API_KEY``, etc.), validated by the modules that need
them.

There is deliberately no default location anywhere in this module or
package: a tracker with nothing configured must refuse to start rather than
silently watching some hard-coded place.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from climate.cli._errors import EXIT_ENV_ERROR, CliError

# Geographic bounds a coordinate must fall within to be usable.
_MIN_LATITUDE, _MAX_LATITUDE = -90.0, 90.0
_MIN_LONGITUDE, _MAX_LONGITUDE = -180.0, 180.0

# Overrides the resolved config file path. Set by tests and by anyone who
# keeps their config somewhere other than the XDG default.
CONFIG_PATH_ENV_VAR = "CLIMATE_WEATHER_CONFIG_PATH"

# Sub-path under the XDG config home (or its fallback) where the weather
# config file lives by default.
_CONFIG_SUBPATH = ("climate-cli", "weather.json")

DEFAULT_COORDINATE_PRECISION = 2
# MET Norway's terms of use cap request precision at 4 decimals (~11 m);
# nothing in this service ever needs, sends or stores more than that.
MAX_COORDINATE_PRECISION = 4

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class Location:
    """A single labelled place to track, with coordinates already rounded."""

    label: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Quota:
    """A provider's request budget. ``calls_per_day=None`` means unchecked."""

    calls_per_day: int | None = None


@dataclass(frozen=True)
class ProviderSettings:
    """Per-provider configuration: enabled flag, cadence, params, quota."""

    provider_id: str
    enabled: bool = True
    # None means "use the provider adapter's own default interval".
    interval_seconds: int | None = None
    request_params: dict = field(default_factory=dict)
    quota: Quota = field(default_factory=Quota)

    @property
    def params(self) -> dict:
        """Alias matching ``providers.base.ProviderSettingsLike.params``."""
        return self.request_params


@dataclass(frozen=True)
class WeatherConfig:
    """Typed, validated configuration for the weather-tracking service."""

    locations: dict[str, Location] = field(default_factory=dict)
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    coordinate_precision: int = DEFAULT_COORDINATE_PRECISION


def round_coordinate(value: float, precision: int) -> float:
    """Round a coordinate to ``precision`` decimals, clamped to the allowed max.

    ``precision`` is clamped to ``[0, MAX_COORDINATE_PRECISION]`` so no
    caller — however configured — can ever push more than 4 decimal places
    (about 11 m) into a stored or transmitted coordinate.
    """
    clamped = max(0, min(int(precision), MAX_COORDINATE_PRECISION))
    return round(float(value), clamped)


def default_config_path() -> Path:
    """The XDG-config default path, always outside this repository checkout."""
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_home) if xdg_home else Path.home() / ".config"
    return base.joinpath(*_CONFIG_SUBPATH)


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the config file path: explicit arg > env var > XDG default."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    return default_config_path()


# Used only for the config-level quota estimate when the user set no interval;
# the tracker re-validates against each adapter's real default at startup.
_FALLBACK_INTERVAL_SECONDS = 300


def _validate_quota(locations: dict[str, Location], providers: dict[str, ProviderSettings]) -> None:
    """Reject a configuration whose demand outruns any *enabled* provider's quota.

    A disabled provider is never polled, so its quota is irrelevant here —
    the scheduler already treats "disabled" as always valid.
    """
    location_count = len(locations)
    if location_count == 0:
        return
    for provider_id, settings in providers.items():
        if not settings.enabled:
            continue
        quota = settings.quota
        if quota.calls_per_day is None:
            continue
        interval = max(1, int(settings.interval_seconds or _FALLBACK_INTERVAL_SECONDS))
        ticks_per_day = _SECONDS_PER_DAY / interval
        requests_per_day = location_count * ticks_per_day
        if requests_per_day > quota.calls_per_day:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"provider '{provider_id}' quota exceeded: "
                    f"{location_count} location(s) at a {interval}s interval "
                    f"need about {requests_per_day:.0f} calls/day, "
                    f"but its quota is {quota.calls_per_day} calls/day"
                ),
                remediation=(
                    f"reduce the number of locations, increase "
                    f"'{provider_id}' interval_seconds, or lower "
                    f"'{provider_id}' quota expectations in the weather "
                    "config file"
                ),
            )


def _config_error(message: str, remediation: str) -> CliError:
    """Build the one shape every malformed-config problem raises."""
    return CliError(code=EXIT_ENV_ERROR, message=message, remediation=remediation)


def _require_mapping(value: object, path_hint: str) -> dict:
    """Require ``value`` to be a JSON object, naming the offending config path."""
    if not isinstance(value, dict):
        raise _config_error(
            message=f"weather config '{path_hint}' must be a JSON object",
            remediation=f"fix '{path_hint}' in the weather config file to be a JSON object",
        )
    return value


def _positive_int(value: object, path_hint: str) -> int:
    """Require ``value`` to convert to a positive integer, never a bare traceback."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _config_error(
            message=f"weather config '{path_hint}' must be a positive whole number",
            remediation=f"set '{path_hint}' to a positive whole number",
        )
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _config_error(
            message=f"weather config '{path_hint}' must be a positive whole number",
            remediation=f"set '{path_hint}' to a positive whole number",
        ) from None
    if number <= 0:
        raise _config_error(
            message=f"weather config '{path_hint}' must be a positive whole number",
            remediation=f"set '{path_hint}' to a positive whole number",
        )
    return number


def _finite_coordinate(value: object, label: str, field_name: str) -> float:
    """Coerce a location field to a finite float; never echo the value itself."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _config_error(
            message=f"location '{label}': '{field_name}' must be a finite number",
            remediation=f"set a finite numeric '{field_name}' for location '{label}'",
        )
    number = float(value)
    if not math.isfinite(number):
        raise _config_error(
            message=f"location '{label}': '{field_name}' must be a finite number",
            remediation=f"set a finite numeric '{field_name}' for location '{label}'",
        )
    return number


def _validate_coordinate_range(
    value: float, label: str, field_name: str, low: float, high: float
) -> None:
    """Reject a geographically impossible coordinate, naming the field, never the value."""
    if not low <= value <= high:
        raise _config_error(
            message=f"location '{label}': '{field_name}' is outside its valid range",
            remediation=(
                f"set '{field_name}' for location '{label}' within its valid geographic range"
            ),
        )


def _parse_location(label: str, raw: object, precision: int) -> Location:
    raw = _require_mapping(raw, f"locations.{label}")
    missing = [field_name for field_name in ("latitude", "longitude") if field_name not in raw]
    if missing:
        raise _config_error(
            message=f"location '{label}' is missing {' and '.join(missing)}",
            remediation=f"add {' and '.join(missing)} for location '{label}'",
        )

    latitude = _finite_coordinate(raw["latitude"], label, "latitude")
    longitude = _finite_coordinate(raw["longitude"], label, "longitude")
    _validate_coordinate_range(latitude, label, "latitude", _MIN_LATITUDE, _MAX_LATITUDE)
    _validate_coordinate_range(longitude, label, "longitude", _MIN_LONGITUDE, _MAX_LONGITUDE)

    return Location(
        label=label,
        latitude=round_coordinate(latitude, precision),
        longitude=round_coordinate(longitude, precision),
    )


def _parse_quota(raw: dict, provider_id: str) -> Quota:
    quota_raw = raw.get("quota")
    if quota_raw is None:
        return Quota()
    quota_raw = _require_mapping(quota_raw, f"providers.{provider_id}.quota")
    calls_per_day = quota_raw.get("calls_per_day")
    if calls_per_day is None:
        return Quota()
    return Quota(
        calls_per_day=_positive_int(calls_per_day, f"providers.{provider_id}.quota.calls_per_day")
    )


def _parse_provider(provider_id: str, raw: object) -> ProviderSettings:
    raw = _require_mapping(raw, f"providers.{provider_id}")
    interval_raw = raw.get("interval_seconds")
    request_params = raw.get("request_params") or {}
    request_params = _require_mapping(request_params, f"providers.{provider_id}.request_params")
    return ProviderSettings(
        provider_id=provider_id,
        enabled=bool(raw.get("enabled", True)),
        interval_seconds=(
            _positive_int(interval_raw, f"providers.{provider_id}.interval_seconds")
            if interval_raw is not None
            else None
        ),
        request_params=dict(request_params),
        quota=_parse_quota(raw, provider_id),
    )


def _read_raw_config(resolved: Path) -> dict:
    """Read and JSON-decode the config file, never letting a decode error escape as-is."""
    if not resolved.exists():
        return {}
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _config_error(
            message=f"weather config at {resolved} is not valid JSON ({exc})",
            remediation=f"fix the JSON syntax in {resolved}",
        ) from None
    return _require_mapping(raw, "<root>")


def load_config(path: str | Path | None = None) -> WeatherConfig:
    """Load and validate the weather config file.

    A missing file is treated as an empty configuration (no locations, no
    providers) rather than an error at this level — callers that require at
    least one location (the tracker) call :func:`ensure_locations_configured`
    or :func:`load_tracker_config`. Any malformed shape (wrong JSON type,
    missing coordinate, non-numeric interval/quota/precision, an impossible
    coordinate) becomes a :class:`CliError` naming the offending config path
    rather than a traceback.
    """
    resolved = resolve_config_path(path)
    raw = _read_raw_config(resolved)

    precision_raw = raw.get("coordinate_precision", DEFAULT_COORDINATE_PRECISION)
    precision = _positive_int(precision_raw, "coordinate_precision")

    locations_raw = _require_mapping(raw.get("locations") or {}, "locations")
    providers_raw = _require_mapping(raw.get("providers") or {}, "providers")

    locations = {
        label: _parse_location(label, loc_raw, precision)
        for label, loc_raw in locations_raw.items()
    }
    providers = {
        provider_id: _parse_provider(provider_id, provider_raw)
        for provider_id, provider_raw in providers_raw.items()
    }

    _validate_quota(locations, providers)

    return WeatherConfig(
        locations=locations,
        providers=providers,
        coordinate_precision=min(precision, MAX_COORDINATE_PRECISION),
    )


def ensure_locations_configured(config: WeatherConfig) -> WeatherConfig:
    """Require at least one configured location; used by the tracker path.

    Raises a :class:`CliError` (exit 2, environment error) when nothing is
    configured — there is no default place to fall back to.
    """
    if not config.locations:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="no location configured for the weather tracker",
            remediation=(
                "add at least one entry under 'locations' to "
                f"{resolve_config_path()} (see docs/specs/"
                "2026-09-17-weather-tracking-service.md for the shape), or "
                f"set {CONFIG_PATH_ENV_VAR} to point at a config file that has one"
            ),
        )
    return config


def load_tracker_config(path: str | Path | None = None) -> WeatherConfig:
    """Load configuration for the tracker, requiring at least one location."""
    return ensure_locations_configured(load_config(path))
