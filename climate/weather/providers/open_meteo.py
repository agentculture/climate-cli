"""Open-Meteo adapter (keyless, model-based forecast).

Open-Meteo's ``/v1/forecast`` endpoint answers with four independent time
blocks in one response: ``current`` (a 900-second model slice for "now"),
``minutely_15``, ``hourly`` and ``daily``. This adapter requests all four in
a single HTTPS call per location (one call, several blocks — that is how
Open-Meteo bills it, see :func:`call_weight` below) and normalizes:

* the ``current`` block into one :class:`~climate.weather.store.Reading` of
  ``kind="model"`` — it is a model's value for the present, not a
  measurement (spec vocabulary, ``docs/weather-api.md`` section 4.2);
* the ``hourly`` block's entries that fall strictly after the ``current``
  block's own time into ``kind="forecast"`` readings. The ``minutely_15``
  and ``daily`` blocks are fetched and stored verbatim in the raw fetch
  record (so they are re-derivable later) but are not turned into readings
  by this adapter yet — nothing in the plan consumes them.

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
from datetime import UTC, datetime, timedelta
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
# ``showers`` and ``snowfall`` have no vocabulary entry and are intentionally
# left out: this adapter still requests them (the fixture's request does),
# but normalize() has nowhere honest to put them.
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
    "precipitation_probability": ("precipitation_probability", "percent"),
    "cloud_cover": ("cloud_cover", "percent"),
    "shortwave_radiation": ("shortwave_radiation", "w_m2"),
    "direct_radiation": ("direct_radiation", "w_m2"),
    "diffuse_radiation": ("diffuse_radiation", "w_m2"),
    "uv_index": ("uv_index", "index"),
    "weather_code": ("weather_code", "code"),
}

# Open-Meteo reports wind in km/h; the vocabulary unit is m/s. Every other
# mapped variable needs no numeric conversion, only a unit id rename.
_CONVERTERS: Mapping[str, Any] = {
    "wind_speed_10m": lambda value: value / 3.6,
    "wind_gusts_10m": lambda value: value / 3.6,
}

#: Default source label: Open-Meteo's own default model family when a
#: request names none (the plan's "upstream model family reported or
#: 'best_match'").
DEFAULT_SOURCE = "best_match"


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
        """Turn one stored fetch into a current reading plus hourly forecasts.

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

        readings.extend(
            self._forecast_readings(
                document, fetch_record, utc_offset_seconds, current_reading.observed_at
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
            source=DEFAULT_SOURCE,
            model=None,
            location=fetch_record.location,
            observed_at=observed_at,
            requested_at=fetch_record.requested_at,
            kind="model",
            values=values,
        )

    def _forecast_readings(
        self,
        document: Mapping[str, Any],
        fetch_record: Any,
        utc_offset_seconds: int,
        now: datetime,
    ) -> list[Reading]:
        hourly = document.get("hourly") or {}
        hourly_units = document.get("hourly_units") or {}
        times = hourly.get("time") or []
        readings: list[Reading] = []
        for index, time_text in enumerate(times):
            observed_at = _to_utc(time_text, utc_offset_seconds)
            if observed_at <= now:
                continue
            values = _build_values(hourly, hourly_units, index=index)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=DEFAULT_SOURCE,
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
    """Open-Meteo's naive ``iso8601`` local time -> timezone-aware UTC.

    ``timezone=UTC`` is requested, so ``utc_offset_seconds`` from the
    response is normally ``0``; the conversion still uses the response's
    own reported offset rather than assuming it, in case a caller's
    settings ever request a different ``timezone``.
    """
    naive = datetime.fromisoformat(time_text)
    return (naive - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=UTC)


def _build_values(
    block: Mapping[str, Any],
    units: Mapping[str, Any],
    *,
    index: int | None = None,
) -> dict[str, Measurement]:
    """Map one block's (optionally indexed, for series blocks) variables.

    Only variables in :data:`_VARIABLE_MAP` are kept — unmapped ones
    (``showers``, ``snowfall``) have no vocabulary entry to normalize into.
    ``None`` values (a gap in the provider's own data) are skipped rather
    than stored as zero.
    """
    values: dict[str, Measurement] = {}
    for open_meteo_name, (variable_id, unit_id) in _VARIABLE_MAP.items():
        if open_meteo_name not in block:
            continue
        raw = block[open_meteo_name]
        original_value = raw[index] if index is not None else raw
        if original_value is None:
            continue
        converter = _CONVERTERS.get(open_meteo_name)
        value = float(converter(original_value) if converter else original_value)
        values[variable_id] = Measurement(
            value=value,
            unit=unit_id,
            original_value=original_value,
            original_unit=units.get(open_meteo_name) or None,
        )
    return values
