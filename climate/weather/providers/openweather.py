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
The API key is required (spec: :data:`ENV_VAR`) and is sent the only way
OpenWeather accepts it: as the ``appid`` query parameter on the live
request. :meth:`OpenWeatherProvider.build_requests` therefore builds a
:class:`~climate.weather.providers.base.RequestSpec` whose ``url`` *does*
carry the live key — the scheduler needs the real URL to make the call, and
``climate.weather.store.FetchRecord`` independently refuses to persist an
endpoint that still carries one (see :func:`climate.weather.store.redact_url`
/ ``SECRET_QUERY_PARAMS``, which already includes ``appid``). Nothing in
this module logs, so there is no log line to redact.

Known contract gap: ``RequestSpec`` is a plain frozen dataclass with no
custom ``__repr__`` in ``climate.weather.providers.base``, so
``repr(spec)``/``str(spec)`` on the object this module returns would show
the live key verbatim if anything ever printed it. This module never does
that itself (it returns the spec and nothing else), but it cannot fix the
leak for a caller that logs the spec directly without editing
``providers/base.py``, which is out of scope for this task — flagged rather
than worked around silently.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Sequence
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
    ) -> Sequence[RequestSpec]:
        """Describe exactly one current-weather request for ``location``.

        Returns an empty sequence when the API key is not configured —
        this never raises, so a missing key disables the provider (see
        :meth:`~climate.weather.providers.base.WeatherProvider.availability`)
        instead of failing the tick.
        """
        api_key = os.environ.get(ENV_VAR)
        if not api_key:
            return ()
        query = urlencode(
            {
                "lat": location.latitude,
                "lon": location.longitude,
                "units": "metric",
                "appid": api_key,
            }
        )
        url = f"{_ENDPOINT}?{query}"
        return (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                purpose="current",
            ),
        )

    def normalize(self, fetch_record: FetchRecordLike) -> Sequence[Reading]:
        """Derive one "model" reading from a stored current-weather fetch.

        A transport failure, an HTTP error status, or an empty body (a
        conditional-GET 304, or nothing usable in the payload) yields no
        readings.
        """
        status = fetch_record.status
        body = fetch_record.body
        if status is None or status >= 400 or not body:
            return ()
        try:
            payload: dict[str, Any] = _load_json(body)
        except ValueError:
            return ()

        observed_at = _observed_at(payload)
        if observed_at is None:
            return ()

        values = _extract_values(payload)
        if not values:
            return ()

        reading = Reading(
            provider=self.id,
            source=self.id,
            model=None,
            location=fetch_record.location,
            observed_at=observed_at,
            requested_at=fetch_record.requested_at,
            kind="model",
            values=values,
        )
        return (reading,)


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


def _extract_values(payload: dict[str, Any]) -> dict[str, Measurement]:
    main = payload.get("main") or {}
    wind = payload.get("wind") or {}
    clouds = payload.get("clouds") or {}
    visibility = payload.get("visibility")

    values: dict[str, Measurement] = {}

    def _add(name: str, source: dict[str, Any], key: str, unit: str) -> None:
        if key in source and source[key] is not None:
            values[name] = Measurement(value=float(source[key]), unit=unit)

    _add("temperature", main, "temp", "C")
    _add("feels_like", main, "feels_like", "C")
    _add("humidity", main, "humidity", "%")
    _add("pressure", main, "pressure", "hPa")
    _add("wind_speed", wind, "speed", "m/s")
    _add("wind_deg", wind, "deg", "deg")
    _add("cloud_cover", clouds, "all", "%")
    if visibility is not None:
        values["visibility"] = Measurement(value=float(visibility), unit="m")

    return values
