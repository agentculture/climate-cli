"""Open-Meteo adapter (keyless, model-based forecast).

Open-Meteo's ``/v1/forecast`` endpoint answers with four independent time
blocks in one response: ``current`` (a 900-second model slice for "now"),
``minutely_15``, ``hourly`` and ``daily``. This adapter requests all four in
a single HTTPS call per location (one call, several blocks — that is how
Open-Meteo bills it, see :func:`call_weight` below) and normalizes:

* the ``current`` block into one :class:`~climate.weather.store.Reading` of
  ``kind="model"`` — it is a model's value for the present, not a
  measurement (spec vocabulary, ``docs/weather-api.md`` section 4.2);
* every entry of the ``minutely_15``, ``hourly`` and ``daily`` blocks that
  falls strictly after the ``current`` block's own time, as ``kind="forecast"``
  readings. Each block gets its own ``source`` — ``v1/forecast/minutely_15``,
  ``v1/forecast/hourly``, ``v1/forecast/daily`` (the ``current`` reading uses
  ``v1/forecast/current``) — so a client can tell a 15-minute forecast step
  apart from an hourly or daily one, per ``docs/weather-api.md`` section 3.1's
  definition of ``source`` as the feed name.

**No provider value is dropped** (``docs/weather-api.md`` section 4). Every
numeric or coded variable Open-Meteo actually returns is represented in some
reading, whether or not the shared vocabulary has a row for it: a variable
with a vocabulary row (``temperature_2m`` -> ``temperature``, ``degC``, …) is
normalized under its vocabulary id and unit; a variable with no vocabulary
row (anything not in :data:`_VARIABLE_MAP`) is still emitted, under the id
``x_<open-meteo-variable-name>``, with ``original_value``/``original_unit``
verbatim and ``unit`` set to a matching unit id when the provider's own unit
string is unambiguous, otherwise ``other``. See :func:`_build_values`.

Open-Meteo has no API key. Its documented free-tier budget bills each call
by ``variables_requested * models_requested / 10`` (minimum 1) rather than a
flat "one call" — a single wide request like this adapter's default can cost
several calls. :data:`QUOTA` declares that weight so
:meth:`~climate.weather.providers.base.Quota.daily_calls` accounts for it
correctly. The published numeric limits (600/min, 10 000/day, 300 000/month)
come from issue 5's research rather than an official Open-Meteo page, hence
``verified=False``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Capability,
    FreshnessStrategy,
    LocationLike,
    ProviderSettingsLike,
    Quota,
    RequestSpec,
    WeatherProvider,
)
from climate.weather.store import Measurement, Reading

__all__ = ["OpenMeteoProvider", "call_weight"]

_BASE_URL = "https://api.open-meteo.com/v1/forecast"

#: Default forecast horizon, in hours, when settings carry none.
DEFAULT_FORECAST_HOURS = 48

# The "full AC-relevant variable list": every variable the plan's acceptance
# criteria need, grouped exactly as the fixture (tests/fixtures/README.md)
# was captured with.
DEFAULT_CURRENT_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "uv_index",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "dew_point_2m",
)
DEFAULT_MINUTELY_15_VARIABLES: tuple[str, ...] = ("temperature_2m", "precipitation")
DEFAULT_HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "relative_humidity_2m",
)
DEFAULT_DAILY_VARIABLES: tuple[str, ...] = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "uv_index_max",
)

#: Total variable count of the default request, across all four blocks —
#: what the default :data:`QUOTA` weight is computed from.
_DEFAULT_VARIABLE_COUNT = (
    len(DEFAULT_CURRENT_VARIABLES)
    + len(DEFAULT_MINUTELY_15_VARIABLES)
    + len(DEFAULT_HOURLY_VARIABLES)
    + len(DEFAULT_DAILY_VARIABLES)
)

# Open-Meteo variable name -> (vocabulary variable id, vocabulary unit id).
# Vocabulary per docs/weather-api.md section 4 ("Vocabulary") / 4.1 ("Units").
# Anything Open-Meteo returns that is *not* in this map still gets emitted
# (see _build_values) as ``x_<name>`` — the vocabulary is not a filter.
_VARIABLE_MAP: Mapping[str, tuple[str, str]] = {
    "temperature_2m": ("temperature", "degC"),
    "apparent_temperature": ("apparent_temperature", "degC"),
    "dew_point_2m": ("dew_point", "degC"),
    "relative_humidity_2m": ("relative_humidity", "percent"),
    "pressure_msl": ("pressure_msl", "hPa"),
    "surface_pressure": ("pressure_surface", "hPa"),
    "wind_speed_10m": ("wind_speed", "m_s"),
    "wind_gusts_10m": ("wind_gust", "m_s"),
    "wind_direction_10m": ("wind_direction", "deg"),
    "precipitation": ("precipitation", "mm"),
    "rain": ("rain", "mm"),
    "showers": ("showers", "mm"),
    "snowfall": ("snowfall", "mm"),
    "precipitation_probability": ("precipitation_probability", "percent"),
    "cloud_cover": ("cloud_cover", "percent"),
    "cloud_cover_low": ("cloud_cover_low", "percent"),
    "cloud_cover_mid": ("cloud_cover_medium", "percent"),
    "cloud_cover_high": ("cloud_cover_high", "percent"),
    "shortwave_radiation": ("shortwave_radiation", "w_m2"),
    "direct_radiation": ("direct_radiation", "w_m2"),
    "diffuse_radiation": ("diffuse_radiation", "w_m2"),
    "uv_index": ("uv_index", "index"),
    "uv_index_clear_sky": ("uv_index_clear_sky", "index"),
    "is_day": ("is_day", "index"),
    "weather_code": ("weather_code", "code"),
    "temperature_2m_max": ("temperature_max", "degC"),
    "temperature_2m_min": ("temperature_min", "degC"),
    "precipitation_sum": ("precipitation", "mm"),
}

# Numeric conversions applied before the value reaches the vocabulary unit.
# Open-Meteo reports wind in km/h (vocabulary: m/s) and snowfall in cm
# (vocabulary: mm, per docs/weather-api.md's "converted to millimetres"
# note); every other mapped variable needs only a unit id rename.
_CONVERTERS: Mapping[str, Any] = {
    "wind_speed_10m": lambda value: value / 3.6,
    "wind_gusts_10m": lambda value: value / 3.6,
    "snowfall": lambda value: value * 10,
}

# Open-Meteo's own *_units strings that translate to a vocabulary unit id
# with no numeric conversion — used only for the ``x_`` fallback path, where
# no per-variable converter is known. A unit not in this table (e.g. the
# unconverted "km/h" or "cm") falls back to "other", per docs/weather-api.md
# section 4's rule, rather than guess at a conversion factor.
_UNIT_ID_BY_DISPLAY: Mapping[str, str] = {
    "°C": "degC",
    "%": "percent",
    "hPa": "hPa",
    "mm": "mm",
    "W/m²": "w_m2",
    "°": "deg",
    "": "index",
    "wmo code": "code",
    "m": "m",
}

# Feed-name sources (docs/weather-api.md section 3.1: "source" is the
# provider feed/endpoint by name, never a URL). Distinct per block so a
# 15-minute forecast step is never confused with an hourly or daily one.
_SOURCE_CURRENT = "v1/forecast/current"
_SOURCE_MINUTELY_15 = "v1/forecast/minutely_15"
_SOURCE_HOURLY = "v1/forecast/hourly"
_SOURCE_DAILY = "v1/forecast/daily"


def call_weight(variable_count: int, model_count: int = 1) -> float:
    """Open-Meteo's own call-cost rule: ``variables * models / 10``, min 1.

    A single request naming many variables (as this adapter's default does)
    therefore costs more than one API call — this is what lets the budget
    check in :meth:`~climate.weather.providers.base.Quota.daily_calls`
    count a wide request as several calls rather than one.
    """
    if variable_count <= 0 or model_count <= 0:
        return 1.0
    return max(1.0, (variable_count * model_count) / 10)


QUOTA = Quota(
    calls_per_minute=600,
    calls_per_day=10_000,
    calls_per_month=300_000,
    call_weight=call_weight(_DEFAULT_VARIABLE_COUNT),
    unlimited=False,
    verified=False,
    source="issue-5",
    notes="Numeric limits are issue 5's research, not an official Open-Meteo page.",
)


class OpenMeteoProvider(WeatherProvider):
    """Keyless model/forecast adapter for Open-Meteo's ``/v1/forecast``."""

    id = "open-meteo"
    capabilities = frozenset({Capability.CURRENT_MODEL, Capability.FORECAST, Capability.RADIATION})
    auth = AuthRequirement(required=False)
    default_interval_seconds = 900
    quota = QUOTA
    freshness = FreshnessStrategy.INTERVAL
    attribution = Attribution(
        text="Weather data by Open-Meteo.com (CC BY 4.0)",
        url="https://open-meteo.com/en/license",
        licence="CC BY 4.0",
    )

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
    ) -> Sequence[RequestSpec]:
        """One HTTPS request for ``location`` naming all four blocks.

        Horizon and every block's variable list are overridable through
        ``settings.params``: ``forecast_hours`` (int) and ``current`` /
        ``minutely_15`` / ``hourly`` / ``daily`` (sequences of Open-Meteo
        variable names). Anything left unset keeps the AC-relevant default.
        """
        params = dict(getattr(settings, "params", None) or {})
        forecast_hours = int(params.get("forecast_hours", DEFAULT_FORECAST_HOURS))
        current_vars = tuple(params.get("current", DEFAULT_CURRENT_VARIABLES))
        minutely_vars = tuple(params.get("minutely_15", DEFAULT_MINUTELY_15_VARIABLES))
        hourly_vars = tuple(params.get("hourly", DEFAULT_HOURLY_VARIABLES))
        daily_vars = tuple(params.get("daily", DEFAULT_DAILY_VARIABLES))

        query = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": ",".join(current_vars),
            "minutely_15": ",".join(minutely_vars),
            "hourly": ",".join(hourly_vars),
            "daily": ",".join(daily_vars),
            "forecast_hours": forecast_hours,
            "timezone": "UTC",
        }
        url = f"{_BASE_URL}?{urlencode(query, safe=',')}"
        return (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                purpose="forecast",
                context={
                    "current": current_vars,
                    "minutely_15": minutely_vars,
                    "hourly": hourly_vars,
                    "daily": daily_vars,
                    "forecast_hours": forecast_hours,
                },
            ),
        )

    def normalize(self, fetch_record: Any) -> Sequence[Reading]:
        """Turn one stored fetch into a current reading plus forecast readings.

        Pure and re-derivable (spec ``h2``): the same bytes always give the
        same readings. A failed fetch, a ``304`` or an unparseable body
        yields no readings. ``observed_at`` always comes from the response's
        own ``time`` fields, converted from its reported ``utc_offset_seconds``
        to UTC — never from ``fetch_record.requested_at``.
        """
        if getattr(fetch_record, "status", None) != 200 or not fetch_record.body:
            return ()
        try:
            document = json.loads(fetch_record.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ()

        utc_offset_seconds = int(document.get("utc_offset_seconds") or 0)
        readings: list[Reading] = []

        current_reading = self._current_reading(document, fetch_record, utc_offset_seconds)
        if current_reading is None:
            return ()
        readings.append(current_reading)
        now = current_reading.observed_at

        for block_key, units_key, source in (
            ("minutely_15", "minutely_15_units", _SOURCE_MINUTELY_15),
            ("hourly", "hourly_units", _SOURCE_HOURLY),
            ("daily", "daily_units", _SOURCE_DAILY),
        ):
            readings.extend(
                self._series_readings(
                    document, fetch_record, utc_offset_seconds, now, block_key, units_key, source
                )
            )
        return tuple(readings)

    # -- internals ----------------------------------------------------------

    def _current_reading(
        self,
        document: Mapping[str, Any],
        fetch_record: Any,
        utc_offset_seconds: int,
    ) -> Reading | None:
        current = document.get("current") or {}
        current_units = document.get("current_units") or {}
        time_text = current.get("time")
        if not time_text:
            return None
        observed_at = _to_utc(time_text, utc_offset_seconds)
        values = _build_values(current, current_units)
        if not values:
            return None
        return Reading(
            provider=self.id,
            source=_SOURCE_CURRENT,
            model=None,
            location=fetch_record.location,
            observed_at=observed_at,
            requested_at=fetch_record.requested_at,
            kind="model",
            values=values,
        )

    def _series_readings(
        self,
        document: Mapping[str, Any],
        fetch_record: Any,
        utc_offset_seconds: int,
        now: datetime,
        block_key: str,
        units_key: str,
        source: str,
    ) -> list[Reading]:
        block = document.get(block_key) or {}
        units = document.get(units_key) or {}
        times = block.get("time") or []
        readings: list[Reading] = []
        for index, time_text in enumerate(times):
            observed_at = _to_utc(time_text, utc_offset_seconds)
            if observed_at <= now:
                continue
            values = _build_values(block, units, index=index)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=source,
                    model=None,
                    location=fetch_record.location,
                    observed_at=observed_at,
                    requested_at=fetch_record.requested_at,
                    kind="forecast",
                    values=values,
                )
            )
        return readings


def _to_utc(time_text: str, utc_offset_seconds: int) -> datetime:
    """Open-Meteo's naive local time -> timezone-aware UTC.

    Handles both an ``iso8601`` datetime (``current``/``minutely_15``/
    ``hourly``) and a bare date (``daily``, taken as that day's midnight).
    ``timezone=UTC`` is requested, so ``utc_offset_seconds`` from the
    response is normally ``0``; the conversion still uses the response's
    own reported offset rather than assuming it, in case a caller's
    settings ever request a different ``timezone``.
    """
    if "T" in time_text:
        naive = datetime.fromisoformat(time_text)
    else:
        naive = datetime.combine(date.fromisoformat(time_text), datetime.min.time())
    return (naive - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=UTC)


def _build_values(
    block: Mapping[str, Any],
    units: Mapping[str, Any],
    *,
    index: int | None = None,
) -> dict[str, Measurement]:
    """Map one block's (optionally indexed, for series blocks) variables.

    Every numeric variable Open-Meteo actually returned is represented: a
    variable in :data:`_VARIABLE_MAP` is normalized under its vocabulary id
    and unit (with a converter from :data:`_CONVERTERS` when one applies); a
    variable with no vocabulary row is still emitted, as ``x_<name>``, with
    ``original_value``/``original_unit`` verbatim and a unit id from
    :data:`_UNIT_ID_BY_DISPLAY` when the provider's own unit string is
    unambiguous, otherwise ``"other"`` — the "no provider value is dropped"
    rule (docs/weather-api.md section 4). ``time``/``interval`` are metadata,
    never variables. ``None`` values (a gap in the provider's own data) are
    skipped rather than stored as zero. A value that cannot be coerced to a
    float even after that (Open-Meteo has none today) is skipped too:
    :class:`~climate.weather.store.Measurement` stores ``value`` as a float,
    so a genuinely non-numeric, unmapped variable would be a real contract
    gap, not something to fabricate a number for.
    """
    values: dict[str, Measurement] = {}
    for open_meteo_name, raw in block.items():
        if open_meteo_name in ("time", "interval"):
            continue
        original_value = raw[index] if index is not None else raw
        if original_value is None:
            continue
        original_unit = units.get(open_meteo_name) or None
        mapped = _VARIABLE_MAP.get(open_meteo_name)
        if mapped is not None:
            variable_id, unit_id = mapped
            converter = _CONVERTERS.get(open_meteo_name)
            numeric = converter(original_value) if converter else original_value
        else:
            variable_id = f"x_{open_meteo_name}"
            unit_id = _UNIT_ID_BY_DISPLAY.get(original_unit or "", "other")
            numeric = original_value
        try:
            value = float(numeric)
        except (TypeError, ValueError):
            continue
        values[variable_id] = Measurement(
            value=value,
            unit=unit_id,
            original_value=original_value,
            original_unit=original_unit,
        )
    return values
