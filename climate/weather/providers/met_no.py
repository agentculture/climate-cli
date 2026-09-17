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
working), and :meth:`MetNoProvider.conditional_headers` is a small, public
helper a scheduler can call on its own to get the same value without
needing the extra parameter. See this task's final report for exactly how
a scheduler should wire this up.

Coordinates are sent with at most 4 decimal places — MET Norway's terms cap
request precision there — even if a location was configured with more.
"""

from __future__ import annotations

import json
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

# met-no variable id -> (our variable id, our unit id). Every one of these is
# read from ``data.instant.details``; the raw value and met-no's own unit
# (from ``properties.meta.units``) are kept as the reading's original pair.
_INSTANT_VARIABLES: dict[str, tuple[str, str]] = {
    "air_temperature": ("temperature", "degC"),
    "relative_humidity": ("relative_humidity", "percent"),
    "air_pressure_at_sea_level": ("pressure_msl", "hPa"),
    "wind_speed": ("wind_speed", "m_s"),
    "wind_from_direction": ("wind_direction", "deg"),
    "cloud_area_fraction": ("cloud_cover", "percent"),
}


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


def _entry_values(entry: dict[str, Any], units: dict[str, str]) -> dict[str, Measurement]:
    """Build the ``values`` map for one timeseries entry."""
    values: dict[str, Measurement] = {}
    data = entry.get("data") or {}
    instant = (data.get("instant") or {}).get("details") or {}
    for met_key, (variable_id, unit_id) in _INSTANT_VARIABLES.items():
        if met_key not in instant:
            continue
        raw = instant[met_key]
        values[variable_id] = Measurement(
            value=float(raw),
            unit=unit_id,
            original_value=raw,
            original_unit=units.get(met_key),
        )

    next_1h = data.get("next_1_hours") or {}
    precipitation = (next_1h.get("details") or {}).get("precipitation_amount")
    if precipitation is not None:
        values["precipitation"] = Measurement(
            value=float(precipitation),
            unit="mm",
            original_value=precipitation,
            original_unit=units.get("precipitation_amount", "mm"),
        )

    symbol_code = (next_1h.get("summary") or {}).get("symbol_code")
    if symbol_code:
        values["weather_code"] = Measurement(
            value=symbol_code,
            unit="code",
            original_value=symbol_code,
            original_unit=None,
        )
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
    ) -> tuple[RequestSpec, ...]:
        """Describe the one GET this provider ever makes for a location.

        ``last_fetch`` is an addition to the abstract signature (default
        ``None``, so every existing caller keeps working): when the caller
        can supply the previous fetch, its ``Last-Modified`` becomes this
        request's ``If-Modified-Since``. A caller that cannot pass it may
        instead call :meth:`conditional_headers` itself.
        """
        lat = _round_coordinate(location.latitude)
        lon = _round_coordinate(location.longitude)
        url = f"{_ENDPOINT}?lat={lat}&lon={lon}"
        return (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                headers={"User-Agent": _user_agent()},
                if_modified_since=self.conditional_headers(last_fetch),
                purpose="current",
            ),
        )

    @staticmethod
    def conditional_headers(last_fetch: FetchRecord | None) -> str | None:
        """The ``Last-Modified`` of ``last_fetch``, reusable verbatim as
        ``If-Modified-Since`` (both are RFC 7231 dates), or ``None``.

        Public so a scheduler that cannot pass ``last_fetch`` into
        :meth:`build_requests` can still compute this value itself and
        merge it into the :class:`RequestSpec` it gets back.
        """
        if last_fetch is None:
            return None
        headers = getattr(last_fetch, "cache_headers", None)
        if headers is None:
            headers = getattr(last_fetch, "headers", None)
        if not headers:
            return None
        for key, value in headers.items():
            if key.lower() == "last-modified":
                return value
        return None

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
        timeseries = properties.get("timeseries") or []
        units = (properties.get("meta") or {}).get("units") or {}
        location_label = getattr(fetch_record, "location", None) or getattr(
            fetch_record, "location_label", ""
        )

        readings: list[Reading] = []
        for index, entry in enumerate(timeseries):
            observed_at = _parse_time(entry.get("time"))
            if observed_at is None:
                continue
            values = _entry_values(entry, units)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source="locationforecast",
                    model=None,
                    location=location_label,
                    observed_at=observed_at,
                    requested_at=fetch_record.requested_at,
                    kind="model" if index == 0 else "forecast",
                    values=values,
                )
            )
        return tuple(readings)
