"""Tests for the weather HTTP API route handlers (climate/weather/web/api.py).

Every handler is exercised directly with an ``InMemoryWeatherStore`` — no
socket is ever opened (blocked globally by tests/conftest.py anyway). A
dedicated pair of fake providers stands in for real adapters, matching the
merged ``climate.weather.providers.base`` contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from climate.weather.config import Location, WeatherConfig
from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Capability,
    FreshnessStrategy,
    Quota,
    WeatherProvider,
)
from climate.weather.store import FetchRecord, InMemoryWeatherStore, Measurement, Reading
from climate.weather.web import api
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

NOW = datetime(2026, 9, 17, 8, 35, 0, tzinfo=UTC)
OTHER_LABEL = "office"
OTHER_POINT = (0.0, 0.0)


class _ModelProvider(WeatherProvider):
    id = "test-model"
    capabilities = frozenset({Capability.CURRENT_MODEL, Capability.FORECAST})
    auth = AuthRequirement(required=False)
    default_interval_seconds = 900
    quota = Quota(calls_per_day=1000, source="test")
    freshness = FreshnessStrategy.INTERVAL
    attribution = Attribution(text="Test Model", url="https://example.test/model-terms")

    def build_requests(self, location, settings=None):
        return []

    def normalize(self, fetch_record):
        return []


class _ObsProvider(WeatherProvider):
    id = "test-obs"
    capabilities = frozenset({Capability.CURRENT_OBSERVATION})
    auth = AuthRequirement(required=True, env_var="CLIMATE_TEST_OBS_TOKEN")
    default_interval_seconds = 1800
    quota = Quota(unlimited=True, source="test")
    freshness = FreshnessStrategy.STATION_CADENCE
    attribution = Attribution(text="Test Obs", url="https://example.test/obs-terms")

    def build_requests(self, location, settings=None):
        return []

    def normalize(self, fetch_record):
        return []


@pytest.fixture
def store() -> InMemoryWeatherStore:
    return InMemoryWeatherStore()


@pytest.fixture
def providers() -> tuple[WeatherProvider, ...]:
    return (_ModelProvider(), _ObsProvider())


@pytest.fixture
def config() -> WeatherConfig:
    return WeatherConfig(
        locations={
            NEUTRAL_LABEL: Location(NEUTRAL_LABEL, *NEUTRAL_POINT),
            OTHER_LABEL: Location(OTHER_LABEL, *OTHER_POINT),
        }
    )


@pytest.fixture(autouse=True)
def _obs_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIMATE_TEST_OBS_TOKEN", "secret")


def _save_reading(
    store: InMemoryWeatherStore,
    *,
    provider: str,
    source: str,
    location: str,
    observed_at: datetime,
    requested_at: datetime,
    kind: str,
    values: dict[str, tuple[float, str]],
    model: str | None = None,
    status: int = 200,
    body: bytes = b"{}",
) -> str:
    fetch = FetchRecord(
        provider=provider,
        endpoint=f"https://example.test/{provider}",
        location=location,
        requested_at=requested_at,
        status=status,
        body=body,
    )
    fetch_id = store.save_fetch(fetch)
    reading = Reading(
        provider=provider,
        source=source,
        model=model,
        location=location,
        observed_at=observed_at,
        requested_at=requested_at,
        kind=kind,
        values={name: Measurement(value=v, unit=u) for name, (v, u) in values.items()},
    )
    store.save_readings(fetch_id, [reading])
    return fetch_id


# --- dispatch / transport-agnostic behaviour --------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health",
        "/api/v1/providers",
        "/api/v1/locations",
        "/api/v1/latest",
        "/api/v1/series",
        "/api/v1/forecast",
        "/api/v1/stats",
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_returns_405(store, config, providers, path, method) -> None:
    status, body = api.dispatch(
        method, path, {}, store=store, config=config, providers=providers, now=NOW
    )
    assert status == 405
    assert body["error"]["code"] == "method_not_allowed"
    assert body["error"]["status"] == 405


def test_unknown_route_returns_404(store, config, providers) -> None:
    status, body = api.dispatch(
        "GET", "/api/v1/nope", {}, store=store, config=config, providers=providers, now=NOW
    )
    assert status == 404
    assert body["error"]["code"] == "not_found"


# --- /health -----------------------------------------------------------------


def test_health_ok_with_fresh_fetch(store, config, providers) -> None:
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(seconds=30),
        requested_at=NOW - timedelta(seconds=30),
        kind="model",
        values={"temperature": (21.0, "degC")},
    )
    status, body = api.health(store, config, providers, {}, NOW)
    assert status == 200
    assert body["status"] == "ok"
    assert body["api_version"] == "v1"
    assert body["store"]["reachable"] is True
    assert body["store"]["fetch_count"] == 1
    assert body["newest_fetch"]["provider"] == "test-model"
    # tracker_version is a documented contract gap: always null.
    assert body["tracker_version"] is None
    provider_rows = {row["provider"]: row for row in body["providers"]}
    assert provider_rows["test-model"]["enabled"] is True
    assert provider_rows["test-obs"]["enabled"] is True


def test_health_degraded_with_no_data(store, config, providers) -> None:
    status, body = api.health(store, config, providers, {}, NOW)
    assert status == 200
    assert body["status"] == "degraded"
    assert body["store"]["fetch_count"] == 0
    assert body["newest_fetch"] == {"requested_at": None, "age_seconds": None, "provider": None}


def test_health_reports_disabled_provider_with_reason(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)
    status, body = api.health(store, config, providers, {}, NOW)
    assert status == 200
    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is False
    warning = next(w for w in body["warnings"] if w["provider"] == "test-obs")
    assert warning["code"] == "provider_disabled"


# --- /providers ---------------------------------------------------------------


def test_providers_lists_metadata(store, config, providers) -> None:
    status, body = api.list_providers(store, config, providers, {}, NOW)
    assert status == 200
    rows = {row["provider"]: row for row in body["providers"]}
    assert set(rows) == {"test-model", "test-obs"}
    model = rows["test-model"]
    assert model["kind"] in ("model", "forecast")
    assert "model" in model["capabilities"]["kinds"]
    assert "forecast" in model["capabilities"]["kinds"]
    assert model["freshness"]["strategy"] == "interval"
    assert model["attribution"]["text"] == "Test Model"
    assert model["quota"]["calls_per_day"] == 1000


def test_providers_filters_by_enabled(store, config, providers, monkeypatch) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)
    status, body = api.list_providers(store, config, providers, {"enabled": ["false"]}, NOW)
    assert status == 200
    assert [row["provider"] for row in body["providers"]] == ["test-obs"]


def test_providers_unknown_id_is_400(store, config, providers) -> None:
    status, body = api.list_providers(store, config, providers, {"provider": ["nope"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "unknown_provider"


# --- /locations ----------------------------------------------------------------


def test_locations_reports_reading_count_and_newest_observed(store, config, providers) -> None:
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=5),
        requested_at=NOW - timedelta(minutes=5),
        kind="model",
        values={"temperature": (20.0, "degC")},
    )
    status, body = api.list_locations(store, config, providers, {}, NOW)
    assert status == 200
    rows = {row["location"]: row for row in body["locations"]}
    assert rows[NEUTRAL_LABEL]["reading_count"] == 1
    assert rows[NEUTRAL_LABEL]["newest_observed_at"] is not None
    assert rows[OTHER_LABEL]["reading_count"] == 0
    assert rows[OTHER_LABEL]["newest_observed_at"] is None


# --- /latest ---------------------------------------------------------------------


def test_latest_returns_fully_annotated_reading(store, config, providers) -> None:
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=5),
        requested_at=NOW - timedelta(minutes=4),
        kind="model",
        model="best_match",
        values={"temperature": (21.5, "degC"), "relative_humidity": (55.0, "percent")},
    )
    status, body = api.latest(
        store, config, providers, {"provider": ["test-model"], "location": [NEUTRAL_LABEL]}, NOW
    )
    assert status == 200
    assert body["stale"] is False
    assert len(body["readings"]) == 1
    reading = body["readings"][0]
    assert reading["provider"] == "test-model"
    assert reading["location"] == NEUTRAL_LABEL
    value = reading["values"]["temperature"]
    for key in ("unit", "observed_at", "age_seconds", "provenance", "stale", "stale_after_seconds"):
        assert key in value
    assert value["unit"] == "degC"
    assert value["age_seconds"] == 300
    assert reading["values"]["relative_humidity"]["value"] == 55.0
    assert body["missing"] == []


def test_latest_flags_stale_beyond_max_age(store, config, providers) -> None:
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=30),
        requested_at=NOW - timedelta(minutes=30),
        kind="model",
        values={"temperature": (21.5, "degC")},
    )
    status, body = api.latest(
        store,
        config,
        providers,
        {"provider": ["test-model"], "location": [NEUTRAL_LABEL], "max_age": ["600"]},
        NOW,
    )
    assert status == 200
    assert body["stale"] is True
    assert body["max_age_seconds"] == 600
    assert body["readings"][0]["stale"] is True
    assert body["readings"][0]["stale_after_seconds"] == 600


def test_latest_reports_missing_pair(store, config, providers) -> None:
    status, body = api.latest(
        store, config, providers, {"provider": ["test-model"], "location": [NEUTRAL_LABEL]}, NOW
    )
    assert status == 200
    assert body["readings"] == []
    assert body["missing"] == [
        {"provider": "test-model", "location": NEUTRAL_LABEL, "reason": "no_data"}
    ]


def test_latest_rejects_forecast_kind(store, config, providers) -> None:
    status, body = api.latest(store, config, providers, {"kind": ["forecast"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "unknown_kind"


def test_latest_rejects_unknown_variable(store, config, providers) -> None:
    status, body = api.latest(store, config, providers, {"variables": ["not_a_variable"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "unknown_variable"


def test_latest_rejects_unknown_location(store, config, providers) -> None:
    status, body = api.latest(store, config, providers, {"location": ["nowhere"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "unknown_location"


# --- /series -----------------------------------------------------------------------


def test_series_requires_variable(store, config, providers) -> None:
    status, body = api.series(store, config, providers, {}, NOW)
    assert status == 400
    assert body["error"]["code"] == "missing_parameter"


def test_series_rejects_from_after_to(store, config, providers) -> None:
    query = {
        "variable": ["temperature"],
        "from": ["2026-09-17T08:00:00Z"],
        "to": ["2026-09-17T07:00:00Z"],
    }
    status, body = api.series(store, config, providers, query, NOW)
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"


def test_series_grid_carries_explicit_nulls_for_missed_ticks(store, config, providers) -> None:
    base = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=base,
        requested_at=base,
        kind="model",
        values={"temperature": (20.0, "degC")},
    )
    # Deliberately no reading at base + 15m: that tick must come back null.
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=base + timedelta(minutes=30),
        requested_at=base + timedelta(minutes=30),
        kind="model",
        values={"temperature": (22.0, "degC")},
    )
    query = {
        "variable": ["temperature"],
        "provider": ["test-model"],
        "location": [NEUTRAL_LABEL],
        "from": [api._format_time(base)],
        "to": [api._format_time(base + timedelta(minutes=45))],
        "step": ["900"],
    }
    status, body = api.series(store, config, providers, query, NOW)
    assert status == 200
    assert body["variable"] == "temperature"
    assert body["unit"] == "degC"
    assert len(body["series"]) == 1
    entry = body["series"][0]
    assert entry["kind"] == "model"
    points = entry["points"]
    assert points[0]["value"] == 20.0
    assert points[1]["value"] is None
    assert points[1]["observed_at"] is None
    assert points[1]["fetch_id"] is None
    assert points[2]["value"] == 22.0
    assert entry["null_count"] == 1
    assert entry["value_count"] == 2


def test_series_truncates_to_max_points_with_warning(store, config, providers) -> None:
    query = {
        "variable": ["temperature"],
        "from": [api._format_time(NOW - timedelta(hours=1))],
        "to": [api._format_time(NOW)],
        "step": ["60"],
        "max_points": ["5"],
    }
    status, body = api.series(store, config, providers, query, NOW)
    assert status == 200
    assert body["point_count"] == 5
    assert any(w["code"] == "truncated" for w in body["warnings"])


# --- /forecast ----------------------------------------------------------------------


def test_forecast_returns_stored_horizon(store, config, providers) -> None:
    issued_at = NOW - timedelta(hours=1)
    requested_at = issued_at
    fetch = FetchRecord(
        provider="test-model",
        endpoint="https://example.test/test-model",
        location=NEUTRAL_LABEL,
        requested_at=requested_at,
        status=200,
        body=b"{}",
    )
    fetch_id = store.save_fetch(fetch)
    readings = []
    for hour in (1, 2, 3):
        readings.append(
            Reading(
                provider="test-model",
                source="v1/forecast",
                model="best_match",
                location=NEUTRAL_LABEL,
                observed_at=issued_at + timedelta(hours=hour),
                requested_at=requested_at,
                kind="forecast",
                values={"temperature": Measurement(value=20.0 + hour, unit="degC")},
            )
        )
    store.save_readings(fetch_id, readings)

    status, body = api.forecast(
        store,
        config,
        providers,
        {"location": [NEUTRAL_LABEL], "provider": ["test-model"], "horizon_hours": ["3"]},
        NOW,
    )
    assert status == 200
    assert len(body["forecasts"]) == 1
    forecast_entry = body["forecasts"][0]
    assert forecast_entry["provider"] == "test-model"
    assert forecast_entry["kind"] == "forecast"
    assert forecast_entry["point_count"] == 3
    assert forecast_entry["points"][0]["lead_seconds"] == 3600
    assert forecast_entry["points"][0]["values"]["temperature"] == 21.0
    assert forecast_entry["units"]["temperature"] == "degC"
    # Documented gaps: no separate model-run/station data on Reading.
    assert forecast_entry["provenance"]["model_run_at"] is None
    assert forecast_entry["provenance"]["station"] is None


def test_forecast_empty_is_200_with_warning(store, config, providers) -> None:
    status, body = api.forecast(store, config, providers, {}, NOW)
    assert status == 200
    assert body["forecasts"] == []
    assert any(w["code"] == "no_data" for w in body["warnings"])


# --- /stats -------------------------------------------------------------------------


def test_stats_computes_due_versus_stored(store, config, providers) -> None:
    for minutes_ago in (10, 25, 40):
        _save_reading(
            store,
            provider="test-model",
            source="v1/forecast",
            location=NEUTRAL_LABEL,
            observed_at=NOW - timedelta(minutes=minutes_ago),
            requested_at=NOW - timedelta(minutes=minutes_ago),
            kind="model",
            values={"temperature": (20.0, "degC")},
        )
    status, body = api.stats(
        store,
        config,
        providers,
        {"provider": ["test-model"], "location": [NEUTRAL_LABEL], "window": ["1h"]},
        NOW,
    )
    assert status == 200
    row = body["providers"][0]
    assert row["stored_count"] == 3
    assert row["due_count"] == int(3600 // 900)
    assert row["completeness"] == round(3 / row["due_count"], 4)
    assert row["ok_count"] == 3
    assert body["totals"]["stored_count"] == 3


def test_stats_rejects_bad_window(store, config, providers) -> None:
    status, body = api.stats(store, config, providers, {"window": ["nonsense"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"


def test_stats_bucket_must_meet_minimum(store, config, providers) -> None:
    status, body = api.stats(store, config, providers, {"bucket": ["10"]}, NOW)
    assert status == 400


# --- privacy: no coordinate ever leaves any route -----------------------------


def _no_coordinate_leak(payload: dict) -> None:
    blob = json.dumps(payload)
    for forbidden in ("latitude", "longitude", str(NEUTRAL_POINT[0])):
        assert forbidden not in blob, f"{forbidden!r} leaked in response: {blob}"


@pytest.mark.parametrize(
    "handler_name, query",
    [
        ("health", {}),
        ("list_providers", {}),
        ("list_locations", {}),
        ("latest", {}),
        ("series", {"variable": ["temperature"]}),
        ("forecast", {}),
        ("stats", {}),
    ],
)
def test_no_route_ever_leaks_a_coordinate(store, config, providers, handler_name, query) -> None:
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=5),
        requested_at=NOW - timedelta(minutes=5),
        kind="model",
        values={"temperature": (20.0, "degC")},
    )
    handler = getattr(api, handler_name)
    status, body = handler(store, config, providers, query, NOW)
    assert status == 200
    _no_coordinate_leak(body)


# --- dispatch-level: the full request/response envelope ----------------------


def test_dispatch_sets_headers_via_write_json_shape(store, config, providers) -> None:
    status, body = api.dispatch(
        "GET", "/api/v1/health", {}, store=store, config=config, providers=providers, now=NOW
    )
    assert status == 200
    assert body["api_version"] == "v1"


def test_dispatch_error_envelope_shape(store, config, providers) -> None:
    status, body = api.dispatch(
        "POST", "/api/v1/latest", {}, store=store, config=config, providers=providers, now=NOW
    )
    assert status == 405
    error = body["error"]
    assert set(error) == {"code", "message", "status", "detail"}
    assert error["status"] == 405
