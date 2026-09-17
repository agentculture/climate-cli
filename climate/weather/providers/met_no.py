"""MET Norway Locationforecast 2.0 adapter (``met-no``).

Keyless, but MET Norway's terms of use require an identifying ``User-Agent``
and reward conditional GETs: every response carries ``Expires`` (when the
model run is due to be superseded) and ``Last-Modified`` (when this exact
run was produced). This adapter's freshness strategy is
:attr:`~climate.weather.providers.base.FreshnessStrategy.HTTP_EXPIRES` — the
base contract's :meth:`WeatherProvider.is_due` already reads ``Expires``
from the stored fetch record's ``cache_headers`` and applies the interval
floor, so this module does not override it.

The provider contract's :meth:`WeatherProvider.build_requests` is called
with only ``(location, settings)`` — it has no way to see the *previous*
fetch, which is what a conditional GET needs (``If-Modified-Since`` echoes
the last response's ``Last-Modified`` verbatim). Rather than silently
working around that gap, :meth:`MetNoProvider.build_requests` accepts an
additional, optional ``last_fetch`` keyword (default ``None``, so every
existing call site that only passes ``location``/``settings`` keeps
working). The scheduler instead calls :meth:`MetNoProvider.conditional_headers`
itself — a small, public helper returning a header mapping it merges
straight into the request headers it sends — so ``build_requests`` always
sets its ``User-Agent`` header regardless of which path is used.

Model run time
--------------
``properties.meta.updated_at`` is MET Norway's own statement of when the
forecast in this response was produced. It becomes every reading's
:attr:`~climate.weather.store.Reading.model_run_at` — one issue time for the
whole fetch, distinct from ``requested_at`` (when *we* downloaded it) and
from ``observed_at`` (the entry's own valid time). A response without a
usable ``updated_at`` leaves ``model_run_at`` ``None``; the fetch time is
never substituted for a stated model run.

Coordinates are sent with at most 4 decimal places — MET Norway's terms cap
request precision there — even if a location was configured with more.

Every provider value this endpoint returns is kept (docs/weather-api.md
section 4's "no provider value is dropped" rule): a variable with a shared
vocabulary row is emitted under that id; one without gets ``x_<name>`` with
its original value/unit verbatim. ``precipitation_amount`` is the one key
the ``complete`` product repeats across two windows in the same entry
(``next_1_hours`` and ``next_6_hours``) — the hourly one is the vocabulary's
``precipitation``, the 6-hour one is disambiguated as
``x_precipitation_amount_next_6_hours`` so neither is silently dropped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import climate
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
from climate.weather.store import FetchRecord, Measurement, Reading

_ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
_REPO_URL = "https://github.com/agentculture/climate-cli"
_MAX_COORDINATE_DECIMALS = 4

# met-no variable id -> (our vocabulary variable id, our unit id). Read from
# ``data.instant.details``; the raw value and met-no's own unit (from
# ``properties.meta.units``) are kept as the reading's original pair.
_INSTANT_VARIABLES: dict[str, tuple[str, str]] = {
    "air_temperature": ("temperature", "degC"),
    "apparent_air_temperature": ("apparent_temperature", "degC"),
    "dew_point_temperature": ("dew_point", "degC"),
    "relative_humidity": ("relative_humidity", "percent"),
    "air_pressure_at_sea_level": ("pressure_msl", "hPa"),
    "wind_speed": ("wind_speed", "m_s"),
    "wind_speed_of_gust": ("wind_gust", "m_s"),
    "wind_from_direction": ("wind_direction", "deg"),
    "cloud_area_fraction": ("cloud_cover", "percent"),
    "cloud_area_fraction_low": ("cloud_cover_low", "percent"),
    "cloud_area_fraction_medium": ("cloud_cover_medium", "percent"),
    "cloud_area_fraction_high": ("cloud_cover_high", "percent"),
    "fog_area_fraction": ("fog", "percent"),
    "ultraviolet_index_clear_sky": ("uv_index_clear_sky", "index"),
}

# next_6_hours.details variable id -> (our vocabulary variable id, unit id).
_NEXT_6_HOURS_VARIABLES: dict[str, tuple[str, str]] = {
    "air_temperature_max": ("temperature_max", "degC"),
    "air_temperature_min": ("temperature_min", "degC"),
}

# Unit ids from docs/weather-api.md section 4.1 that a fallback x_ variable
# can carry verbatim when met-no's own unit happens to already match one.
_KNOWN_UNIT_IDS = frozenset(
    {"degC", "percent", "hPa", "m_s", "deg", "mm", "w_m2", "m", "index", "code"}
)

# met-no's own unit string (properties.meta.units) -> our unit id, for
# variables that fall back to x_<name> and have no vocabulary row of their
# own but whose unit is still one we recognise.
_UNIT_ALIASES = {
    "celsius": "degC",
    "%": "percent",
    "m/s": "m_s",
    "degrees": "deg",
    "mm": "mm",
    "1": "index",
}


def _fallback_unit(met_no_unit: str | None) -> str:
    if met_no_unit in _KNOWN_UNIT_IDS:
        return met_no_unit  # type: ignore[return-value]
    if met_no_unit in _UNIT_ALIASES:
        return _UNIT_ALIASES[met_no_unit]
    return "other"


def _round_coordinate(value: float) -> str:
    """Round to at most 4 decimals and render without a fixed-width pad."""
    return str(round(float(value), _MAX_COORDINATE_DECIMALS))


def _user_agent() -> str:
    """``climate-cli/<package version> <repo url>`` — MET Norway requires this."""
    return f"climate-cli/{climate.__version__} {_REPO_URL}"


def _parse_time(value: Any) -> datetime | None:
    """Parse one timeseries entry's ``time`` (RFC 3339 UTC, ``Z`` suffix)."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _measurement(raw: Any, unit_id: str, original_unit: str | None) -> Measurement:
    value: Any = raw
    if isinstance(raw, (int, float)):
        value = float(raw)
    return Measurement(value=value, unit=unit_id, original_value=raw, original_unit=original_unit)


def _add_known_or_fallback(
    values: dict[str, Measurement],
    raw_key: str,
    raw: Any,
    known: dict[str, tuple[str, str]],
    units: dict[str, str],
    *,
    fallback_id: str | None = None,
) -> None:
    """Emit ``raw_key`` under its vocabulary id, or ``x_<name>`` when unknown.

    ``fallback_id`` overrides the generated ``x_<raw_key>`` id — used to
    disambiguate a provider key that legitimately repeats across two
    forecast windows in the same entry (``precipitation_amount`` in both
    ``next_1_hours`` and ``next_6_hours``).
    """
    original_unit = units.get(raw_key)
    if fallback_id is None and raw_key in known:
        variable_id, unit_id = known[raw_key]
        values[variable_id] = _measurement(raw, unit_id, original_unit)
        return
    variable_id = fallback_id or f"x_{raw_key}"
    values[variable_id] = _measurement(raw, _fallback_unit(original_unit), original_unit)


def _details(data: dict[str, Any], block: str) -> dict[str, Any]:
    """One block's ``details`` map (``{}`` when the block is absent)."""
    return (data.get(block) or {}).get("details") or {}


def _add_precipitation(
    values: dict[str, Measurement],
    raw: Any,
    units: dict[str, str],
    *,
    disambiguated_id: str,
) -> None:
    """``precipitation_amount`` from one window, never dropping the other.

    The first window to supply it fills the vocabulary's ``precipitation``.
    A second window in the same entry (the ``complete`` product repeats the
    key across ``next_1_hours`` and ``next_6_hours``) is real data too, so it
    is kept under ``disambiguated_id`` rather than overwriting or being
    dropped.
    """
    if "precipitation" not in values:
        values["precipitation"] = _measurement(raw, "mm", units.get("precipitation_amount", "mm"))
        return
    _add_known_or_fallback(
        values, "precipitation_amount", raw, {}, units, fallback_id=disambiguated_id
    )


def _add_window_values(
    values: dict[str, Measurement],
    details: dict[str, Any],
    units: dict[str, str],
    known: dict[str, tuple[str, str]],
    *,
    disambiguated_precipitation_id: str,
) -> None:
    """Every key of one forecast window, precipitation handled specially."""
    for raw_key, raw in details.items():
        if raw_key == "precipitation_amount":
            _add_precipitation(values, raw, units, disambiguated_id=disambiguated_precipitation_id)
            continue
        _add_known_or_fallback(values, raw_key, raw, known, units)


def _weather_code(data: dict[str, Any]) -> Measurement | None:
    """The finest-grained ``symbol_code`` this entry offers, or ``None``."""
    for block in ("next_1_hours", "next_6_hours", "next_12_hours"):
        symbol_code = ((data.get(block) or {}).get("summary") or {}).get("symbol_code")
        if symbol_code:
            return Measurement(
                value=symbol_code, unit="code", original_value=symbol_code, original_unit=None
            )
    return None


def _entry_values(entry: dict[str, Any], units: dict[str, str]) -> dict[str, Measurement]:
    """Build the ``values`` map for one timeseries entry.

    Every numeric/coded key met-no returns is kept (docs/weather-api.md
    section 4): known keys map to their vocabulary id, unknown ones fall
    back to ``x_<name>``.
    """
    values: dict[str, Measurement] = {}
    data = entry.get("data") or {}

    for raw_key, raw in _details(data, "instant").items():
        _add_known_or_fallback(values, raw_key, raw, _INSTANT_VARIABLES, units)

    _add_window_values(
        values,
        _details(data, "next_1_hours"),
        units,
        {},
        disambiguated_precipitation_id="x_precipitation_amount_next_1_hours",
    )
    _add_window_values(
        values,
        _details(data, "next_6_hours"),
        units,
        _NEXT_6_HOURS_VARIABLES,
        disambiguated_precipitation_id="x_precipitation_amount_next_6_hours",
    )
    for raw_key, raw in _details(data, "next_12_hours").items():
        _add_known_or_fallback(values, raw_key, raw, {}, units)

    weather_code = _weather_code(data)
    if weather_code is not None:
        values["weather_code"] = weather_code

    return values


class MetNoProvider(WeatherProvider):
    """MET Norway Locationforecast 2.0 complete — model now + hourly forecast."""

    id = "met-no"
    capabilities = frozenset(
        {Capability.CURRENT_MODEL, Capability.FORECAST, Capability.CONDITIONAL_GET}
    )
    auth = AuthRequirement(required=False)
    default_interval_seconds = 1800
    # MET Norway's terms of use publish no numeric call quota; the honest
    # promise is to honour Expires/If-Modified-Since rather than a budget.
    quota = Quota(
        unlimited=True,
        source="https://api.met.no/doc/TermsOfService",
        notes=(
            "No published numeric call quota; obey the Expires header and "
            "send a conditional GET instead of polling on a fixed budget."
        ),
    )
    freshness = FreshnessStrategy.HTTP_EXPIRES
    attribution = Attribution(
        text="MET Norway",
        url="https://api.met.no/doc/License",
        licence="CC BY 4.0 / NLOD",
    )

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
        last_fetch: FetchRecord | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[RequestSpec, ...]:
        """Describe the one GET this provider ever makes for a location.

        ``last_fetch`` is an addition to the abstract signature (default
        ``None``, so every existing caller keeps working): when the caller
        can supply the previous fetch, its ``Last-Modified`` becomes this
        request's ``If-Modified-Since``. A caller that cannot pass it may
        instead call :meth:`conditional_headers` itself.

        ``env`` is accepted for the contract's sake and ignored: MET Norway
        is keyless, so this adapter never reads a credential.
        """
        del env
        lat = _round_coordinate(location.latitude)
        lon = _round_coordinate(location.longitude)
        url = f"{_ENDPOINT}?lat={lat}&lon={lon}"
        headers = {"User-Agent": _user_agent()}
        if_modified_since = self.conditional_headers(last_fetch).get("If-Modified-Since")
        return (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                headers=headers,
                if_modified_since=if_modified_since,
                purpose="current",
            ),
        )

    @staticmethod
    def conditional_headers(last_fetch: FetchRecord | None) -> dict[str, str]:
        """Header(s) a conditional GET should carry, given the last fetch.

        Returns ``{"If-Modified-Since": <that record's Last-Modified>}``
        when ``last_fetch`` has a usable one, else ``{}`` (including when
        ``last_fetch`` is ``None`` or was an error with no headers). Reads
        ``last_fetch.cache_headers`` (the store's :class:`FetchRecord`
        field name) case-insensitively.

        The scheduler calls this directly and merges the result into the
        request headers it sends — it does not pass ``last_fetch`` into
        :meth:`build_requests`.
        """
        if last_fetch is None:
            return {}
        headers = getattr(last_fetch, "cache_headers", None)
        if headers is None:
            headers = getattr(last_fetch, "headers", None)
        if not headers:
            return {}
        for key, value in headers.items():
            if key.lower() == "last-modified":
                return {"If-Modified-Since": value}
        return {}

    def normalize(self, fetch_record: FetchRecord) -> tuple[Reading, ...]:
        """First timeseries entry is the current model value; the rest forecast."""
        if fetch_record.status is None or fetch_record.status >= 400:
            return ()
        if fetch_record.status == 304 or not fetch_record.body:
            return ()
        try:
            payload = json.loads(fetch_record.body)
        except ValueError:
            return ()

        properties = payload.get("properties") or {}
        meta = properties.get("meta") or {}
        timeseries = properties.get("timeseries") or []
        units = meta.get("units") or {}
        location_label = getattr(fetch_record, "location", None) or getattr(
            fetch_record, "location_label", ""
        )
        # properties.meta.updated_at is met-no's own statement of when this
        # forecast was produced — the model run behind every entry of this
        # response, so it goes on all of them. Absent or unparseable leaves
        # it None; the fetch time is never substituted for it.
        model_run_at = _parse_time(meta.get("updated_at"))

        readings: list[Reading] = []
        for index, entry in enumerate(timeseries):
            reading = self._entry_reading(
                entry,
                units,
                index=index,
                location_label=location_label,
                requested_at=fetch_record.requested_at,
                model_run_at=model_run_at,
            )
            if reading is not None:
                readings.append(reading)
        return tuple(readings)

    def _entry_reading(
        self,
        entry: dict[str, Any],
        units: dict[str, str],
        *,
        index: int,
        location_label: str,
        requested_at: datetime,
        model_run_at: datetime | None,
    ) -> Reading | None:
        """One timeseries entry as a reading, or ``None`` when it carries nothing."""
        observed_at = _parse_time(entry.get("time"))
        if observed_at is None:
            return None
        values = _entry_values(entry, units)
        if not values:
            return None
        return Reading(
            provider=self.id,
            source="locationforecast/2.0/complete",
            model=None,
            location=location_label,
            observed_at=observed_at,
            requested_at=requested_at,
            model_run_at=model_run_at,
            kind="model" if index == 0 else "forecast",
            values=values,
        )
