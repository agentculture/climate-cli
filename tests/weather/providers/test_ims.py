"""Tests for the IMS (Israel Meteorological Service) Envista adapter.

Built against ``tests/fixtures/ims_stations.json`` and
``tests/fixtures/ims_latest.json`` — synthesized from the vendor PDF (no
token exists in this environment; see the module docstring and
``tests/fixtures/README.md``). The adapter never touches the network here:
fixture bytes are wrapped in a real :class:`~climate.weather.store.FetchRecord`
and handed to :meth:`ImsProvider.normalize` / :meth:`ImsProvider.build_requests`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from climate.weather.providers.base import Capability, FreshnessStrategy, validate_provider
from climate.weather.providers.ims import _CHANNEL_MAP, ImsProvider
from climate.weather.store import FetchRecord
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

TOKEN_ENV = "CLIMATE_IMS_API_TOKEN"
TOKEN = "test-token"

REQUESTED_AT = datetime(2026, 6, 15, 14, 5, tzinfo=UTC)


def _stations_body() -> bytes:
    return (FIXTURES / "ims_stations.json").read_bytes()


def _latest_body() -> bytes:
    return (FIXTURES / "ims_latest.json").read_bytes()


def _fetch(body: bytes, *, status: int = 200) -> FetchRecord:
    return FetchRecord(
        provider="ims",
        endpoint="https://api.ims.gov.il/v1/envista/stations",
        location=NEUTRAL_LABEL,
        requested_at=REQUESTED_AT,
        status=status,
        body=body,
    )


class _Location:
    def __init__(self, label: str, latitude: float, longitude: float) -> None:
        self.label = label
        self.latitude = latitude
        self.longitude = longitude


class _Settings:
    def __init__(self, params: dict | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.interval_seconds = None
        self.params = params or {}


# --- contract metadata ------------------------------------------------------


def test_declares_the_contract_this_task_owns() -> None:
    provider = ImsProvider()
    assert provider.id == "ims"
    assert Capability.CURRENT_OBSERVATION in provider.capabilities
    assert Capability.RADIATION in provider.capabilities
    assert Capability.STATION_METADATA in provider.capabilities
    assert provider.freshness is FreshnessStrategy.STATION_CADENCE
    assert provider.default_interval_seconds == 600
    assert provider.auth.required is True
    assert provider.auth.env_var == TOKEN_ENV
    assert validate_provider(provider) == []


def test_quota_is_declared_but_honestly_unverified() -> None:
    quota = ImsProvider.quota
    assert quota is not None
    assert quota.calls_per_day == 1000
    assert quota.verified is False
    assert quota.source == "unverified"
    assert quota.is_declared()


def test_disabled_without_a_token_gives_a_reason() -> None:
    provider = ImsProvider()
    availability = provider.availability(env={})
    assert availability.enabled is False
    assert TOKEN_ENV in (availability.reason or "")


def test_enabled_with_a_token() -> None:
    provider = ImsProvider()
    availability = provider.availability(env={TOKEN_ENV: TOKEN})
    assert availability.enabled is True
    assert availability.reason is None


# --- build_requests: auth gate ---------------------------------------------


def test_build_requests_returns_nothing_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
    assert provider.build_requests(location, _Settings()) == []


# --- build_requests: station discovery and caching --------------------------


def test_build_requests_asks_for_station_metadata_when_nothing_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)

    requests = provider.build_requests(location, _Settings())

    assert len(requests) == 1
    (spec,) = requests
    assert spec.purpose == "stations"
    assert spec.url == "https://api.ims.gov.il/v1/envista/stations"
    assert spec.headers["Authorization"] == f"ApiToken {TOKEN}"


def test_build_requests_uses_explicit_station_ids_without_any_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
    settings = _Settings({"station_ids": [5, 9]})

    requests = provider.build_requests(location, settings)

    assert [spec.purpose for spec in requests] == ["latest", "latest"]
    assert [spec.context["station_id"] for spec in requests] == [5, 9]
    assert requests[0].url == "https://api.ims.gov.il/v1/envista/stations/5/data/latest"


def test_build_requests_picks_nearest_active_stations_once_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)

    # Prime the cache the same way normalize() would, from a real stations fetch.
    assert provider.normalize(_fetch(_stations_body())) == []

    requests = provider.build_requests(location, _Settings())

    assert len(requests) == 1  # the fixture only has one station
    (spec,) = requests
    assert spec.purpose == "latest"
    assert spec.context["station_id"] == 1
    assert spec.url == "https://api.ims.gov.il/v1/envista/stations/1/data/latest"


def test_build_requests_honours_a_station_metadata_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
    stations = json.loads(_stations_body())
    settings = _Settings({"station_metadata": stations, "station_count": 1})

    requests = provider.build_requests(location, settings)

    assert len(requests) == 1
    assert requests[0].context["station_id"] == 1
    # The override is not folded into the instance cache.
    assert provider._station_cache == {}


def test_build_requests_ignores_inactive_stations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    provider = ImsProvider()
    location = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
    stations = json.loads(_stations_body())
    stations[0]["active"] = False
    settings = _Settings({"station_metadata": stations})

    assert provider.build_requests(location, settings) == []


# --- normalize: stations metadata response ----------------------------------


def test_normalize_stations_response_caches_but_yields_no_readings() -> None:
    provider = ImsProvider()
    assert provider.normalize(_fetch(_stations_body())) == []
    assert 1 in provider._station_cache
    assert provider._station_cache[1].channels[8].name == "Grad"


def test_normalize_returns_nothing_for_a_failed_or_not_modified_fetch() -> None:
    provider = ImsProvider()
    assert provider.normalize(_fetch(_latest_body(), status=500)) == []
    assert provider.normalize(_fetch(_latest_body(), status=304)) == []


# --- normalize: latest data --------------------------------------------------


def test_normalize_latest_converts_the_summer_wall_clock_from_utc_plus_2() -> None:
    """The fixture's ``datetime`` is labelled ``+03:00`` but IMS's own docs say
    the wall clock is always UTC+2 regardless of that label. 14:00 local must
    become 12:00 UTC — not 11:00, which is what trusting the label would give.
    """
    provider = ImsProvider()
    (reading,) = provider.normalize(_fetch(_latest_body()))
    assert reading.observed_at == datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def test_normalize_latest_produces_an_observation_with_radiation_channels() -> None:
    provider = ImsProvider()
    (reading,) = provider.normalize(_fetch(_latest_body()))

    assert reading.kind == "observation"
    assert reading.provider == "ims"
    assert reading.model is None
    assert reading.source == "envista/v1/stations/1/data/latest"
    assert reading.requested_at == REQUESTED_AT

    assert reading.values["shortwave_radiation"].value == 610.0
    assert reading.values["shortwave_radiation"].unit == "w_m2"
    assert reading.values["diffuse_radiation"].value == 90.0
    assert reading.values["diffuse_radiation"].unit == "w_m2"
    assert reading.values["direct_normal_radiation"].value == 720.0
    assert reading.values["direct_normal_radiation"].unit == "w_m2"
    assert reading.values["temperature"].value == 28.4
    assert reading.values["temperature"].unit == "degC"
    assert reading.values["temperature"].original_unit == "degC"


def test_normalize_latest_resolves_channels_via_cached_station_metadata_too() -> None:
    provider = ImsProvider()
    provider.normalize(_fetch(_stations_body()))  # prime the id->name cache
    (reading,) = provider.normalize(_fetch(_latest_body()))

    assert reading.values["shortwave_radiation"].value == 610.0
    assert reading.values["direct_normal_radiation"].value == 720.0


def test_normalize_latest_observed_at_never_comes_from_requested_at() -> None:
    provider = ImsProvider()
    (reading,) = provider.normalize(_fetch(_latest_body()))
    assert reading.observed_at != reading.requested_at
    assert reading.requested_at == REQUESTED_AT


# --- normalize: "no provider value is dropped" (docs/weather-api.md sec 4) --


def test_normalize_never_drops_a_channel_the_fixture_reports() -> None:
    """Every valid channel in ims_latest.json must land in the reading's
    values under *some* variable id — mapped or, failing that, ``x_<name>``.
    """
    provider = ImsProvider()
    latest_payload = json.loads(_latest_body())
    (reading,) = provider.normalize(_fetch(_latest_body()))

    reported_channels = [
        channel["name"]
        for entry in latest_payload["data"]
        for channel in entry["channels"]
        if channel.get("valid")
    ]
    assert reported_channels  # sanity: the fixture actually has channels

    resolved_ids = {
        (_CHANNEL_MAP[name][0] if name in _CHANNEL_MAP else f"x_{name.lower()}")
        for name in reported_channels
    }
    assert resolved_ids <= set(reading.values)
    assert resolved_ids == set(reading.values)


def test_normalize_falls_back_to_x_prefixed_id_for_an_unmapped_channel() -> None:
    """A channel outside docs/weather-api.md section 4's vocabulary is still
    emitted, never silently dropped, as ``x_<name>`` with the provider's own
    value/unit preserved verbatim.
    """
    provider = ImsProvider()
    payload = json.loads(_latest_body())
    payload["data"][0]["channels"].append(
        {"id": 99, "name": "SomeNewChannel", "alias": "Something New", "value": 1234, "valid": True}
    )
    record = _fetch(json.dumps(payload).encode("utf-8"))

    (reading,) = provider.normalize(record)

    assert "x_somenewchannel" in reading.values
    measurement = reading.values["x_somenewchannel"]
    assert measurement.value == 1234.0
    assert measurement.original_value == 1234
    assert measurement.unit == "other"
    assert measurement.original_unit is None
