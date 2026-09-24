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
``settings.params["station_ids"]``, when set, is used verbatim and skips
discovery entirely. Scheduling is per configured location, so the setting is
read per location and accepts either shape:

``{"<location label>": [id, ...], ...}`` (a mapping)
    The precise form: each location queries only the stations listed under
    its own label, so one location can never store another's stations. A
    configured location with no entry in the mapping (or an empty one)
    emits **no request at all** — it does not fall back to discovery.
``[id, ...]`` (a plain list)
    The documented single-location convenience: those ids serve every
    configured location. Kept so a single-location config stays a one-liner;
    prefer the mapping form once there is more than one location.

With no ``station_ids`` at all the adapter picks the
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
from collections.abc import Mapping
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

#: The service's own spelling of the radiation unit, as it appears in the
#: ``stations`` endpoint's ``monitors[].units`` for Grad/DiffR/NIP.
_PROVIDER_RADIATION_UNIT = "w/m^2"

#: Canonical radiation unit id (``docs/weather-api.md`` section 4.1).
_RADIATION_UNIT = "w_m2"

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
    "Grad": ("shortwave_radiation", _RADIATION_UNIT, _PROVIDER_RADIATION_UNIT),
    "DiffR": ("diffuse_radiation", _RADIATION_UNIT, _PROVIDER_RADIATION_UNIT),
    "NIP": ("direct_normal_radiation", _RADIATION_UNIT, _PROVIDER_RADIATION_UNIT),
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
    _PROVIDER_RADIATION_UNIT: _RADIATION_UNIT,
    "w/m2": _RADIATION_UNIT,
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


def _parse_station_coordinates(station: dict[str, Any]) -> tuple[float, float]:
    """One station's ``(latitude, longitude)``, ``(0.0, 0.0)`` when unusable."""
    location = station.get("location") or {}
    try:
        return float(location.get("latitude", 0.0)), float(location.get("longitude", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def _parse_station_channels(station: dict[str, Any]) -> dict[int, _ChannelInfo]:
    """One station's ``monitors`` as ``channel id -> channel identity``."""
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
    return channels


def _parse_station(raw: Any) -> _StationInfo | None:
    """One ``stations`` entry, or ``None`` when it is not usable metadata."""
    if not isinstance(raw, dict):
        return None
    try:
        station_id = int(raw["stationId"])
    except (KeyError, TypeError, ValueError):
        return None
    latitude, longitude = _parse_station_coordinates(raw)
    return _StationInfo(
        station_id=station_id,
        active=bool(raw.get("active", True)),
        latitude=latitude,
        longitude=longitude,
        channels=_parse_station_channels(raw),
    )


def _parse_stations_payload(raw: Any) -> dict[int, _StationInfo]:
    """Parse a ``stations`` endpoint list (or override) into id -> metadata.

    Silently skips malformed entries rather than raising: metadata parsing
    must never take the whole tick down over one bad record.
    """
    if not isinstance(raw, list):
        return {}
    parsed: dict[int, _StationInfo] = {}
    for entry in raw:
        station = _parse_station(entry)
        if station is not None:
            parsed[station.station_id] = station
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


def _entry_observed_at(entry: Any) -> datetime | None:
    """One ``data`` entry's observation instant in UTC, or ``None`` when unusable."""
    if not isinstance(entry, dict):
        return None
    raw_datetime = entry.get("datetime")
    if not raw_datetime:
        return None
    try:
        return _parse_ims_datetime(raw_datetime)
    except ValueError:
        return None


def _channel_measurement(
    raw_value: Any, provider_unit: str | None, name: str
) -> tuple[str, Measurement] | None:
    """``(variable id, measurement)`` for one channel, or ``None`` when unusable.

    A channel with a row in :data:`_CHANNEL_MAP` lands on its vocabulary id
    and unit. One without still comes through under a synthetic
    ``x_<name>`` id (``docs/weather-api.md`` section 4: "No provider value
    is dropped"), with its unit resolved through :data:`_UNIT_ALIASES` when
    the provider's own unit string is recognizable.
    """
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    mapping = _CHANNEL_MAP.get(name)
    if mapping is not None:
        variable_id, unit, original_unit = mapping
        original_unit = original_unit or provider_unit
    else:
        variable_id = f"x_{name.lower()}"
        original_unit = provider_unit
        unit = _UNIT_ALIASES.get((provider_unit or "").lower(), "other")
    return variable_id, Measurement(
        value=value,
        unit=unit,
        original_value=raw_value,
        original_unit=original_unit,
    )


def _station_count(params: dict[str, Any]) -> int:
    """How many nearest stations to query when none are pinned (never < 0)."""
    try:
        return max(0, int(params.get("station_count", DEFAULT_NEAREST_STATIONS)))
    except (TypeError, ValueError):
        return DEFAULT_NEAREST_STATIONS


def _explicit_station_ids(configured: Any, location_label: str) -> list[int] | None:
    """The station ids ``location_label`` pinned, or ``None`` for discovery.

    A mapping is per location: only this label's own entry is used, and a
    label absent from it returns an *empty list* — pinned to nothing, so no
    request — never ``None``, which would fall back to nearest-station
    discovery. A plain list is the documented single-location convenience
    and serves every location. ``None`` means nothing was configured at all.
    See the module docstring.
    """
    if isinstance(configured, Mapping):
        selected: Any = configured.get(location_label) or ()
    elif configured:
        selected = configured
    else:
        return None
    station_ids: list[int] = []
    for raw in selected:
        try:
            station_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return station_ids


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
        url="https://ims.gov.il/en/termOfuse",
        licence="IMS Terms of Use",
    )

    def __init__(self) -> None:
        self._station_cache: dict[int, _StationInfo] = {}

    # -- request construction ----------------------------------------------

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> list[RequestSpec]:
        """One ``latest`` request per station *this* location selected.

        The token is read with
        :meth:`~climate.weather.providers.base.WeatherProvider.credential`
        from ``env`` — the same mapping ``availability()`` was resolved
        against — so a key injected by the caller without touching
        :data:`os.environ` builds a real request instead of silently
        producing none.
        """
        token = self.credential(env)
        if not token:
            # Mirrors availability(): no requests are ever built without a
            # credential, regardless of whether the caller checked first.
            return []
        headers = {"Authorization": f"ApiToken {token}"}
        params: dict[str, Any] = dict(getattr(settings, "params", None) or {})

        explicit_ids = _explicit_station_ids(params.get("station_ids"), location.label)
        if explicit_ids is not None:
            return [
                self._latest_request(location, station_id, headers) for station_id in explicit_ids
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

        nearest = _nearest_station_ids(location, stations, _station_count(params))
        return [self._latest_request(location, station_id, headers) for station_id in nearest]

    def requests_per_tick(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
    ) -> int:
        """How many stations one due tick queries for ``location``.

        This adapter fans out — one ``latest`` request per selected station —
        so the base class's "one request per location" would under-count
        startup quota validation by a factor of the station count (four
        locations at the default interval really cost ~1 152 calls/day, not
        576). Reported here as the explicit ``station_ids`` count for this
        location when pinned, else ``station_count``
        (:data:`DEFAULT_NEAREST_STATIONS`). The one-off ``stations``
        metadata request on the first tick is never more than this, so it
        needs no separate allowance.
        """
        params: dict[str, Any] = dict(getattr(settings, "params", None) or {})
        explicit_ids = _explicit_station_ids(params.get("station_ids"), location.label)
        if explicit_ids is not None:
            return len(explicit_ids)
        return _station_count(params)

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
            observed_at = _entry_observed_at(entry)
            if observed_at is None:
                continue
            values = self._entry_values(station, entry)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=source,
                    model=None,
                    location=str(location_label),
                    observed_at=observed_at,
                    # These are station measurements, not a model run: the
                    # feed states no issue/run time, and the fetch time is
                    # never a substitute for one.
                    model_run_at=None,
                    requested_at=requested_at,
                    kind="observation",
                    values=values,
                )
            )
        return readings

    def _entry_values(
        self, station: _StationInfo | None, entry: dict[str, Any]
    ) -> dict[str, Measurement]:
        """Every valid channel of one ``data`` entry, as vocabulary values."""
        values: dict[str, Measurement] = {}
        for channel in entry.get("channels") or []:
            if not isinstance(channel, dict) or not channel.get("valid"):
                continue
            name, provider_unit = self._channel_identity(station, channel)
            if not name:
                continue
            measurement = _channel_measurement(channel.get("value"), provider_unit, name)
            if measurement is None:
                continue
            variable_id, value = measurement
            values[variable_id] = value
        return values

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
