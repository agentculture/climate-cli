"""Tests for the Open-Meteo adapter (spec targets c3, h3).

Uses the verbatim fixture captured for this task (see
``tests/fixtures/README.md``) and the neutral test point — never a real
coordinate. The adapter never touches the network: ``normalize()`` is
exercised directly against a :class:`~climate.weather.store.FetchRecord`
built from the fixture bytes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from climate.weather import providers as registry
from climate.weather.providers.base import (
    Capability,
    FreshnessStrategy,
    ProviderSettings,
    validate_provider,
)
from climate.weather.providers.open_meteo import (
    DEFAULT_CURRENT_VARIABLES,
    DEFAULT_DAILY_VARIABLES,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_HOURLY_VARIABLES,
    DEFAULT_MINUTELY_15_VARIABLES,
    OpenMeteoProvider,
    call_weight,
)
from climate.weather.store import FetchError, FetchRecord
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "open_meteo_forecast.json"
)
FIXTURE_BODY = FIXTURE_PATH.read_bytes()
FIXTURE_DOCUMENT = json.loads(FIXTURE_BODY)

REQUESTED_AT = datetime(2026, 9, 17, 18, 5, tzinfo=UTC)


@pytest.fixture(name="location")
def _location() -> ProviderSettings:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Location:
        label: str
        latitude: float
        longitude: float

    return _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)


_ENDPOINT = (
    f"https://api.open-meteo.com/v1/forecast?latitude={NEUTRAL_POINT[0]}"
    f"&longitude={NEUTRAL_POINT[1]}"
)


def _fetch(**overrides: object) -> FetchRecord:
    kwargs = dict(
        provider="open-meteo",
        endpoint=_ENDPOINT,
        location=NEUTRAL_LABEL,
        requested_at=REQUESTED_AT,
        status=200,
        body=FIXTURE_BODY,
        content_type="application/json",
    )
    kwargs.update(overrides)
    return FetchRecord(**kwargs)


# --- provider metadata ------------------------------------------------------


def test_provider_id_and_contract_are_sound() -> None:
    provider = OpenMeteoProvider()
    assert provider.id == "open-meteo"
    assert validate_provider(provider) == []
    assert provider.auth.required is False
    assert provider.default_interval_seconds == 900
    assert provider.freshness is FreshnessStrategy.INTERVAL
    assert Capability.CURRENT_MODEL in provider.capabilities
    assert Capability.FORECAST in provider.capabilities


def test_registry_discovers_open_meteo() -> None:
    registry.clear_cache()
    provider = registry.get_provider("open-meteo")
    assert isinstance(provider, OpenMeteoProvider)


# --- build_requests ----------------------------------------------------------


def test_build_requests_returns_one_https_request_for_the_location(location) -> None:
    provider = OpenMeteoProvider()
    requests = provider.build_requests(location)
    assert len(requests) == 1
    spec = requests[0]
    assert spec.provider_id == "open-meteo"
    assert spec.location_label == NEUTRAL_LABEL
    assert spec.url.startswith("https://api.open-meteo.com/v1/forecast?")


def test_build_requests_asks_for_all_four_blocks_and_the_48h_default(location) -> None:
    provider = OpenMeteoProvider()
    spec = provider.build_requests(location)[0]
    query = parse_qs(urlsplit(spec.url).query)
    assert query["current"][0].split(",") == list(DEFAULT_CURRENT_VARIABLES)
    assert query["minutely_15"][0].split(",") == list(DEFAULT_MINUTELY_15_VARIABLES)
    assert query["hourly"][0].split(",") == list(DEFAULT_HOURLY_VARIABLES)
    assert query["daily"][0].split(",") == list(DEFAULT_DAILY_VARIABLES)
    assert query["forecast_hours"][0] == str(DEFAULT_FORECAST_HOURS)
    # the AC-relevant list matches what the fixture was actually captured with
    assert set(query["current"][0].split(",")) == set(FIXTURE_DOCUMENT["current"].keys()) - {
        "time",
        "interval",
    }


def test_build_requests_horizon_and_variables_are_overridable(location) -> None:
    provider = OpenMeteoProvider()
    settings = ProviderSettings(
        params={
            "forecast_hours": 24,
            "current": ["temperature_2m"],
            "hourly": ["temperature_2m"],
        }
    )
    spec = provider.build_requests(location, settings)[0]
    query = parse_qs(urlsplit(spec.url).query)
    assert query["forecast_hours"][0] == "24"
    assert query["current"][0] == "temperature_2m"
    assert query["hourly"][0] == "temperature_2m"
    # unset blocks keep their default
    assert query["daily"][0].split(",") == list(DEFAULT_DAILY_VARIABLES)


def test_build_requests_never_embeds_a_real_coordinate_literal(location) -> None:
    # Regression guard for the repo's own hygiene rule: the neutral point is
    # used, and it must actually reach the URL (not silently dropped).
    provider = OpenMeteoProvider()
    spec = provider.build_requests(location)[0]
    query = parse_qs(urlsplit(spec.url).query)
    assert float(query["latitude"][0]) == NEUTRAL_POINT[0]
    assert float(query["longitude"][0]) == NEUTRAL_POINT[1]


# --- normalize: current reading ---------------------------------------------


def test_normalize_yields_a_current_reading_kind_model() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    current_readings = [r for r in readings if r.kind == "model"]
    assert len(current_readings) == 1
    current = current_readings[0]
    assert current.provider == "open-meteo"
    assert current.source == "best_match"
    assert current.model is None
    assert current.location == NEUTRAL_LABEL
    assert current.requested_at == REQUESTED_AT
    # fixture: current.time = "2026-09-17T18:00", utc_offset_seconds = 0
    assert current.observed_at == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def test_normalize_current_reading_carries_normalized_units() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    current = next(r for r in readings if r.kind == "model")
    temperature = current.values["temperature"]
    assert temperature.unit == "degC"
    assert temperature.value == pytest.approx(FIXTURE_DOCUMENT["current"]["temperature_2m"])
    wind_speed = current.values["wind_speed"]
    assert wind_speed.unit == "m_s"
    original_kmh = FIXTURE_DOCUMENT["current"]["wind_speed_10m"]
    assert wind_speed.value == pytest.approx(original_kmh / 3.6)
    assert wind_speed.original_value == original_kmh
    assert wind_speed.original_unit == "km/h"


def test_normalize_drops_variables_with_no_vocabulary_entry() -> None:
    provider = OpenMeteoProvider()
    current = next(r for r in provider.normalize(_fetch()) if r.kind == "model")
    assert "showers" not in current.values
    assert "snowfall" not in current.values


def test_normalize_observed_at_is_never_the_requested_at() -> None:
    provider = OpenMeteoProvider()
    current = next(r for r in provider.normalize(_fetch()) if r.kind == "model")
    assert current.observed_at != current.requested_at


# --- normalize: forecast readings -------------------------------------------


def test_normalize_yields_forecast_readings_for_hourly_entries_after_now() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    forecasts = [r for r in readings if r.kind == "forecast"]
    hourly_times = FIXTURE_DOCUMENT["hourly"]["time"]
    # index 0 equals current.time exactly, so it must be excluded
    assert len(forecasts) == len(hourly_times) - 1
    current = next(r for r in readings if r.kind == "model")
    assert all(reading.observed_at > current.observed_at for reading in forecasts)
    # strictly increasing, oldest excluded
    assert forecasts == sorted(forecasts, key=lambda r: r.observed_at)


def test_normalize_forecast_reading_values_and_provenance() -> None:
    provider = OpenMeteoProvider()
    forecasts = [r for r in provider.normalize(_fetch()) if r.kind == "forecast"]
    first = forecasts[0]
    assert first.provider == "open-meteo"
    assert first.source == "best_match"
    assert first.model is None
    assert "temperature" in first.values
    assert first.values["temperature"].unit == "degC"
    assert "precipitation_probability" in first.values
    assert first.values["precipitation_probability"].unit == "percent"


# --- normalize: failure / empty cases ---------------------------------------


def test_normalize_returns_no_readings_for_a_non_200_status() -> None:
    provider = OpenMeteoProvider()
    record = _fetch(status=304, body=b"")
    assert provider.normalize(record) == ()


def test_normalize_returns_no_readings_for_a_transport_error() -> None:
    provider = OpenMeteoProvider()
    record = _fetch(status=None, body=b"", error=FetchError(kind="timeout"))
    assert provider.normalize(record) == ()


def test_normalize_is_pure_and_re_derivable() -> None:
    provider = OpenMeteoProvider()
    record = _fetch()
    first = provider.normalize(record)
    second = provider.normalize(record)
    assert [r.to_document() for r in first] == [r.to_document() for r in second]


# --- quota / call weight -----------------------------------------------------


def test_quota_metadata() -> None:
    provider = OpenMeteoProvider()
    quota = provider.quota
    assert quota.calls_per_minute == 600
    assert quota.calls_per_day == 10_000
    assert quota.calls_per_month == 300_000
    assert quota.verified is False
    assert quota.source == "issue-5"


def test_call_weight_rule() -> None:
    assert call_weight(10, 1) == 1.0
    assert call_weight(5, 1) == 1.0  # minimum 1
    assert call_weight(20, 1) == 2.0
    assert call_weight(20, 2) == 4.0


def test_default_quota_call_weight_reflects_the_wide_default_request() -> None:
    provider = OpenMeteoProvider()
    total_variables = (
        len(DEFAULT_CURRENT_VARIABLES)
        + len(DEFAULT_MINUTELY_15_VARIABLES)
        + len(DEFAULT_HOURLY_VARIABLES)
        + len(DEFAULT_DAILY_VARIABLES)
    )
    assert provider.quota.call_weight == call_weight(total_variables)
    assert provider.quota.call_weight > 1.0

    # the budget check counts the wide request as several calls, not one
    assert provider.quota.daily_calls(interval_seconds=900, locations=1) == pytest.approx(
        (86_400 / 900) * provider.quota.call_weight
    )
