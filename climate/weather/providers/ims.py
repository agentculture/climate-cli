"""IMS (Israel Meteorological Service) Envista station-observation adapter.

Built against the documented API shape only. No IMS token exists in this
environment: the fixtures this adapter is tested against
(``tests/fixtures/ims_stations.json``, ``tests/fixtures/ims_latest.json``)
are **synthesized** from the vendor PDF (``API_Explanation_en.pdf``, see
``tests/fixtures/README.md``), not captured live. Live verification against
a real token is a recorded plan risk, not part of this task.

Two Envista endpoints are used (base ``https://api.ims.gov.il/v1/envista/``):

``stations``
    ``GET stations`` — the full station list: id, active flag, coordinates
    and its ``monitors`` (channel id -> name -> unit). Station and channel
    ids are *not* stable across stations (the vendor PDF: "the channel Id
    for the same meteorological variable might be different for different
    stations"), so this adapter never hard-codes either. It discovers them
    from this endpoint and caches the parsed result on the instance
    (``self._station_cache``) — the registry instantiates one adapter per
    process and reuses it across ticks (see
    ``climate.weather.providers.iter_providers``), so an instance attribute
    is a reasonable, documented cache seam. :meth:`build_requests` emits a
    ``purpose="stations"`` request only when it has no cached metadata (and
    no ``station_metadata`` override — see below); once cached, station
    selection and channel-id lookups use it without re-fetching.

``stations/{id}/data/latest``
    The latest reading for one station, across all its channels. Each
    channel in the response already carries its own ``name`` (e.g. ``TD``,
    ``Grad``) alongside its numeric ``id``, so a reading can be normalized
    correctly even with no station-metadata cache at all (the cache is used
    to resolve a channel by its *id* when available, falling back to the
    response's own ``name`` field otherwise — belt and suspenders, never a
    hard-coded id).

Station choice per location
----------------------------
``settings.params["station_ids"]``, when set, is used verbatim (a list of
station ids) and skips discovery entirely. Otherwise the adapter picks the
``settings.params.get("station_count", DEFAULT_NEAREST_STATIONS)`` nearest
*active* stations to the location by great-circle (haversine) distance,
computed from the cached station metadata. ``settings.params
["station_metadata"]`` (a list in the same shape as the ``stations``
response) is an override seam for callers that already hold fresh metadata
(and for tests) — it is used in place of, but never merges into, the
instance cache.

Timestamps
----------
The vendor PDF is explicit and is quoted here because it is easy to get
backwards: **"The observation date is always in Israel winter time
(UTC + 2), and not as mentioned in the output datetime +03:00"**. This
adapter therefore ignores the payload's UTC offset label entirely and
treats the naive wall-clock time as a fixed UTC+2, converting it to UTC by
subtracting two hours — including in summer, when the label (wrongly) says
``+03:00``.

Radiation channels ``Grad`` (global), ``DiffR`` (diffuse) and ``NIP``
(direct) are measured station instruments, kept as observations in
``w_m2`` (the service's canonical radiation unit; see
``docs/weather-api.md`` section 4).

Quota
-----
No published Envista rate limit was found without a token, so
:class:`~climate.weather.providers.base.Quota` here is a conservative,
explicitly *unverified* placeholder (``verified=False``,
``source="unverified"``) rather than a guessed-but-presented-as-fact figure.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

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

__all__ = ["ImsProvider"]

_BASE_URL = "https://api.ims.gov.il/v1/envista/"

#: Number of nearest active stations queried per location when the user has
#: not pinned explicit ``station_ids``.
DEFAULT_NEAREST_STATIONS = 2

#: Mean Earth radius used for the haversine distance, in kilometres.
_EARTH_RADIUS_KM = 6371.0

#: Documented Envista channel name -> (canonical variable id, canonical
#: unit, provider's own unit) — from the vendor PDF's Appendix C and
#: confirmed against ``tests/fixtures/ims_stations.json``'s ``monitors``.
#: Canonical ids/units follow ``docs/weather-api.md`` section 4 so IMS
#: readings line up with every other provider's vocabulary. ``NIP`` (the
#: direct-beam pyrheliometer channel) maps to ``direct_normal_radiation``,
#: *not* the generic ``direct_radiation`` — section 4 names it explicitly as
#: the IMS ``NIP`` mapping.
_CHANNEL_MAP: dict[str, tuple[str, str, str]] = {
    "TD": ("temperature", "degC", "degC"),
    "TDmax": ("temperature_max", "degC", "degC"),
    "TDmin": ("temperature_min", "degC", "degC"),
    "TG": ("temperature_grass_min", "degC", "degC"),
    "RH": ("relative_humidity", "percent", "%"),
    "WS": ("wind_speed", "m_s", "m/s"),
    "WD": ("wind_direction", "deg", "deg"),
    "WSMax": ("wind_gust", "m_s", "m/s"),
    "WDmax": ("wind_gust_direction", "deg", "deg"),
    "WS1mm": ("wind_speed_max_1min", "m_s", "m/s"),
    "Ws10mm": ("wind_speed_max_10min", "m_s", "m/s"),
    "STDwd": ("wind_direction_std", "deg", "deg"),
    "Rain": ("rain", "mm", "mm"),
    "BP": ("pressure_surface", "hPa", "hPa"),
    "Grad": ("shortwave_radiation", "w_m2", "w/m^2"),
    "DiffR": ("diffuse_radiation", "w_m2", "w/m^2"),
    "NIP": ("direct_normal_radiation", "w_m2", "w/m^2"),
}

#: Provider unit tokens (as they appear in the ``stations`` endpoint's
#: ``monitors[].units``, case-insensitive) -> canonical unit id from
#: ``docs/weather-api.md`` section 4.1. Used only for the "never dropped"
#: fallback below, where the variable itself has no row in ``_CHANNEL_MAP``
#: but its unit is still recognizable.
_UNIT_ALIASES: dict[str, str] = {
    "c": "degC",
    "degc": "degC",
    "%": "percent",
    "hpa": "hPa",
    "mb": "hPa",
    "m/s": "m_s",
    "deg": "deg",
    "mm": "mm",
    "w/m^2": "w_m2",
    "w/m2": "w_m2",
    "m": "m",
}


@dataclass(frozen=True)
class _ChannelInfo:
    """One monitor's identity, from the ``stations`` endpoint's ``monitors``."""

    name: str
    unit: str | None


@dataclass(frozen=True)
class _StationInfo:
    """Parsed metadata for one Envista station, from the ``stations`` endpoint."""

    station_id: int
    active: bool
    latitude: float
    longitude: float
    channels: dict[int, _ChannelInfo] = field(default_factory=dict)


def _parse_stations_payload(raw: Any) -> dict[int, _StationInfo]:
    """Parse a ``stations`` endpoint list (or override) into id -> metadata.

    Silently skips malformed entries rather than raising: metadata parsing
    must never take the whole tick down over one bad record.
    """
    parsed: dict[int, _StationInfo] = {}
    if not isinstance(raw, list):
        return parsed
    for station in raw:
        if not isinstance(station, dict):
            continue
        try:
            station_id = int(station["stationId"])
        except (KeyError, TypeError, ValueError):
            continue
        location = station.get("location") or {}
        try:
            latitude = float(location.get("latitude", 0.0))
            longitude = float(location.get("longitude", 0.0))
        except (TypeError, ValueError):
            latitude = longitude = 0.0
        channels: dict[int, _ChannelInfo] = {}
        for monitor in station.get("monitors") or []:
            if not isinstance(monitor, dict):
                continue
            channel_id = monitor.get("channelId")
            name = monitor.get("name")
            if channel_id is None or not name:
                continue
            try:
                channels[int(channel_id)] = _ChannelInfo(name=str(name), unit=monitor.get("units"))
            except (TypeError, ValueError):
                continue
        parsed[station_id] = _StationInfo(
            station_id=station_id,
            active=bool(station.get("active", True)),
            latitude=latitude,
            longitude=longitude,
            channels=channels,
        )
    return parsed


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def _nearest_station_ids(
    location: LocationLike, stations: dict[int, _StationInfo], count: int
) -> list[int]:
    """The ``count`` nearest *active* station ids to ``location``, closest first."""
    active = [station for station in stations.values() if station.active]
    active.sort(
        key=lambda station: _haversine_km(
            location.latitude, location.longitude, station.latitude, station.longitude
        )
    )
    return [station.station_id for station in active[: max(0, count)]]


def _parse_ims_datetime(raw: str) -> datetime:
    """Convert an Envista ``datetime`` string to UTC.

    Per the vendor documentation, the observation instant is *always*
    Israel winter time (a fixed UTC+2) regardless of what offset the
    payload's own label claims (it is hard-coded to ``+03:00`` and is
    simply wrong). The offset is therefore ignored outright: only the
    first 19 characters (``YYYY-MM-DDTHH:MM:SS``) are parsed as a naive
    wall-clock time, which is then treated as UTC+2 and converted to UTC.
    """
    wall_clock = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    return (wall_clock - timedelta(hours=2)).replace(tzinfo=UTC)


class ImsProvider(WeatherProvider):
    """Israel Meteorological Service Envista station observations."""

    id = "ims"
    capabilities = frozenset(
        {Capability.CURRENT_OBSERVATION, Capability.RADIATION, Capability.STATION_METADATA}
    )
    auth = AuthRequirement(
        required=True,
        env_var="CLIMATE_IMS_API_TOKEN",
        note="Contact ims@ims.gov.il for an Envista API token.",
    )
    default_interval_seconds = 600
    quota = Quota(
        calls_per_day=1000,
        verified=False,
        source="unverified",
        notes=(
            "No published Envista rate limit was found without a token; "
            "1000 calls/day is a conservative placeholder pending live "
            "verification once a token exists (see plan risks)."
        ),
    )
    freshness = FreshnessStrategy.STATION_CADENCE
    attribution = Attribution(
        text="Data: Israel Meteorological Service (IMS), Envista network",
        url="https://ims.gov.il/en",
        licence="",
    )

    def __init__(self) -> None:
        self._station_cache: dict[int, _StationInfo] = {}

    # -- request construction ----------------------------------------------

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
    ) -> list[RequestSpec]:
        token = os.environ.get(self.auth.env_var or "")
        if not token:
            # Mirrors availability(): no requests are ever built without a
            # credential, regardless of whether the caller checked first.
            return []
        headers = {"Authorization": f"ApiToken {token}"}
        params: dict[str, Any] = dict(getattr(settings, "params", None) or {})

        explicit_ids = params.get("station_ids")
        if explicit_ids:
            return [
                self._latest_request(location, int(station_id), headers)
                for station_id in explicit_ids
            ]

        stations = self._resolve_station_metadata(params)
        if not stations:
            return [
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url=f"{_BASE_URL}stations",
                    headers=headers,
                    purpose="stations",
                )
            ]

        count = int(params.get("station_count", DEFAULT_NEAREST_STATIONS))
        nearest = _nearest_station_ids(location, stations, count)
        return [self._latest_request(location, station_id, headers) for station_id in nearest]

    def _resolve_station_metadata(self, params: dict[str, Any]) -> dict[int, _StationInfo]:
        """Fresh metadata, if any: an explicit override, else the instance cache."""
        override = params.get("station_metadata")
        if override is not None:
            return _parse_stations_payload(override)
        return self._station_cache

    @staticmethod
    def _latest_request(
        location: LocationLike, station_id: int, headers: dict[str, str]
    ) -> RequestSpec:
        return RequestSpec(
            provider_id="ims",
            location_label=location.label,
            url=f"{_BASE_URL}stations/{station_id}/data/latest",
            headers=headers,
            purpose="latest",
            context={"station_id": station_id},
        )

    # -- normalization -------------------------------------------------------

    def normalize(self, fetch_record: Any) -> list[Reading]:
        status = getattr(fetch_record, "status", None)
        if status is None or status == 304 or status >= 400:
            return []
        body = getattr(fetch_record, "body", b"")
        if not body:
            return []
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []

        if isinstance(payload, list):
            # A stations-metadata response: cache it, no readings of its own.
            self._station_cache = _parse_stations_payload(payload)
            return []

        if not isinstance(payload, dict) or "data" not in payload:
            return []

        return self._readings_from_latest(fetch_record, payload)

    def _readings_from_latest(self, fetch_record: Any, payload: dict[str, Any]) -> list[Reading]:
        try:
            station_id = int(payload["stationId"])
        except (KeyError, TypeError, ValueError):
            return []

        location_label = getattr(fetch_record, "location", None)
        if location_label is None:
            location_label = getattr(fetch_record, "location_label", "")
        requested_at = fetch_record.requested_at
        source = f"envista/v1/stations/{station_id}/data/latest"
        station = self._station_cache.get(station_id)

        readings: list[Reading] = []
        for entry in payload.get("data") or []:
            if not isinstance(entry, dict):
                continue
            raw_datetime = entry.get("datetime")
            if not raw_datetime:
                continue
            try:
                observed_at = _parse_ims_datetime(raw_datetime)
            except ValueError:
                continue

            values: dict[str, Measurement] = {}
            for channel in entry.get("channels") or []:
                if not isinstance(channel, dict) or not channel.get("valid"):
                    continue
                raw_value = channel.get("value")
                if raw_value is None:
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                name, provider_unit = self._channel_identity(station, channel)
                if not name:
                    continue
                mapping = _CHANNEL_MAP.get(name)
                if mapping is not None:
                    variable_id, unit, original_unit = mapping
                    original_unit = original_unit or provider_unit
                else:
                    # docs/weather-api.md section 4: "No provider value is
                    # dropped" — a channel with no vocabulary row still
                    # comes through, under a synthetic x_<name> id.
                    variable_id = f"x_{name.lower()}"
                    original_unit = provider_unit
                    unit = _UNIT_ALIASES.get((provider_unit or "").lower(), "other")
                values[variable_id] = Measurement(
                    value=value,
                    unit=unit,
                    original_value=raw_value,
                    original_unit=original_unit,
                )

            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=source,
                    model=None,
                    location=str(location_label),
                    observed_at=observed_at,
                    requested_at=requested_at,
                    kind="observation",
                    values=values,
                )
            )
        return readings

    @staticmethod
    def _channel_identity(
        station: _StationInfo | None, channel: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Resolve a channel's (name, provider unit), preferring cached metadata.

        Cached station metadata (from the ``stations`` endpoint) is looked up
        by channel id first; the ``latest`` response's own self-reported
        ``name`` is the fallback when no cache is available (or the id is
        unknown to it), so a reading can still be normalized with no prior
        ``stations`` fetch at all. The id->name->unit mapping is never
        hard-coded, only ever discovered from one of those two places.
        """
        raw_id = channel.get("id")
        if station is not None and raw_id is not None:
            try:
                cached = station.channels.get(int(raw_id))
            except (TypeError, ValueError):
                cached = None
            if cached is not None and cached.name:
                return cached.name, cached.unit
        name = channel.get("name")
        return (str(name) if name else None), None
