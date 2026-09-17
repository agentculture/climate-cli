#!/usr/bin/env python3
"""Serve the weather dashboard against fixture-derived data, no MongoDB.

This is a **development and screenshot harness** for
``climate/weather/web/static/``. It never touches Mongo, never opens an
outbound socket and never reads the user's real configuration:

1. it builds an :class:`~climate.weather.store.InMemoryWeatherStore`,
2. it replays the verbatim provider responses in ``tests/fixtures/``
   through the *real* adapters' ``normalize()``,
3. it time-shifts copies of those readings across the last few hours so
   the dashboard has a plausible multi-provider history — including one
   deliberate collection gap, one deliberately stale provider and one
   provider that is disabled for want of a credential,
4. it serves the result with :func:`climate.weather.web.api.create_server`
   on loopback, on a free port (never the production default 8095).

Run it::

    uv run python scripts/dev_dashboard_server.py            # picks a free port
    uv run python scripts/dev_dashboard_server.py --port 8123
    uv run python scripts/dev_dashboard_server.py --empty    # first-hour state

Privacy notes
-------------
* The location label is the neutral ``"home"`` from ``tests.weather.neutral``;
  coordinates are the ``0.0`` placeholder the repo-hygiene scan allows.
* Two adapters derive a *name* from their fixture — ``metar`` puts the ICAO
  station id in ``Reading.source`` and ``ims-forecast`` puts a city name
  there. This script rewrites ``metar``'s source to a neutral station id and
  skips ``ims-forecast`` entirely (its adapter needs a real city name in the
  request URL fragment, and this repo does not write place names down).

Value shaping
-------------
A fixture is one instant. Replaying the same bytes N times would draw N
identical flat lines, which tells you nothing about whether the chart
works. Each time-shifted copy therefore gets a smooth, deterministic
modulation applied per unit (a diurnal curve for temperature, a daylight
curve for radiation, a slow drift for pressure, …) plus a small constant
per-provider bias, so the providers visibly disagree the way real ones do.
The *shapes* are synthetic; the variables, units, kinds and provenance are
whatever the real adapters produced.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - dev entry point
    sys.path.insert(0, str(REPO_ROOT))

from climate import __version__ as PACKAGE_VERSION  # noqa: E402
from climate.weather.config import Location, ProviderSettings, WeatherConfig  # noqa: E402
from climate.weather.providers import iter_providers  # noqa: E402
from climate.weather.store import FetchRecord, InMemoryWeatherStore, Measurement  # noqa: E402
from climate.weather.web.api import create_server  # noqa: E402
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
STATIC_ROOT = REPO_ROOT / "climate" / "weather" / "web" / "static"

#: The production default. This harness must never squat on it.
PRODUCTION_PORT = 8095

#: How far back the replayed history reaches. A little over a day, so the
#: 6 h and 24 h windows are full and ``/stats``'s default 24 h window has
#: something honest to measure; the 7 d window is deliberately partial.
DEFAULT_SPAN_HOURS = 26.0

#: A second configured label with no stored data, so the location selector
#: has something to switch to and the empty state is reachable in one click.
SECOND_LABEL = "office"

#: The fixtures were captured in different seasons and at different hours, so
#: their raw air temperatures sit up to 12 degrees apart — a disagreement
#: about the calendar, not about the weather, and it would dominate the
#: comparison chart. Every provider's degC channel is re-based to this value
#: (plus its own bias) at the newest tick, which keeps the *shape* and the
#: provider-to-provider spread that the chart exists to show.
BASE_TEMPERATURE_C = 21.0

#: Adapters this harness deliberately does not replay, and why. Each one
#: therefore shows up in the dashboard as a disabled / no-data provider,
#: which is itself a state worth looking at.
SKIPPED_PROVIDERS = {
    "ims": "needs CLIMATE_IMS_API_TOKEN, so the registry reports it disabled",
    "ims-forecast": "its request URL fragment must carry a real city name",
    "openweather": (
        "its normalize() emits variable ids and unit ids outside the "
        "docs/weather-api.md vocabulary (feels_like / humidity / pressure / "
        "wind_deg; units 'C', '%', 'm/s') and leaves the fixture's Kelvin "
        "temperature unconverted — replaying it would put 285.32 C on screen"
    ),
}


class FixturePlan:
    """How one provider's fixture is replayed into the store."""

    def __init__(
        self,
        provider_id: str,
        fixture: str,
        endpoint: str,
        interval_seconds: int,
        span_hours: float,
        *,
        bias: float = 0.0,
        newest_age_seconds: int = 0,
        gap_ticks: Sequence[int] = (),
        error_ticks: Sequence[int] = (),
        cache_headers: dict[str, str] | None = None,
        charset: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.fixture = fixture
        self.endpoint = endpoint
        self.interval_seconds = interval_seconds
        self.span_hours = span_hours
        self.bias = bias
        self.newest_age_seconds = newest_age_seconds
        self.gap_ticks = frozenset(gap_ticks)
        self.error_ticks = frozenset(error_ticks)
        self.cache_headers = cache_headers or {}
        self.charset = charset

    @property
    def tick_count(self) -> int:
        return max(1, int(self.span_hours * 3600 // self.interval_seconds))


# Only the adapters not listed in SKIPPED_PROVIDERS are replayed.
PLANS: tuple[FixturePlan, ...] = (
    FixturePlan(
        "open-meteo",
        "open_meteo_forecast.json",
        "https://api.open-meteo.com/v1/forecast?timezone=UTC",
        interval_seconds=900,
        span_hours=DEFAULT_SPAN_HOURS,
        bias=0.0,
        # A four-tick hole about two hours back: a stopped stack, a missed
        # fetch window — the gap the chart must draw as a break.
        gap_ticks=(8, 9, 10, 11),
        error_ticks=(14,),
    ),
    FixturePlan(
        "met-no",
        "met_no_locationforecast.json",
        "https://api.met.no/weatherapi/locationforecast/2.0/complete",
        interval_seconds=1800,
        span_hours=DEFAULT_SPAN_HOURS,
        bias=0.7,
        cache_headers={"Expires": "Thu, 17 Sep 2026 09:00:00 GMT"},
    ),
    FixturePlan(
        "metar",
        "metar_llbg.json",
        "https://aviationweather.gov/api/data/metar?format=json",
        interval_seconds=3600,
        span_hours=DEFAULT_SPAN_HOURS,
        bias=-0.3,
        # Nothing since three hours ago: the stale provider.
        newest_age_seconds=3 * 3600,
    ),
)

#: Providers whose forecast readings are stored (only for the newest fetch —
#: keeping every issue would multiply the store for no visible gain).
FORECAST_PROVIDERS = frozenset({"open-meteo", "met-no"})

_DAY = 86400.0


def _diurnal(moment: datetime) -> float:
    """A -1..1 day curve peaking mid-afternoon."""
    seconds = moment.timestamp() % _DAY
    return math.sin((seconds / _DAY) * 2 * math.pi - math.pi / 2 - 0.7)


def _daylight(moment: datetime) -> float:
    """A 0..1 daylight curve, flat zero at night."""
    return max(0.0, _diurnal(moment))


def _ripple(moment: datetime, period_seconds: float) -> float:
    return math.sin((moment.timestamp() % period_seconds) / period_seconds * 2 * math.pi)


def _shape(value: float, unit: str, moment: datetime, bias: float, offset: float = 0.0) -> float:
    """Modulate one measured value so a replayed series is not a flat line."""
    wobble = _ripple(moment, 5400.0)
    if unit == "degC":
        return round(value + offset + 3.4 * _diurnal(moment) + 0.45 * wobble + bias, 1)
    if unit == "percent":
        shaped = value - 14.0 * _diurnal(moment) + 2.5 * wobble - bias * 3
        return round(min(100.0, max(0.0, shaped)), 0)
    if unit == "hPa":
        return round(value + 1.4 * _ripple(moment, 6 * 3600.0) + bias * 0.3, 1)
    if unit == "m_s":
        return round(max(0.0, value * (1 + 0.35 * wobble) + 0.8 * _daylight(moment) + bias), 1)
    if unit == "w_m2":
        return round(max(0.0, value * _daylight(moment) * (1 + 0.06 * wobble)), 0)
    if unit == "mm":
        return round(max(0.0, value + 0.2 * max(0.0, -wobble - 0.6)), 1)
    if unit == "deg":
        return round((value + 26 * wobble + bias * 8) % 360, 0)
    if unit == "index":
        return round(max(0.0, value * _daylight(moment)), 1)
    return value


def _shape_values(
    values: dict[str, Measurement], moment: datetime, bias: float, offset: float
) -> dict:
    shaped = {}
    for name, measurement in values.items():
        raw = measurement.value
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            shaped[name] = replace(
                measurement, value=_shape(float(raw), measurement.unit, moment, bias, offset)
            )
        else:
            shaped[name] = measurement
    return shaped


def _temperature_offset(readings: Iterable[Any], bias: float) -> float:
    """How far this fixture's air temperature has to move to join the others."""
    for reading in readings:
        if reading.kind == "forecast":
            continue
        measurement = reading.values.get("temperature")
        if measurement is not None and isinstance(measurement.value, (int, float)):
            return BASE_TEMPERATURE_C + bias - float(measurement.value)
    return 0.0


def _neutralize_source(source: str) -> str:
    """Strip the one identifying name an adapter puts into ``source``."""
    if source.startswith("metar/"):
        return "metar/STN-1"
    if source.startswith("ims-forecast/"):
        return "ims-forecast/city"
    return source


def _anchor(readings: Iterable[Any]) -> datetime | None:
    """The fixture's own 'now': the earliest non-forecast observation."""
    candidates = [r.observed_at for r in readings if r.kind != "forecast"]
    if candidates:
        return min(candidates)
    every = [r.observed_at for r in readings]
    return min(every) if every else None


def _record(plan: FixturePlan, moment: datetime, body: bytes, status: int) -> FetchRecord:
    return FetchRecord(
        provider=plan.provider_id,
        endpoint=plan.endpoint,
        location=NEUTRAL_LABEL,
        requested_at=moment,
        status=status,
        body=body if status == 200 else b"",
        cache_headers=plan.cache_headers,
        charset=plan.charset,
    )


def _load(
    store: InMemoryWeatherStore,
    plan: FixturePlan,
    provider: Any,
    now: datetime,
    span_hours: float,
) -> int:
    body = (FIXTURES / plan.fixture).read_bytes()
    probe = provider.normalize(_record(plan, now, body, 200))
    anchor = _anchor(probe)
    if anchor is None:
        return 0
    offset = _temperature_offset(probe, plan.bias)

    newest = now - timedelta(seconds=plan.newest_age_seconds)
    ticks = max(1, int(span_hours * 3600 // plan.interval_seconds))
    saved = 0
    for tick in range(ticks):
        if tick in plan.gap_ticks:
            continue
        moment = newest - timedelta(seconds=tick * plan.interval_seconds)
        status = 500 if tick in plan.error_ticks else 200
        fetch_id = store.save_fetch(_record(plan, moment, body, status))
        if status != 200:
            continue
        shift = moment - anchor
        keep_forecast = tick == 0 and plan.provider_id in FORECAST_PROVIDERS
        readings = []
        for reading in provider.normalize(_record(plan, moment, body, 200)):
            if reading.kind == "forecast" and not keep_forecast:
                continue
            observed_at = reading.observed_at + shift
            readings.append(
                replace(
                    reading,
                    source=_neutralize_source(reading.source),
                    observed_at=observed_at,
                    requested_at=moment,
                    values=_shape_values(dict(reading.values), observed_at, plan.bias, offset),
                )
            )
        if readings:
            store.save_readings(fetch_id, readings)
            saved += len(readings)
    return saved


def build_store(
    *,
    empty: bool,
    now: datetime | None = None,
    span_hours: float = DEFAULT_SPAN_HOURS,
) -> InMemoryWeatherStore:
    """Build the in-memory store the dev server serves."""
    store = InMemoryWeatherStore()
    moment = now or datetime.now(UTC)
    store.save_heartbeat(PACKAGE_VERSION, moment)
    if empty:
        return store
    providers = {provider.id: provider for provider in iter_providers()}
    for plan in PLANS:
        provider = providers.get(plan.provider_id)
        if provider is not None:
            _load(store, plan, provider, moment, span_hours)
    return store


def build_config() -> WeatherConfig:
    """Two neutral labels; the second one deliberately has no data."""
    return WeatherConfig(
        locations={
            NEUTRAL_LABEL: Location(NEUTRAL_LABEL, *NEUTRAL_POINT),
            SECOND_LABEL: Location(SECOND_LABEL, 0.0, 0.0),
        },
        providers={
            plan.provider_id: ProviderSettings(
                provider_id=plan.provider_id,
                interval_seconds=plan.interval_seconds,
            )
            for plan in PLANS
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 (default) asks the OS for a free port; 8095 is refused.",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="serve an empty store, to inspect the first-hour / no-data states.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=DEFAULT_SPAN_HOURS,
        help="how many hours of replayed history to build (default 26).",
    )
    args = parser.parse_args(argv)

    if args.port == PRODUCTION_PORT:
        parser.error(f"port {PRODUCTION_PORT} is the production default; pick another")

    server = create_server(
        bind=args.host,
        port=args.port,
        store=build_store(empty=args.empty, span_hours=args.hours),
        config=build_config(),
        providers=iter_providers(),
        static_root=STATIC_ROOT,
    )
    host, port = server.server_address[:2]
    print(f"dashboard: http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - dev entry point
    raise SystemExit(main())
