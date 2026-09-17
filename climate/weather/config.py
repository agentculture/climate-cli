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
import os
from dataclasses import dataclass, field
from pathlib import Path

from climate.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

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
    interval_seconds: int = 300
    request_params: dict = field(default_factory=dict)
    quota: Quota = field(default_factory=Quota)


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


def _validate_quota(locations: dict[str, Location], providers: dict[str, ProviderSettings]) -> None:
    """Reject a configuration whose demand outruns any provider's quota."""
    location_count = len(locations)
    if location_count == 0:
        return
    for provider_id, settings in providers.items():
        quota = settings.quota
        if quota.calls_per_day is None:
            continue
        interval = max(1, int(settings.interval_seconds))
        ticks_per_day = _SECONDS_PER_DAY / interval
        requests_per_day = location_count * ticks_per_day
        if requests_per_day > quota.calls_per_day:
            raise CliError(
                code=EXIT_USER_ERROR,
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


def _parse_location(label: str, raw: dict, precision: int) -> Location:
    return Location(
        label=label,
        latitude=round_coordinate(raw["latitude"], precision),
        longitude=round_coordinate(raw["longitude"], precision),
    )


def _parse_provider(provider_id: str, raw: dict) -> ProviderSettings:
    quota_raw = raw.get("quota") or {}
    return ProviderSettings(
        provider_id=provider_id,
        enabled=bool(raw.get("enabled", True)),
        interval_seconds=int(raw.get("interval_seconds", 300)),
        request_params=dict(raw.get("request_params") or {}),
        quota=Quota(calls_per_day=quota_raw.get("calls_per_day")),
    )


def load_config(path: str | Path | None = None) -> WeatherConfig:
    """Load and validate the weather config file.

    A missing file is treated as an empty configuration (no locations, no
    providers) rather than an error at this level — callers that require at
    least one location (the tracker) call :func:`ensure_locations_configured`
    or :func:`load_tracker_config`.
    """
    resolved = resolve_config_path(path)
    if resolved.exists():
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    else:
        raw = {}

    precision = int(raw.get("coordinate_precision", DEFAULT_COORDINATE_PRECISION))

    locations = {
        label: _parse_location(label, loc_raw, precision)
        for label, loc_raw in (raw.get("locations") or {}).items()
    }
    providers = {
        provider_id: _parse_provider(provider_id, provider_raw)
        for provider_id, provider_raw in (raw.get("providers") or {}).items()
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
