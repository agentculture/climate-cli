"""OpenWeather current-weather adapter (task t12).

Wraps the OpenWeather 2.5 current-weather endpoint
(``https://api.openweathermap.org/data/2.5/weather``) only — there is no
forecast or One Call code path here; the plan splits those (if wanted) into
a separate provider/task. One request is issued per configured location,
with ``units=metric`` so the response's temperatures already arrive in
Celsius and speeds in m/s.

The fixture this adapter is tested against,
``tests/fixtures/openweather_current.json``, is **synthesized** from
OpenWeather's published documentation, not captured live: no
``CLIMATE_OPENWEATHER_API_KEY`` was available in this environment. See that
file's note and ``tests/fixtures/README.md`` for detail; the plan records
the first live fetch as the risk that verifies the fixture's shape.

OpenWeather's current-weather response is a blended nowcast, not a raw
station observation, so normalized readings use
``kind="model"`` with ``source="openweather"`` and ``model=None`` (there is
no named model/run identifier to report) — distinct from ``provider``, which
is who served it. ``observed_at`` always comes from the response's own
``dt`` field (epoch seconds, UTC), never from the fetch's ``requested_at``.

Secret handling
----------------
The API key is required (spec: :data:`ENV_VAR`) and is read through
:meth:`~climate.weather.providers.base.WeatherProvider.credential`, from the
``env`` mapping the caller resolved availability against — this module never
reads :data:`os.environ` itself, so the key that decides "enabled" is always
the key that builds the request. It is sent the only way OpenWeather accepts
it: as the ``appid`` query parameter on the live request.
:meth:`OpenWeatherProvider.build_requests` therefore builds a
:class:`~climate.weather.providers.base.RequestSpec` whose ``url`` *does*
carry the live key — the scheduler needs the real URL to make the call, and
``climate.weather.store.FetchRecord`` independently refuses to persist an
endpoint that still carries one (see :func:`climate.weather.store.redact_url`
/ ``SECRET_QUERY_PARAMS``, which already includes ``appid``). Nothing in
this module logs, so there is no log line to redact.

(An earlier version of this note flagged ``RequestSpec.__repr__`` as a leak
for a caller that logged the spec directly; ``providers/base.py`` has since
grown a redacting ``__repr__``, so that gap is closed.)
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Capability,
    FetchRecordLike,
    FreshnessStrategy,
    LocationLike,
    ProviderSettingsLike,
    Quota,
    RequestSpec,
    WeatherProvider,
)
from climate.weather.store import Measurement, Reading

#: Environment variable the live API key is read from. Never stored in
#: config, a fetch record, or a log line.
ENV_VAR = "CLIMATE_OPENWEATHER_API_KEY"

_ENDPOINT = "https://api.openweathermap.org/data/2.5/weather"


class OpenWeatherProvider(WeatherProvider):
    """Adapter for the OpenWeather 2.5 current-weather endpoint."""

    id = "openweather"
    capabilities = frozenset({Capability.CURRENT_MODEL})
    auth = AuthRequirement(
        required=True,
        env_var=ENV_VAR,
        note="OpenWeather API key; free tier, see https://openweathermap.org/appid",
    )
    default_interval_seconds = 600
    quota = Quota(
        calls_per_minute=60,
        calls_per_month=1_000_000,
        verified=False,
        source="issue-5",
    )
    freshness = FreshnessStrategy.INTERVAL
    attribution = Attribution(
        text="Weather data provided by OpenWeather (openweathermap.org)",
        url="https://openweathermap.org/current",
        licence="OpenWeather terms of use",
    )

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Sequence[RequestSpec]:
        """Describe exactly one current-weather request for ``location``.

        The key is read with
        :meth:`~climate.weather.providers.base.WeatherProvider.credential`
        from ``env`` — the *same* mapping
        :meth:`~climate.weather.providers.base.WeatherProvider.availability`
        was resolved against, never :data:`os.environ` directly — so a key
        the caller injects without mutating the process environment builds a
        real request rather than declaring the provider enabled and then
        producing nothing.

        Returns an empty sequence when the API key is not configured —
        this never raises, so a missing key disables the provider instead of
        failing the tick.
        """
        api_key = self.credential(env)
        if not api_key:
            return []
        query = urlencode(
            {
                "lat": location.latitude,
                "lon": location.longitude,
                "units": "metric",
                "appid": api_key,
            }
        )
        url = f"{_ENDPOINT}?{query}"
        return [
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                purpose="current",
            )
        ]

    def normalize(self, fetch_record: FetchRecordLike) -> Sequence[Reading]:
        """Derive one "model" reading from a stored current-weather fetch.

        A transport failure, an HTTP error status, or an empty body (a
        conditional-GET 304, or nothing usable in the payload) yields no
        readings.
        """
        status = fetch_record.status
        body = fetch_record.body
        if status is None or status >= 400 or not body:
            return []
        try:
            payload: dict[str, Any] = _load_json(body)
        except ValueError:
            return []

        observed_at = _observed_at(payload)
        if observed_at is None:
            return []

        values = _extract_values(payload)
        if not values:
            return []

        reading = Reading(
            provider=self.id,
            source=self.id,
            model=None,
            location=fetch_record.location,
            observed_at=observed_at,
            requested_at=fetch_record.requested_at,
            # OpenWeather's current-weather response states no model issue
            # or run time. ``generationtime_ms``-style fields are a server
            # compute duration, and the fetch time is not a model run time,
            # so this stays None rather than claiming a provenance the
            # provider never gave (see docs/weather-api.md, Reading).
            model_run_at=None,
            kind="model",
            values=values,
        )
        return [reading]


def _load_json(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))


def _observed_at(payload: dict[str, Any]) -> datetime | None:
    dt = payload.get("dt")
    if dt is None:
        return None
    try:
        return datetime.fromtimestamp(int(dt), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


#: OpenWeather ``main.*`` key -> (vocabulary variable id, vocabulary unit
#: id). Vocabulary and units per ``docs/weather-api.md`` section 4 / 4.1.
#: ``pressure``/``sea_level``/``grnd_level`` are handled separately (see
#: ``_pressure_values``) because they collectively fill only two vocabulary
#: slots (``pressure_msl``, ``pressure_surface``).
_MAIN_VARIABLE_MAP: dict[str, tuple[str, str]] = {
    "temp": ("temperature", "degC"),
    "feels_like": ("apparent_temperature", "degC"),
    "temp_min": ("temperature_min", "degC"),
    "temp_max": ("temperature_max", "degC"),
    "humidity": ("relative_humidity", "percent"),
}

#: OpenWeather ``wind.*`` key -> (vocabulary variable id, vocabulary unit id).
_WIND_VARIABLE_MAP: dict[str, tuple[str, str]] = {
    "speed": ("wind_speed", "m_s"),
    "gust": ("wind_gust", "m_s"),
    "deg": ("wind_direction", "deg"),
}

#: ``sys`` fields never emitted: ``country`` is part of the place name the
#: API contract forbids (docs/weather-api.md section 1.3); every other
#: ``sys`` field (``type``, ``id``, ``sunrise``, ``sunset``) has no privacy
#: concern and is kept under ``x_sys_<name>``.
_EXCLUDED_SYS_FIELDS = frozenset({"country"})


def _add_measurement(
    values: dict[str, Measurement],
    variable_id: str,
    raw: Any,
    unit: str,
    *,
    original_value: Any = None,
    original_unit: str | None = None,
) -> None:
    try:
        value: Any = float(raw)
    except (TypeError, ValueError):
        value = raw
    values[variable_id] = Measurement(
        value=value,
        unit=unit,
        original_value=original_value,
        original_unit=original_unit,
    )


def _pressure_values(main: dict[str, Any]) -> dict[str, Measurement]:
    """``pressure_msl``/``pressure_surface`` from the ``main`` pressure family.

    ``pressure_msl`` comes from ``sea_level`` when the provider sent it
    (the more precise sea-level figure), falling back to the always-present
    ``pressure`` field otherwise -- the two represent the same physical
    quantity (OpenWeather's own docs: ``pressure`` defaults to the sea-level
    value when no ``sea_level``/``grnd_level`` breakdown is given), so
    ``pressure`` is not additionally re-emitted as an ``x_`` field once one
    of them has filled the ``pressure_msl`` slot. ``pressure_surface`` comes
    from ``grnd_level`` only when present.
    """
    values: dict[str, Measurement] = {}
    sea_level = main.get("sea_level")
    if sea_level is not None:
        _add_measurement(values, "pressure_msl", sea_level, "hPa")
    elif main.get("pressure") is not None:
        _add_measurement(values, "pressure_msl", main["pressure"], "hPa")
    grnd_level = main.get("grnd_level")
    if grnd_level is not None:
        _add_measurement(values, "pressure_surface", grnd_level, "hPa")
    return values


def _mapped_values(
    block: dict[str, Any], variable_map: dict[str, tuple[str, str]]
) -> dict[str, Measurement]:
    """The vocabulary values one payload block carries, per ``variable_map``."""
    values: dict[str, Measurement] = {}
    for key, (variable_id, unit) in variable_map.items():
        raw = block.get(key)
        if raw is not None:
            _add_measurement(values, variable_id, raw, unit)
    return values


def _precipitation_values(
    block: dict[str, Any], canonical_id: str, fallback_prefix: str
) -> dict[str, Measurement]:
    """One precipitation block (``rain``/``snow``) mapped, dropping nothing.

    The ``1h`` window is the vocabulary's own variable (``rain`` /
    ``snowfall`` -- OpenWeather already reports both in millimetres). Every
    other window the provider might send (``3h``, ...) has no vocabulary row
    of its own and is kept under ``x_<prefix>_<window>`` rather than dropped.
    """
    values: dict[str, Measurement] = {}
    for window, raw in block.items():
        if raw is None:
            continue
        variable_id = canonical_id if window == "1h" else f"{fallback_prefix}_{window}"
        _add_measurement(values, variable_id, raw, "mm")
    return values


def _weather_block_values(payload: dict[str, Any]) -> dict[str, Measurement]:
    """``weather[0]``'s code and its human-readable companions."""
    weather_list = payload.get("weather") or []
    first = weather_list[0] if weather_list and isinstance(weather_list[0], dict) else {}
    values: dict[str, Measurement] = {}
    weather_id = first.get("id")
    if weather_id is not None:
        code = str(weather_id)
        values["weather_code"] = Measurement(value=code, unit="code", original_value=code)
    for text_key in ("main", "description", "icon"):
        raw_text = first.get(text_key)
        if raw_text:
            values[f"x_weather_{text_key}"] = Measurement(
                value=str(raw_text), unit="code", original_value=str(raw_text)
            )
    return values


def _top_level_extra_values(payload: dict[str, Any]) -> dict[str, Measurement]:
    """The remaining top-level/``sys`` scalars, under ``x_`` ids.

    No-drop rule (docs/weather-api.md section 4), minus the fields the API
    contract forbids: ``coord``, ``name``, ``sys.country`` and the top-level
    ``id`` (OpenWeather's *city* id for the nearest town -- a GeoNames id
    that resolves to a place name, so it is location data; found on the
    first live payload).
    """
    values: dict[str, Measurement] = {}
    base = payload.get("base")
    if base:
        values["x_base"] = Measurement(value=str(base), unit="code", original_value=str(base))
    for key in ("timezone", "cod"):
        raw = payload.get(key)
        if raw is not None:
            _add_measurement(values, f"x_{key}", raw, "other", original_value=raw)
    for key, raw in (payload.get("sys") or {}).items():
        if key in _EXCLUDED_SYS_FIELDS or raw is None:
            continue
        _add_measurement(values, f"x_sys_{key}", raw, "other", original_value=raw)
    return values


def _extract_values(payload: dict[str, Any]) -> dict[str, Measurement]:
    """Map the payload to vocabulary ids, dropping nothing (section 4).

    Every numeric or coded field OpenWeather actually sent is represented
    under a vocabulary id when one fits, or as ``x_<name>`` otherwise --
    except ``coord.lat``/``coord.lon`` and the place name (``name``,
    ``id``, ``sys.country``), which the API contract forbids emitting
    because location is private data (docs/weather-api.md section 1.3).

    One helper per payload section: ``main`` (plus its pressure family),
    ``wind``, ``clouds``/``visibility``, ``rain``, ``snow``, ``weather[0]``
    and the leftover top-level/``sys`` scalars.
    """
    main = payload.get("main") or {}
    values: dict[str, Measurement] = {}

    values.update(_mapped_values(main, _MAIN_VARIABLE_MAP))
    values.update(_pressure_values(main))
    values.update(_mapped_values(payload.get("wind") or {}, _WIND_VARIABLE_MAP))

    cloud_all = (payload.get("clouds") or {}).get("all")
    if cloud_all is not None:
        _add_measurement(values, "cloud_cover", cloud_all, "percent")

    visibility = payload.get("visibility")
    if visibility is not None:
        _add_measurement(values, "visibility", visibility, "m")

    values.update(_precipitation_values(payload.get("rain") or {}, "rain", "x_rain"))
    values.update(_precipitation_values(payload.get("snow") or {}, "snowfall", "x_snow"))
    values.update(_weather_block_values(payload))
    values.update(_top_level_extra_values(payload))
    return values
