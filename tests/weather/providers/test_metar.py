"""Tests for the keyless aviationweather.gov METAR adapter (spec ``c8``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from climate.weather.providers import base
from climate.weather.providers.base import Availability, ProviderSettings
from climate.weather.providers.metar import MetarProvider
from climate.weather.store import FetchError, FetchRecord
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "metar_llbg.json"
FIXTURE_BODY = FIXTURE_PATH.read_bytes()

NOW = datetime(2026, 9, 17, 18, 5, tzinfo=UTC)


@dataclass(frozen=True)
class _Location:
    label: str
    latitude: float
    longitude: float


LOCATION = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)


def _fetch_record(body: bytes = FIXTURE_BODY, status: int | None = 200) -> FetchRecord:
    return FetchRecord(
        provider="metar",
        endpoint="https://aviationweather.gov/api/data/metar?ids=LLBG&format=json",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=status,
        body=body,
    )


# --- contract surface -------------------------------------------------------


def test_metar_declares_a_sound_contract() -> None:
    provider = MetarProvider()
    assert provider.id == "metar"
    assert base.Capability.CURRENT_OBSERVATION in provider.capabilities
    assert provider.default_interval_seconds == 1800
    assert provider.freshness is base.FreshnessStrategy.STATION_CADENCE
    assert base.validate_provider(provider) == []
    assert provider.auth.required is False


# --- availability ------------------------------------------------------------


def test_metar_is_disabled_when_no_settings_are_configured() -> None:
    provider = MetarProvider()
    result = provider.availability(None, env={})
    assert result == Availability(False, result.reason)
    assert "enabled" in (result.reason or "")


def test_metar_is_disabled_when_settings_omit_enabled() -> None:
    provider = MetarProvider()
    # ProviderSettings() defaults enabled=True; simulate an omitted/false flag.
    result = provider.availability(ProviderSettings(enabled=False), env={})
    assert result.enabled is False


def test_metar_is_enabled_only_with_explicit_enabled_true() -> None:
    provider = MetarProvider()
    result = provider.availability(ProviderSettings(enabled=True), env={})
    assert result == Availability(True, None)


# --- build_requests ----------------------------------------------------------


def test_no_stations_configured_emits_no_requests() -> None:
    provider = MetarProvider()
    assert list(provider.build_requests(LOCATION, ProviderSettings(enabled=True))) == []
    assert list(provider.build_requests(LOCATION, ProviderSettings(enabled=True, params={}))) == []


def test_configured_stations_build_one_comma_joined_request() -> None:
    provider = MetarProvider()
    settings = ProviderSettings(enabled=True, params={"stations": ["LLBG", "KJFK"]})
    requests = provider.build_requests(LOCATION, settings)
    assert len(requests) == 1
    spec = requests[0]
    assert spec.provider_id == "metar"
    assert spec.location_label == NEUTRAL_LABEL
    assert spec.url == "https://aviationweather.gov/api/data/metar?ids=LLBG,KJFK&format=json"
    assert spec.url.startswith("https://")


def test_a_stations_mapping_gives_each_location_only_its_own_stations() -> None:
    """Regression (qodo-8): stations are per location, not global.

    Before this fix one global list was fetched once per configured
    location, and every station's report was then stored under each
    location's label. With a mapping each location asks for — and so stores
    — only the stations listed under its own label.
    """
    provider = MetarProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(
        enabled=True,
        params={"stations": {NEUTRAL_LABEL: ["LLBG"], other.label: ["KJFK"]}},
    )

    (home_spec,) = provider.build_requests(LOCATION, settings)
    (away_spec,) = provider.build_requests(other, settings)

    assert home_spec.context["stations"] == ("LLBG",)
    assert away_spec.context["stations"] == ("KJFK",)
    assert "ids=LLBG&" in home_spec.url
    assert "KJFK" not in home_spec.url
    assert "ids=KJFK&" in away_spec.url
    assert "LLBG" not in away_spec.url

    # ... and each location only ever stores the report of its own station.
    home_readings = provider.normalize(_fetch_record())
    assert {r.source for r in home_readings} == {"metar/LLBG"}
    assert {r.location for r in home_readings} == {NEUTRAL_LABEL}


def test_a_stations_mapping_without_this_location_emits_no_request() -> None:
    provider = MetarProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(enabled=True, params={"stations": {other.label: ["KJFK"]}})
    assert list(provider.build_requests(LOCATION, settings)) == []


def test_a_plain_stations_list_still_serves_every_location() -> None:
    """The documented single-location convenience keeps working."""
    provider = MetarProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(enabled=True, params={"stations": ["LLBG"]})
    for location in (LOCATION, other):
        (spec,) = provider.build_requests(location, settings)
        assert spec.location_label == location.label
        assert spec.context["stations"] == ("LLBG",)


def test_metar_costs_one_request_per_location_per_tick() -> None:
    """Its declared per-tick cost matches what build_requests really emits."""
    provider = MetarProvider()
    settings = ProviderSettings(enabled=True, params={"stations": ["LLBG", "KJFK"]})
    assert provider.requests_per_tick(LOCATION, settings) == len(
        provider.build_requests(LOCATION, settings)
    )


def test_build_requests_accepts_the_env_keyword_although_it_needs_no_credential() -> None:
    provider = MetarProvider()
    settings = ProviderSettings(enabled=True, params={"stations": ["LLBG"]})
    assert provider.build_requests(LOCATION, settings, env={}) == provider.build_requests(
        LOCATION, settings
    )


# --- normalize -----------------------------------------------------------


def test_normalize_the_real_fixture_produces_one_observation_reading() -> None:
    provider = MetarProvider()
    readings = provider.normalize(_fetch_record())
    assert len(readings) == 1
    reading = readings[0]
    assert reading.kind == "observation"
    assert reading.provider == "metar"
    assert reading.source == "metar/LLBG"
    assert reading.location == NEUTRAL_LABEL
    # observed_at comes from the report's own reportTime, never requested_at.
    assert reading.observed_at == datetime(2026, 9, 17, 18, 0, 0, tzinfo=UTC)
    assert reading.observed_at != reading.requested_at
    assert reading.requested_at == NOW


def test_normalize_converts_knots_to_m_s_and_keeps_the_original() -> None:
    provider = MetarProvider()
    reading = provider.normalize(_fetch_record())[0]
    wind = reading.values["wind_speed"]
    assert wind.unit == "m_s"
    assert wind.value == pytest.approx(round(6 * 0.514444, 2))
    assert wind.original_value == 6
    assert wind.original_unit == "kt"


def test_normalize_keeps_other_metric_fields_as_reported() -> None:
    provider = MetarProvider()
    reading = provider.normalize(_fetch_record())[0]
    assert reading.values["temperature"].value == 30
    assert reading.values["temperature"].unit == "degC"
    assert reading.values["dew_point"].value == 21
    assert reading.values["dew_point"].unit == "degC"
    assert reading.values["pressure_msl"].value == 1007
    assert reading.values["pressure_msl"].unit == "hPa"


def test_normalize_converts_visibility_from_statute_miles_to_metres() -> None:
    provider = MetarProvider()
    reading = provider.normalize(_fetch_record())[0]
    visibility = reading.values["visibility"]
    assert visibility.unit == "m"
    assert visibility.value == pytest.approx(round(6 * 1609.344, 1))
    assert visibility.original_value == "6+"
    assert visibility.original_unit == "sm"


def test_normalize_drops_no_provider_value_from_the_fixture() -> None:
    """docs/weather-api.md sec. 4: 'No provider value is dropped.'

    Every scalar field the fixture's one report carries ends up under some
    reading id, either the vocabulary's canonical id or an ``x_`` fallback —
    except station coordinates, which the API's own hard rule (never a
    coordinate in a response) overrides, and the empty ``clouds`` list,
    which is not a scalar.
    """
    provider = MetarProvider()
    reading = provider.normalize(_fetch_record())[0]
    (report,) = json.loads(FIXTURE_BODY)

    accounted_for = {
        "temp": "temperature",
        "dewp": "dew_point",
        "wdir": "wind_direction",
        "wspd": "wind_speed",
        "visib": "visibility",
        "altim": "pressure_msl",
    }
    never_emitted = {"icaoId", "receiptTime", "obsTime", "reportTime", "name", "lat", "lon"}

    for field_name, raw_value in report.items():
        if field_name in never_emitted:
            continue
        if field_name == "clouds":
            assert raw_value == [], "this assumption only holds for the fixture's empty list"
            continue
        if field_name in accounted_for:
            reading_key = accounted_for[field_name]
        else:
            reading_key = "x_" + "".join(
                f"_{c.lower()}" if c.isupper() else c for c in field_name
            ).lstrip("_")
        assert reading_key in reading.values, f"{field_name!r} was dropped (no {reading_key!r})"


def test_normalize_returns_nothing_for_a_failed_fetch() -> None:
    provider = MetarProvider()
    failed = FetchRecord(
        provider="metar",
        endpoint="https://aviationweather.gov/api/data/metar?ids=LLBG&format=json",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=None,
        body=b"",
        error=FetchError(kind="timeout"),
    )
    assert provider.normalize(failed) == ()


def test_normalize_returns_nothing_for_an_empty_station_list() -> None:
    provider = MetarProvider()
    empty = FetchRecord(
        provider="metar",
        endpoint="https://aviationweather.gov/api/data/metar?ids=ZZZZ&format=json",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=b"[]",
    )
    assert provider.normalize(empty) == ()


def test_normalize_converts_a_wind_gust_when_the_feed_reports_one() -> None:
    # The LLBG fixture carries no gust; build a minimal synthetic report to
    # exercise wind_gust without inventing a second real fixture.
    provider = MetarProvider()
    body = json.dumps(
        [
            {
                "icaoId": "KJFK",
                "reportTime": "2026-09-17T18:00:00.000Z",
                "temp": 20,
                "wgst": 25,
            }
        ]
    ).encode("utf-8")
    record = _fetch_record(body=body)
    reading = provider.normalize(record)[0]
    gust = reading.values["wind_gust"]
    assert gust.unit == "m_s"
    assert gust.value == pytest.approx(round(25 * 0.514444, 2))
    assert gust.original_value == 25
    assert gust.original_unit == "kt"


def test_model_run_at_is_none_for_a_station_observation() -> None:
    """qodo-14: METAR reports are measurements, not a model run.

    The feed states no issue/run time, and the fetch time is never a
    substitute for one.
    """
    provider = MetarProvider()
    readings = provider.normalize(_fetch_record())
    assert readings
    assert all(r.model_run_at is None for r in readings)


def test_normalize_is_pure_and_re_derivable() -> None:
    provider = MetarProvider()
    record = _fetch_record()
    first = provider.normalize(record)
    second = provider.normalize(record)
    assert first, "the fixture must normalize to at least one reading"
    assert second == first
