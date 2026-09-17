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
    assert current.source == "v1/forecast/current"
    assert current.model is None
    assert current.location == NEUTRAL_LABEL
    assert current.requested_at == REQUESTED_AT
    # fixture: current.time = "2026-09-17T18:00", utc_offset_seconds = 0
    assert current.observed_at == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def test_model_run_at_is_none_because_the_payload_states_none() -> None:
    """qodo-14: Open-Meteo states no model issue/run time, so none is claimed.

    ``generationtime_ms`` is how long the *server* spent computing the
    answer, not when the model ran, and the fetch time is not a model run
    time either — neither may stand in for one.
    """
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    assert readings
    assert FIXTURE_DOCUMENT.get("generationtime_ms") is not None
    assert all(r.model_run_at is None for r in readings)
    assert all(r.requested_at == REQUESTED_AT for r in readings)


def test_build_requests_accepts_the_env_keyword_although_it_needs_no_credential(location) -> None:
    provider = OpenMeteoProvider()
    assert provider.build_requests(location, None, env={}) == provider.build_requests(location)


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


def test_normalize_never_drops_showers_or_snowfall() -> None:
    # docs/weather-api.md section 4 now carries vocabulary rows for both.
    provider = OpenMeteoProvider()
    current = next(r for r in provider.normalize(_fetch()) if r.kind == "model")
    showers = current.values["showers"]
    assert showers.unit == "mm"
    assert showers.value == pytest.approx(FIXTURE_DOCUMENT["current"]["showers"])

    snowfall = current.values["snowfall"]
    original_cm = FIXTURE_DOCUMENT["current"]["snowfall"]
    assert snowfall.unit == "mm"
    # Open-Meteo reports snowfall in cm; the vocabulary unit is mm (x10).
    assert snowfall.value == pytest.approx(original_cm * 10)
    assert snowfall.original_value == original_cm
    assert snowfall.original_unit == "cm"


def test_normalize_observed_at_is_never_the_requested_at() -> None:
    provider = OpenMeteoProvider()
    current = next(r for r in provider.normalize(_fetch()) if r.kind == "model")
    assert current.observed_at != current.requested_at


# --- normalize: forecast readings -------------------------------------------


def test_normalize_yields_forecast_readings_for_hourly_entries_after_now() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    forecasts = [r for r in readings if r.source == "v1/forecast/hourly"]
    hourly_times = FIXTURE_DOCUMENT["hourly"]["time"]
    # index 0 equals current.time exactly, so it must be excluded
    assert len(forecasts) == len(hourly_times) - 1
    current = next(r for r in readings if r.kind == "model")
    assert all(reading.kind == "forecast" for reading in forecasts)
    assert all(reading.observed_at > current.observed_at for reading in forecasts)
    # strictly increasing, oldest excluded
    assert forecasts == sorted(forecasts, key=lambda r: r.observed_at)


def test_normalize_forecast_reading_values_and_provenance() -> None:
    provider = OpenMeteoProvider()
    forecasts = [r for r in provider.normalize(_fetch()) if r.kind == "forecast"]
    hourly = [r for r in forecasts if r.source == "v1/forecast/hourly"]
    first = hourly[0]
    assert first.provider == "open-meteo"
    assert first.model is None
    assert "temperature" in first.values
    assert first.values["temperature"].unit == "degC"
    assert "precipitation_probability" in first.values
    assert first.values["precipitation_probability"].unit == "percent"


def test_normalize_yields_minutely_15_forecasts_with_a_distinct_source() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    current = next(r for r in readings if r.kind == "model")
    minutely = [r for r in readings if r.source == "v1/forecast/minutely_15"]
    minutely_times = FIXTURE_DOCUMENT["minutely_15"]["time"]
    expected = sum(
        1
        for text in minutely_times
        if datetime.fromisoformat(text).replace(tzinfo=UTC) > current.observed_at
    )
    assert len(minutely) == expected
    assert all(reading.kind == "forecast" for reading in minutely)
    assert all(reading.observed_at > current.observed_at for reading in minutely)
    assert "temperature" in minutely[0].values


def test_normalize_yields_daily_forecasts_with_a_distinct_source_and_x_fallback() -> None:
    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    daily = [r for r in readings if r.source == "v1/forecast/daily"]
    # fixture's daily.time[0] is "today" (before current.time), so it is
    # excluded; the remaining 6 future days are all forecasts.
    assert len(daily) == len(FIXTURE_DOCUMENT["daily"]["time"]) - 1
    first = daily[0]
    assert first.kind == "forecast"
    assert "temperature_max" in first.values
    assert first.values["temperature_max"].unit == "degC"
    assert "temperature_min" in first.values
    assert "precipitation" in first.values
    # uv_index_max has no vocabulary row: "no provider value is dropped"
    assert "x_uv_index_max" in first.values
    assert first.values["x_uv_index_max"].unit == "index"
    assert (
        first.values["x_uv_index_max"].original_value
        == FIXTURE_DOCUMENT["daily"]["uv_index_max"][1]
    )


# --- normalize: failure / empty cases ---------------------------------------


def test_normalize_returns_no_readings_for_a_non_200_status() -> None:
    provider = OpenMeteoProvider()
    record = _fetch(status=304, body=b"")
    assert provider.normalize(record) == ()


def test_normalize_returns_no_readings_for_a_transport_error() -> None:
    provider = OpenMeteoProvider()
    record = _fetch(status=None, body=b"", error=FetchError(kind="timeout"))
    assert provider.normalize(record) == ()


def test_normalize_drops_no_provider_variable() -> None:
    """Every variable key the fixture's four blocks carry ends up under some id.

    docs/weather-api.md section 4: "No provider value is dropped." A mapped
    variable lands under its vocabulary id; an unmapped one (only
    ``uv_index_max`` today) lands under ``x_<name>``.
    """
    from climate.weather.providers.open_meteo import _VARIABLE_MAP

    provider = OpenMeteoProvider()
    readings = provider.normalize(_fetch())
    ids_by_source: dict[str, set[str]] = {}
    for reading in readings:
        ids_by_source.setdefault(reading.source, set()).update(reading.values.keys())

    def expected_ids(block: dict) -> set[str]:
        keys = set(block.keys()) - {"time", "interval"}
        return {_VARIABLE_MAP[key][0] if key in _VARIABLE_MAP else f"x_{key}" for key in keys}

    assert expected_ids(FIXTURE_DOCUMENT["current"]) <= ids_by_source["v1/forecast/current"]
    assert expected_ids(FIXTURE_DOCUMENT["minutely_15"]) <= ids_by_source["v1/forecast/minutely_15"]
    assert expected_ids(FIXTURE_DOCUMENT["hourly"]) <= ids_by_source["v1/forecast/hourly"]
    assert expected_ids(FIXTURE_DOCUMENT["daily"]) <= ids_by_source["v1/forecast/daily"]
    assert "x_uv_index_max" in ids_by_source["v1/forecast/daily"]


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
