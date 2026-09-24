"""Tests for the weather HTTP API route handlers (climate/weather/web/api.py).

Every handler is exercised directly with an ``InMemoryWeatherStore`` — no
socket is ever opened (blocked globally by tests/conftest.py anyway). A
dedicated pair of fake providers stands in for real adapters, matching the
merged ``climate.weather.providers.base`` contract.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from climate.weather import tracker, vocabulary
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
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT, fake_secret

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


# --- availability comes from the tracker, not from this process ---------------
#
# Regression: the web container is not given the provider credentials (only
# the tracker service has the ``env_file``), so evaluating availability here
# reported a provider that was collecting perfectly well as "disabled, no
# credential" — on this route, in /health and on the dashboard built from them.


def _tracker_says(store, provider_id: str, *, enabled: bool, credential_present, at=NOW) -> None:
    """Write a heartbeat whose availability snapshot covers one provider."""
    store.save_heartbeat(
        "9.9.9",
        at,
        {
            provider_id: {
                "enabled": enabled,
                "reason": None if enabled else "disabled in configuration",
                "credential_present": credential_present,
            }
        },
    )


def test_providers_prefers_the_trackers_snapshot_over_this_process_environment(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)  # as in the web container
    _tracker_says(store, "test-obs", enabled=True, credential_present=True)

    status, body = api.list_providers(store, config, providers, {}, NOW)

    assert status == 200
    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is True
    assert row["credential_present"] is True
    assert row["enabled_reason"] is None
    assert row["availability_source"] == "tracker"


def test_health_prefers_the_trackers_snapshot_and_raises_no_disabled_warning(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)
    _tracker_says(store, "test-obs", enabled=True, credential_present=True)

    status, body = api.health(store, config, providers, {}, NOW)

    assert status == 200
    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is True
    assert row["availability_source"] == "tracker"
    assert not [w for w in body["warnings"] if w["code"] == "provider_disabled"]


def test_providers_falls_back_to_this_process_and_says_so(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)  # and no heartbeat at all

    status, body = api.list_providers(store, config, providers, {}, NOW)

    assert status == 200
    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is False
    assert row["credential_present"] is False
    assert row["availability_source"] == "web"
    assert body["warnings"] == []


def test_a_provider_missing_from_the_snapshot_falls_back_on_its_own(
    store, config, providers
) -> None:
    _tracker_says(store, "test-obs", enabled=True, credential_present=True)

    status, body = api.list_providers(store, config, providers, {}, NOW)

    rows = {row["provider"]: row for row in body["providers"]}
    assert rows["test-obs"]["availability_source"] == "tracker"
    assert rows["test-model"]["availability_source"] == "web"


def test_an_old_heartbeat_is_still_used_but_its_age_is_reported(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.delenv("CLIMATE_TEST_OBS_TOKEN", raising=False)
    age = api.HEARTBEAT_STALE_SECONDS + 60
    _tracker_says(
        store,
        "test-obs",
        enabled=True,
        credential_present=True,
        at=NOW - timedelta(seconds=age),
    )

    status, body = api.list_providers(store, config, providers, {}, NOW)

    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is True  # unknown-but-reported, not discarded
    assert row["availability_source"] == "tracker"
    warning = next(w for w in body["warnings"] if w["code"] == "store_degraded")
    assert str(age) in warning["message"]


def test_a_disabled_provider_in_the_snapshot_is_reported_disabled(
    store, config, providers, monkeypatch
) -> None:
    monkeypatch.setenv("CLIMATE_TEST_OBS_TOKEN", fake_secret("obs-token"))  # this process has one
    _tracker_says(store, "test-obs", enabled=False, credential_present=False)

    status, body = api.list_providers(store, config, providers, {}, NOW)

    row = next(r for r in body["providers"] if r["provider"] == "test-obs")
    assert row["enabled"] is False
    assert row["credential_present"] is False
    assert row["availability_source"] == "tracker"


def test_no_route_ever_echoes_the_credential_value(store, config, providers, monkeypatch) -> None:
    secret = fake_secret("obs-token")
    monkeypatch.setenv("CLIMATE_TEST_OBS_TOKEN", secret)
    snapshot = tracker.availability_snapshot(providers, config, {"CLIMATE_TEST_OBS_TOKEN": secret})
    store.save_heartbeat("9.9.9", NOW, snapshot)

    stored = store.latest_heartbeat()
    assert stored["providers"]["test-obs"]["credential_present"] is True
    assert secret not in json.dumps(stored, default=str)

    for handler, params in (
        (api.health, {}),
        (api.list_providers, {}),
        (api.list_locations, {}),
        (api.latest, {}),
        (api.series, {"variable": ["temperature"]}),
        (api.forecast, {}),
        (api.stats, {}),
    ):
        status, body = handler(store, config, providers, params, NOW)
        assert status == 200
        assert secret not in json.dumps(body, default=str)


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


def test_locations_newest_is_max_across_providers_and_kinds_via_narrow_lookups(
    store, config, providers, monkeypatch
) -> None:
    """newest_observed_at is the max over every (provider, kind), and each
    latest_reading lookup names provider AND kind so Mongo can serve it from
    the readings compound index instead of sorting the label's history."""
    newest = NOW + timedelta(hours=3)
    for provider, kind, observed_at in (
        ("test-model", "model", NOW - timedelta(minutes=5)),
        ("test-model", "forecast", newest),
        ("test-obs", "observation", NOW - timedelta(minutes=1)),
    ):
        _save_reading(
            store,
            provider=provider,
            source="v1/forecast",
            location=NEUTRAL_LABEL,
            observed_at=observed_at,
            requested_at=NOW - timedelta(minutes=5),
            kind=kind,
            values={"temperature": (20.0, "degC")},
        )
    calls = []
    real_latest = store.latest_reading

    def spy(**kwargs):
        calls.append(kwargs)
        return real_latest(**kwargs)

    monkeypatch.setattr(store, "latest_reading", spy)
    _, body = api.list_locations(store, config, providers, {}, NOW)
    rows = {row["location"]: row for row in body["locations"]}
    assert rows[NEUTRAL_LABEL]["newest_observed_at"] == api._format_time(newest)
    assert calls
    assert all(call.get("provider") and call.get("kind") for call in calls)


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


def test_series_rejects_a_window_longer_than_the_documented_maximum(
    store, config, providers
) -> None:
    """qodo-4: an unbounded span is what let the store query run unbounded."""
    span = timedelta(seconds=api.MAX_WINDOW_SPAN_SECONDS + 1)
    query = {
        "variable": ["temperature"],
        "from": [api._format_time(NOW - span)],
        "to": [api._format_time(NOW)],
    }
    status, body = api.series(store, config, providers, query, NOW)
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"
    assert str(api.MAX_WINDOW_SPAN_SECONDS) in body["error"]["message"]
    assert body["error"]["detail"]["max_span_seconds"] == api.MAX_WINDOW_SPAN_SECONDS


def test_series_multi_year_window_at_minimum_step_answers_promptly(
    store, config, providers
) -> None:
    """qodo-4: the old handler built the whole grid before applying max_points.

    A multi-year window at the 60 s minimum names millions of buckets. The
    span guard is arithmetic, so the answer is immediate rather than after
    millions of ``datetime`` allocations.
    """
    query = {
        "variable": ["temperature"],
        "from": [api._format_time(NOW - timedelta(days=5 * 365))],
        "to": [api._format_time(NOW)],
        "step": [str(api.MIN_SERIES_STEP_SECONDS)],
    }
    started = time.perf_counter()
    status, body = api.series(store, config, providers, query, NOW)
    elapsed = time.perf_counter() - started
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_series_largest_allowed_window_at_minimum_step_answers_promptly(
    store, config, providers
) -> None:
    """The widest *accepted* window is still half a million buckets at 60 s;
    only ``max_points`` of them may ever be allocated."""
    query = {
        "variable": ["temperature"],
        "from": [api._format_time(NOW - timedelta(seconds=api.MAX_WINDOW_SPAN_SECONDS))],
        "to": [api._format_time(NOW)],
        "step": [str(api.MIN_SERIES_STEP_SECONDS)],
        # Ask for more than the ceiling: it is clamped, never honoured.
        "max_points": [str(api.MAX_SERIES_POINTS * 100)],
    }
    started = time.perf_counter()
    status, body = api.series(store, config, providers, query, NOW)
    elapsed = time.perf_counter() - started
    assert status == 200
    assert body["point_count"] == api.MAX_SERIES_POINTS
    assert any(w["code"] == "truncated" for w in body["warnings"])
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_series_truncation_keeps_the_earliest_buckets_and_narrows_to(
    store, config, providers
) -> None:
    from_dt = NOW - timedelta(hours=1)
    query = {
        "variable": ["temperature"],
        "from": [api._format_time(from_dt)],
        "to": [api._format_time(NOW)],
        "step": ["60"],
        "max_points": ["5"],
    }
    status, body = api.series(store, config, providers, query, NOW)
    assert status == 200
    assert body["point_count"] == 5
    assert body["from"] == api._format_time(from_dt)
    # The window the response actually covers, not the one that was asked for.
    assert body["to"] == api._format_time(from_dt + timedelta(minutes=5))


def test_series_bounds_the_store_query_with_a_limit(store, config, providers) -> None:
    """qodo-4: the store query used to have no limit at all."""
    seen: dict[str, object] = {}

    class _RecordingStore(InMemoryWeatherStore):
        def series(self, variable, **kwargs):
            seen.update(kwargs)
            return super().series(variable, **kwargs)

    recording = _RecordingStore()
    status, _ = api.series(recording, config, providers, {"variable": ["temperature"]}, NOW)
    assert status == 200
    assert seen["limit"] == api.MAX_SERIES_STORE_POINTS


# --- the shared vocabulary (climate/weather/vocabulary.py) --------------------


@pytest.mark.parametrize(
    "variable",
    ["showers", "snowfall", "cloud_cover_low", "uv_index_clear_sky", "is_day", "temperature_max"],
)
def test_extended_vocabulary_variables_are_queryable(store, config, providers, variable) -> None:
    """qodo-5: these were stored by the adapters but rejected by the API."""
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=5),
        requested_at=NOW - timedelta(minutes=5),
        kind="model",
        values={variable: (1.0, vocabulary.VARIABLES[variable])},
    )
    status, body = api.latest(store, config, providers, {"variables": [variable]}, NOW)
    assert status == 200
    assert body["readings"][0]["values"][variable]["value"] == 1.0

    status, body = api.series(store, config, providers, {"variable": [variable]}, NOW)
    assert status == 200
    assert body["unit"] == vocabulary.VARIABLES[variable]


def test_series_serves_an_extension_variable_with_its_stored_unit(store, config, providers) -> None:
    """Section 4: ``x_`` variables are never dropped, so they stay queryable."""
    _save_reading(
        store,
        provider="test-model",
        source="v1/forecast",
        location=NEUTRAL_LABEL,
        observed_at=NOW - timedelta(minutes=5),
        requested_at=NOW - timedelta(minutes=5),
        kind="model",
        values={"x_snow_depth": (0.12, "m")},
    )
    status, body = api.series(store, config, providers, {"variable": ["x_snow_depth"]}, NOW)
    assert status == 200
    assert body["unit"] == "m"
    assert body["series"][0]["unit"] == "m"


def test_series_extension_variable_with_no_data_falls_back_to_other(
    store, config, providers
) -> None:
    status, body = api.series(store, config, providers, {"variable": ["x_unheard_of"]}, NOW)
    assert status == 200
    assert body["unit"] == "other"
    assert body["series"] == []


def test_a_bare_x_prefix_is_still_an_unknown_variable(store, config, providers) -> None:
    status, body = api.series(store, config, providers, {"variable": ["x_"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "unknown_variable"


def test_api_variable_units_is_the_shared_vocabulary(store, config, providers) -> None:
    assert api.VARIABLE_UNITS is vocabulary.VARIABLES


# --- /forecast ----------------------------------------------------------------------


def test_forecast_returns_stored_horizon(store, config, providers) -> None:
    issued_at = NOW - timedelta(hours=1)
    requested_at = issued_at
    # This provider states no model-run time, so issued_at falls back to the
    # fetch time and the entry says so (see the dedicated tests below).
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
    # This fixture stores no model_run_at, so the issue time is the fetch
    # time and the response admits the substitution.
    assert forecast_entry["provenance"]["model_run_at"] is None
    assert forecast_entry["issued_at_estimated"] is True
    assert forecast_entry["issued_at"] == api._format_time(requested_at)
    # Still a documented gap: Reading has no station field.
    assert forecast_entry["provenance"]["station"] is None


def _save_forecast(
    store: InMemoryWeatherStore,
    *,
    requested_at: datetime,
    valid_ats: tuple[datetime, ...],
    model_run_at: datetime | None = None,
    variable: str = "temperature",
    unit: str = "degC",
) -> str:
    fetch_id = store.save_fetch(
        FetchRecord(
            provider="test-model",
            endpoint="https://example.test/test-model",
            location=NEUTRAL_LABEL,
            requested_at=requested_at,
            status=200,
            body=b"{}",
        )
    )
    store.save_readings(
        fetch_id,
        [
            Reading(
                provider="test-model",
                source="v1/forecast",
                model="best_match",
                location=NEUTRAL_LABEL,
                observed_at=valid_at,
                requested_at=requested_at,
                model_run_at=model_run_at,
                kind="forecast",
                values={variable: Measurement(value=float(index), unit=unit)},
            )
            for index, valid_at in enumerate(valid_ats)
        ],
    )
    return fetch_id


def _forecast_entry(store, config, providers, **extra) -> dict:
    query = {"location": [NEUTRAL_LABEL], "provider": ["test-model"], **extra}
    status, body = api.forecast(store, config, providers, query, NOW)
    assert status == 200
    return body["forecasts"][0]


def test_forecast_issue_time_is_the_providers_model_run_not_the_fetch(
    store, config, providers
) -> None:
    """qodo-14: the tracker's download time is not the model's issue time."""
    model_run_at = NOW - timedelta(hours=7)
    requested_at = NOW - timedelta(hours=6)
    _save_forecast(
        store,
        requested_at=requested_at,
        model_run_at=model_run_at,
        valid_ats=(NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
    )
    entry = _forecast_entry(store, config, providers)
    assert entry["issued_at"] == api._format_time(model_run_at)
    assert entry["issued_at_estimated"] is False
    assert entry["provenance"]["model_run_at"] == api._format_time(model_run_at)
    assert entry["requested_at"] == api._format_time(requested_at)
    assert entry["points"][0]["lead_seconds"] == 8 * 3600


def test_forecast_horizon_is_measured_from_now_not_from_the_issue(store, config, providers) -> None:
    """qodo-14: a six-hour-old issue still carries valid future points.

    Measured from the issue time a ``horizon_hours=3`` window ended three
    hours before ``now`` and discarded every one of them.
    """
    _save_forecast(
        store,
        requested_at=NOW - timedelta(hours=6),
        valid_ats=(NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
    )
    entry = _forecast_entry(store, config, providers, horizon_hours=["3"])
    assert entry["point_count"] == 2
    assert entry["points"][0]["valid_at"] == api._format_time(NOW + timedelta(hours=1))


def test_forecast_excludes_points_already_in_the_past(store, config, providers) -> None:
    _save_forecast(
        store,
        requested_at=NOW - timedelta(hours=6),
        valid_ats=(
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
        ),
    )
    entry = _forecast_entry(store, config, providers, horizon_hours=["6"])
    assert [point["valid_at"] for point in entry["points"]] == [
        api._format_time(NOW + timedelta(hours=1))
    ]


def test_a_fully_expired_forecast_is_no_data_not_an_empty_entry(store, config, providers) -> None:
    """Every point is in the past, so the issue carries no forecast at all.

    The store is asked only for points from ``now`` onwards — reading a
    provider's whole stored forecast history to discard it would be work
    done for nothing.
    """
    _save_forecast(
        store,
        requested_at=NOW - timedelta(days=2),
        valid_ats=(NOW - timedelta(hours=5), NOW - timedelta(hours=4)),
    )
    status, body = api.forecast(
        store, config, providers, {"location": [NEUTRAL_LABEL], "provider": ["test-model"]}, NOW
    )
    assert status == 200
    assert body["forecasts"] == []
    assert any(w["code"] == "no_data" for w in body["warnings"])


def test_forecast_reports_an_extension_variables_stored_unit(store, config, providers) -> None:
    _save_forecast(
        store,
        requested_at=NOW - timedelta(hours=1),
        valid_ats=(NOW + timedelta(hours=1),),
        variable="x_snow_depth",
        unit="m",
    )
    entry = _forecast_entry(store, config, providers, variables=["x_snow_depth"])
    assert entry["variables"] == ["x_snow_depth"]
    assert entry["units"] == {"x_snow_depth": "m"}


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


def _save_fetches(store: InMemoryWeatherStore, count: int, *, spacing_seconds: int = 900) -> None:
    """``count`` bare fetch records ending just before ``NOW``."""
    for index in range(count):
        store.save_fetch(
            FetchRecord(
                provider="test-model",
                endpoint="https://example.test/test-model",
                location=NEUTRAL_LABEL,
                requested_at=NOW - timedelta(seconds=(index + 1) * spacing_seconds),
                status=200,
                body=b"{}",
            )
        )


def test_stats_rejects_a_window_longer_than_the_documented_maximum(
    store, config, providers
) -> None:
    """Same defect class as the /series span: an untrusted window with no
    ``bucket`` iterated the store with nothing bounding it."""
    span = timedelta(seconds=api.MAX_WINDOW_SPAN_SECONDS + 1)
    query = {"from": [api._format_time(NOW - span)], "to": [api._format_time(NOW)]}
    status, body = api.stats(store, config, providers, query, NOW)
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"
    assert str(api.MAX_WINDOW_SPAN_SECONDS) in body["error"]["message"]
    assert body["error"]["detail"]["max_span_seconds"] == api.MAX_WINDOW_SPAN_SECONDS
    assert body["error"]["detail"]["span_seconds"] == api.MAX_WINDOW_SPAN_SECONDS + 1


def test_stats_rejects_a_multi_year_window_spelled_as_a_duration(store, config, providers) -> None:
    status, body = api.stats(store, config, providers, {"window": ["3650d"]}, NOW)
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"


def test_stats_multi_year_window_answers_promptly(store, config, providers) -> None:
    _save_fetches(store, 2000)
    query = {
        "from": [api._format_time(NOW - timedelta(days=5 * 365))],
        "to": [api._format_time(NOW)],
    }
    started = time.perf_counter()
    status, body = api.stats(store, config, providers, query, NOW)
    elapsed = time.perf_counter() - started
    assert status == 400
    assert body["error"]["code"] == "invalid_parameter"
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_stats_largest_allowed_window_answers_promptly_over_many_records(
    store, config, providers
) -> None:
    """The widest accepted window, the finest bucket grid, a full store."""
    _save_fetches(store, 3000, spacing_seconds=60)
    query = {
        "provider": ["test-model"],
        "location": [NEUTRAL_LABEL],
        "from": [api._format_time(NOW - timedelta(seconds=api.MAX_WINDOW_SPAN_SECONDS))],
        "to": [api._format_time(NOW)],
        "bucket": [str(api.MAX_WINDOW_SPAN_SECONDS // api.MAX_STATS_BUCKETS + 1)],
    }
    started = time.perf_counter()
    status, body = api.stats(store, config, providers, query, NOW)
    elapsed = time.perf_counter() - started
    assert status == 200
    row = body["providers"][0]
    assert row["stored_count"] == 3000
    assert sum(b["stored_count"] for b in row["buckets"]) == 3000
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_stats_uses_a_count_query_and_a_bounded_record_read(store, config, providers) -> None:
    seen: dict[str, object] = {}

    class _RecordingStore(InMemoryWeatherStore):
        def iter_fetches(self, **kwargs):
            seen.update(kwargs)
            return super().iter_fetches(**kwargs)

    recording = _RecordingStore()
    status, _ = api.stats(recording, config, providers, {}, NOW)
    assert status == 200
    assert seen["limit"] == api.MAX_STATS_FETCH_RECORDS


def test_stats_caps_the_records_it_reads_and_says_so(store, config, providers, monkeypatch) -> None:
    monkeypatch.setattr(api, "MAX_STATS_FETCH_RECORDS", 3)
    _save_fetches(store, 10, spacing_seconds=60)
    query = {"provider": ["test-model"], "location": [NEUTRAL_LABEL], "window": ["24h"]}
    status, body = api.stats(store, config, providers, query, NOW)
    assert status == 200
    row = body["providers"][0]
    # Exact, because it comes from a count query rather than the records.
    assert row["stored_count"] == 10
    # Derived from the newest 3 records only.
    assert row["ok_count"] == 3
    truncated = next(w for w in body["warnings"] if w["code"] == "truncated")
    assert truncated["provider"] == "test-model"


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


def test_forecast_points_survive_a_non_round_fetch_time(store, config, providers) -> None:
    """Found by live validation: a real fetch happens at e.g. 19:33:37, and
    top-of-the-hour forecast points are never a whole number of steps after
    that, so a lead-relative step filter returned every forecast empty."""
    top_of_hour = NOW.replace(minute=0, second=0, microsecond=0)
    requested_at = top_of_hour - timedelta(minutes=26, seconds=23)
    fetch_id = store.save_fetch(
        FetchRecord(
            provider="test-model",
            endpoint="https://example.test/test-model",
            location=NEUTRAL_LABEL,
            requested_at=requested_at,
            status=200,
            body=b"{}",
        )
    )
    store.save_readings(
        fetch_id,
        [
            Reading(
                provider="test-model",
                source="v1/forecast/hourly",
                model="best_match",
                location=NEUTRAL_LABEL,
                observed_at=top_of_hour + timedelta(hours=hour),
                requested_at=requested_at,
                kind="forecast",
                values={"temperature": Measurement(value=20.0 + hour, unit="degC")},
            )
            for hour in (1, 2, 3)
        ],
    )
    status, body = api.forecast(
        store,
        config,
        providers,
        {"location": [NEUTRAL_LABEL], "provider": ["test-model"], "horizon_hours": ["6"]},
        NOW,
    )
    assert status == 200
    entry = body["forecasts"][0]
    assert entry["point_count"] == 3
    assert [point["values"]["temperature"] for point in entry["points"]] == [21.0, 22.0, 23.0]
