"""aviationweather.gov METAR adapter — keyless airport observations.

Fetches the raw METAR/decoded-JSON feed for a set of configured ICAO station
codes (spec ``c8``). There is no default station anywhere in this module: a
user configures ``params["stations"]`` or the adapter simply has nothing to
ask for, and :meth:`MetarProvider.build_requests` returns no requests at all.

Station selection is **per location**
-------------------------------------
Scheduling is per configured location, so the stations are too.
``params["stations"]`` accepts either shape:

``{"<location label>": ["LLBG", ...], ...}`` (a mapping)
    The precise form. Each location requests — and therefore stores readings
    for — only the stations listed under its own label. A configured
    location with no entry in the mapping (or an empty one) emits **no
    request at all**, rather than silently inheriting another location's
    stations.
``["LLBG", ...]`` (a plain list)
    The documented single-location convenience: the list serves *every*
    configured location. It is kept so that a single-location config stays a
    one-liner, but with more than one location every location then fetches
    and stores the same stations — use the mapping form instead.

Before this was per location, one global list was fetched once per location
and every station's report was stored under each location's label.

One HTTP request covers every station selected for that location
(``ids=A,B,C``), so the adapter still costs one request per location per
tick (the base :meth:`~climate.weather.providers.base.WeatherProvider\
.requests_per_tick` of ``1``);
:meth:`MetarProvider.normalize` turns each object in the JSON array response
into one :class:`~climate.weather.store.Reading` of
``kind="observation"``, keyed as ``metar/<ICAO>`` so multiple stations never
collide. ``observed_at`` always comes from the report's own ``reportTime``
(UTC), never from the fetch's ``requested_at``.

Values use ``docs/weather-api.md`` section 4's shared vocabulary
(``temperature``, ``dew_point``, ``wind_direction``, ``wind_speed``,
``wind_gust``, ``visibility``, ``pressure_msl``, all in the vocabulary's
units — ``degC``, ``deg``, ``m_s``, ``m``, ``hPa``). Wind speed/gust are
converted from the feed's knots to ``m_s``; visibility is converted from
statute miles (``"6+"`` meaning "6 or more", ``"1/2"`` meaning a fraction)
to metres. Every conversion keeps the provider's own value and unit in
``original_value``/``original_unit``.

Per that section's **"no provider value is dropped"** rule, every other
scalar field the feed reports — the QC bitmask, the raw METAR text, the
flight category, the sky-cover code, the report type, and station
elevation — is still emitted, as ``x_<field>`` (the feed's own field name in
``snake_case``) with ``unit="code"`` for an opaque provider code/string or a
matching unit id for a number, and the original value/unit preserved
alongside. Only two feed fields are deliberately never emitted as a
reading value: ``lat``/``lon`` (station coordinates — ``docs/weather-api.md``
section 1's hard rule that no response ever carries a coordinate overrides
"nothing is dropped" here) and ``clouds`` (a list of cloud-layer objects,
not a single value; the fixture's list is empty and this adapter does not
invent a multi-value shape the merged contract does not define).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Availability,
    Capability,
    FetchRecordLike,
    FreshnessStrategy,
    LocationLike,
    ProviderSettingsLike,
    Quota,
    ReadingLike,
    RequestSpec,
    WeatherProvider,
)
from climate.weather.store import Measurement, Reading

_ENDPOINT = "https://aviationweather.gov/api/data/metar"

#: 1 knot = 1 nautical mile/hour = 1852 m / 3600 s (exact, by definition).
_KNOTS_TO_MPS = 1852.0 / 3600.0

#: 1 statute mile = 1609.344 m (exact conversion factor).
_STATUTE_MILES_TO_M = 1609.344

#: Feed fields with no row in the shared vocabulary but a station-metadata
#: meaning this adapter still trusts (aviationweather.gov decodes elevation
#: in metres). Coordinates (``lat``/``lon``) and ``clouds`` are excluded on
#: purpose — see the module docstring.
_EXCLUDED_FIELDS = frozenset(
    {"icaoId", "receiptTime", "obsTime", "reportTime", "name", "lat", "lon", "clouds"}
)

#: Feed field name -> the id it is emitted under, when it is not one of the
#: vocabulary's own canonical ids handled explicitly in ``_extract_values``.
_CAMEL_RUN_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _field_to_snake(name: str) -> str:
    """``"qcField"`` -> ``"qc_field"``; already-lower names pass through."""
    return _CAMEL_RUN_RE.sub("_", name).lower()


def _stations_for(configured: Any, location_label: str) -> list[str]:
    """The ICAO codes ``location_label`` owns, from either configured shape.

    A mapping is per location: only this label's own entry is used, and a
    label absent from it selects nothing (no request). A plain list is the
    documented single-location convenience and serves every location — see
    the module docstring.
    """
    if isinstance(configured, Mapping):
        selected: Any = configured.get(location_label) or ()
    else:
        selected = configured or ()
    if isinstance(selected, str):
        selected = [selected]
    return [str(station).strip() for station in selected if station]


class MetarProvider(WeatherProvider):
    """Keyless METAR observations from aviationweather.gov, per ICAO station."""

    id = "metar"
    capabilities = frozenset({Capability.CURRENT_OBSERVATION})
    auth = AuthRequirement(required=False)
    default_interval_seconds = 1800
    quota = Quota(
        unlimited=True,
        source="aviationweather.gov Data API docs (no published rate limit for the "
        "keyless metar endpoint)",
        notes="keyless, no credential, no documented per-minute/day cap",
    )
    freshness = FreshnessStrategy.STATION_CADENCE
    attribution = Attribution(
        text=(
            "Aviation weather data from aviationweather.gov "
            "(NOAA/FAA, U.S. Government Work, public domain)"
        ),
        url="https://aviationweather.gov/data/api/",
        licence="U.S. Government Work (public domain)",
    )

    def availability(
        self,
        settings: ProviderSettingsLike | None = None,
        env: Any = None,
    ) -> Availability:
        """Disabled unless explicitly ``enabled: true`` in provider settings.

        The base implementation treats a *missing* settings entry as
        enabled; this adapter needs the opposite default, so it is checked
        here before delegating to the base behaviour (credential checks —
        moot for a keyless provider, but kept for consistency).
        """
        if settings is None or not getattr(settings, "enabled", False):
            return Availability(
                False, "metar is disabled until 'enabled: true' is set in its provider settings"
            )
        return super().availability(settings, env)

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Sequence[RequestSpec]:
        """One request for the stations *this* location selected, if any.

        ``env`` is accepted for the contract's sake and ignored: METAR is
        keyless, so this adapter never reads a credential.
        """
        del env
        params = dict(getattr(settings, "params", None) or {})
        stations = _stations_for(params.get("stations"), location.label)
        if not stations:
            return []
        url = f"{_ENDPOINT}?ids={','.join(stations)}&format=json"
        return [
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                purpose="metar",
                context={"stations": tuple(stations)},
            )
        ]

    def normalize(self, fetch_record: FetchRecordLike) -> tuple[ReadingLike, ...]:
        status = getattr(fetch_record, "status", None)
        if status is None or status >= 300:
            return ()
        body = getattr(fetch_record, "body", b"")
        if not body:
            return ()
        try:
            reports = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return ()
        if not isinstance(reports, list):
            return ()

        location_label = getattr(fetch_record, "location", "")
        requested_at = fetch_record.requested_at
        readings: list[Reading] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            icao = report.get("icaoId")
            observed_at = _parse_report_time(report.get("reportTime"))
            if not icao or observed_at is None:
                continue
            values = _extract_values(report)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=f"metar/{icao}",
                    location=location_label,
                    observed_at=observed_at,
                    requested_at=requested_at,
                    kind="observation",
                    values=values,
                )
            )
        return tuple(readings)


def _parse_report_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_statute_miles(raw: Any) -> float | None:
    """Parse the feed's visibility text (``"6+"``, ``"1/2"``, ``"2 1/2"``, ``"10"``)."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().rstrip("+")
    total = 0.0
    for part in text.split():
        try:
            if "/" in part:
                numerator, denominator = part.split("/", 1)
                total += float(numerator) / float(denominator)
            else:
                total += float(part)
        except (ValueError, ZeroDivisionError):
            return None
    return total


def _wind(raw: Any) -> tuple[float, float, str] | None:
    """``(m_s value, original knots value, "kt")`` or ``None`` when unusable."""
    if not isinstance(raw, (int, float)):
        return None
    knots = float(raw)
    return round(knots * _KNOTS_TO_MPS, 2), knots, "kt"


def _extract_values(report: dict[str, Any]) -> dict[str, Measurement]:
    values: dict[str, Measurement] = {}

    temp = report.get("temp")
    if isinstance(temp, (int, float)):
        values["temperature"] = Measurement(value=float(temp), unit="degC")

    dewp = report.get("dewp")
    if isinstance(dewp, (int, float)):
        values["dew_point"] = Measurement(value=float(dewp), unit="degC")

    wdir = report.get("wdir")
    if isinstance(wdir, (int, float)):
        values["wind_direction"] = Measurement(value=float(wdir), unit="deg")

    wspd_wind = _wind(report.get("wspd"))
    if wspd_wind is not None:
        m_s, original, original_unit = wspd_wind
        values["wind_speed"] = Measurement(
            value=m_s, unit="m_s", original_value=original, original_unit=original_unit
        )

    wgst_wind = _wind(report.get("wgst"))
    if wgst_wind is not None:
        m_s, original, original_unit = wgst_wind
        values["wind_gust"] = Measurement(
            value=m_s, unit="m_s", original_value=original, original_unit=original_unit
        )

    visib_miles = _parse_statute_miles(report.get("visib"))
    if visib_miles is not None:
        values["visibility"] = Measurement(
            value=round(visib_miles * _STATUTE_MILES_TO_M, 1),
            unit="m",
            original_value=report.get("visib"),
            original_unit="sm",
        )

    altim = report.get("altim")
    if isinstance(altim, (int, float)):
        values["pressure_msl"] = Measurement(value=float(altim), unit="hPa")

    values.update(_extra_values(report))
    return values


def _extra_values(report: dict[str, Any]) -> dict[str, Measurement]:
    """Every remaining scalar field, so no provider value is dropped.

    Fields already handled above (canonical vocabulary ids), pure
    identifiers/timestamps used elsewhere, station coordinates, and the
    non-scalar ``clouds`` list are excluded — see the module docstring.
    """
    handled = {"temp", "dewp", "wdir", "wspd", "wgst", "visib", "altim"}
    extra: dict[str, Measurement] = {}
    for field_name, raw_value in report.items():
        if field_name in handled or field_name in _EXCLUDED_FIELDS:
            continue
        key = f"x_{_field_to_snake(field_name)}"
        if isinstance(raw_value, bool) or raw_value is None:
            continue
        if isinstance(raw_value, (int, float)):
            extra[key] = Measurement(value=float(raw_value), unit="other")
        elif isinstance(raw_value, str) and raw_value.strip():
            # An opaque provider code/string (e.g. the raw METAR text, the
            # sky-cover code, the flight category): keep it verbatim.
            extra[key] = Measurement(value=raw_value, unit="code", original_value=raw_value)
    return extra
