"""Tests for the keyless IMS city-forecast XML adapter (spec ``c8``).

Uses the verbatim ``tests/fixtures/ims_isr_cities.xml`` fixture, captured as
raw ISO-8859-8 bytes. This module never re-encodes it — it is passed
straight to :mod:`xml.etree.ElementTree`, which reads the declared encoding
from the XML prolog itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from climate.weather.providers import base
from climate.weather.providers.base import Availability, ProviderSettings
from climate.weather.providers.ims_forecast import ImsForecastProvider
from climate.weather.store import FetchError, FetchRecord
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "ims_isr_cities.xml"
FIXTURE_BODY = FIXTURE_PATH.read_bytes()

NOW = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Location:
    label: str
    latitude: float
    longitude: float


LOCATION = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)


def test_fixture_bytes_are_iso_8859_8_and_untouched() -> None:
    # The fixture must decode cleanly under its declared charset; this test
    # protects the "stored byte-exact, never re-encoded" requirement by
    # proving the raw bytes themselves are genuine ISO-8859-8.
    assert FIXTURE_BODY.decode("iso-8859-8")
    assert b'encoding="ISO-8859-8"' in FIXTURE_BODY[:100]


def _fetch_record(url: str, body: bytes = FIXTURE_BODY, status: int | None = 200) -> FetchRecord:
    return FetchRecord(
        provider="ims-forecast",
        endpoint=url,
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=status,
        body=body,
    )


# --- contract surface --------------------------------------------------------


def test_ims_forecast_declares_a_sound_contract() -> None:
    provider = ImsForecastProvider()
    assert provider.id == "ims-forecast"
    assert base.Capability.FORECAST in provider.capabilities
    assert provider.default_interval_seconds == 21600
    assert base.validate_provider(provider) == []
    assert provider.auth.required is False
    assert provider.attribution.url == "https://ims.gov.il/en/termOfuse"


# --- availability -------------------------------------------------------------


def test_ims_forecast_is_disabled_when_no_settings_are_configured() -> None:
    provider = ImsForecastProvider()
    result = provider.availability(None, env={})
    assert result.enabled is False
    assert "enabled" in (result.reason or "")


def test_ims_forecast_is_disabled_when_settings_say_so() -> None:
    provider = ImsForecastProvider()
    result = provider.availability(ProviderSettings(enabled=False), env={})
    assert result.enabled is False


def test_ims_forecast_is_enabled_only_with_explicit_enabled_true() -> None:
    provider = ImsForecastProvider()
    result = provider.availability(ProviderSettings(enabled=True), env={})
    assert result == Availability(True, None)


# --- build_requests -----------------------------------------------------------


def test_no_cities_configured_emits_no_requests() -> None:
    provider = ImsForecastProvider()
    assert provider.build_requests(LOCATION, ProviderSettings(enabled=True)) == ()
    assert provider.build_requests(LOCATION, ProviderSettings(enabled=True, params={})) == ()


def test_configured_cities_build_one_request_carrying_the_candidates() -> None:
    provider = ImsForecastProvider()
    settings = ProviderSettings(enabled=True, params={"cities": ["Herzliya", "Tel Aviv - Yafo"]})
    requests = provider.build_requests(LOCATION, settings)
    assert len(requests) == 1
    spec = requests[0]
    assert spec.url.startswith(
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml?"
    )
    assert "cities=" in spec.url


# --- normalize: city matching --------------------------------------------------


def test_normalize_picks_the_first_candidate_present_in_the_feed() -> None:
    # "Herzliya" is not one of the ~15 feed cities; "Tel Aviv - Yafo" is,
    # exercising the fallback-candidate use case from the brief.
    provider = ImsForecastProvider()
    url = (
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
        "?cities=Herzliya,Tel+Aviv+-+Yafo"
    )
    readings = provider.normalize(_fetch_record(url))
    assert readings
    assert all(r.source == "ims-forecast/Tel Aviv - Yafo" for r in readings)
    assert all(r.kind == "forecast" for r in readings)
    assert all(r.provider == "ims-forecast" for r in readings)
    assert all(r.location == NEUTRAL_LABEL for r in readings)


def test_city_matching_is_case_insensitive_and_tolerates_dash_spacing() -> None:
    provider = ImsForecastProvider()
    url = (
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
        "?cities=tel+aviv-yafo"
    )
    readings = provider.normalize(_fetch_record(url))
    assert readings
    assert all(r.source == "ims-forecast/Tel Aviv - Yafo" for r in readings)


def test_no_candidate_present_returns_no_readings_and_logs_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = ImsForecastProvider()
    url = (
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
        "?cities=Nowhereville"
    )
    with caplog.at_level(logging.WARNING):
        readings = provider.normalize(_fetch_record(url))
    assert readings == ()
    assert any("Nowhereville" in message for message in caplog.messages)


def test_no_configured_candidates_on_this_record_returns_no_readings() -> None:
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
    assert provider.normalize(_fetch_record(url)) == ()


# --- normalize: values ---------------------------------------------------------


def test_normalize_emits_one_forecast_reading_per_date_with_daily_values() -> None:
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml?cities=Elat"
    readings = provider.normalize(_fetch_record(url))
    by_date = {r.observed_at.date().isoformat(): r for r in readings}
    assert "2026-09-18" in by_date
    reading = by_date["2026-09-18"]
    assert reading.values["temperature_max"].value == 37
    assert reading.values["temperature_max"].unit == "degC"
    assert reading.values["temperature_min"].value == 28
    assert reading.values["relative_humidity_max"].value == 25
    assert reading.values["relative_humidity_max"].unit == "percent"
    assert reading.values["relative_humidity_min"].value == 25
    assert reading.values["weather_code"].value == "1250"
    assert reading.values["weather_code"].unit == "code"
    # observed_at is the forecast target date, never the fetch's requested_at.
    assert reading.observed_at != reading.requested_at
    assert reading.requested_at == NOW


def test_normalize_drops_no_provider_value_from_the_fixture() -> None:
    """docs/weather-api.md sec. 4: 'No provider value is dropped.'

    Elat's 2026-09-18 ``TimeUnitData`` carries all six element kinds the
    fixture ever uses, including the vocabulary-less "Wind direction and
    speed" range string — every one of them must land under some reading
    id.
    """
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml?cities=Elat"
    readings = provider.normalize(_fetch_record(url))
    reading = next(r for r in readings if r.observed_at.date().isoformat() == "2026-09-18")

    expected_keys = {
        "temperature_max",
        "temperature_min",
        "relative_humidity_max",
        "relative_humidity_min",
        "weather_code",
        "x_wind_direction_and_speed",
    }
    assert expected_keys <= reading.values.keys()
    wind = reading.values["x_wind_direction_and_speed"]
    assert wind.unit == "code"
    assert wind.value == "270-45/15-25"


def test_normalize_returns_nothing_for_a_failed_fetch() -> None:
    provider = ImsForecastProvider()
    failed = FetchRecord(
        provider="ims-forecast",
        endpoint=(
            "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml?cities=Elat"
        ),
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=None,
        body=b"",
        error=FetchError(kind="timeout"),
    )
    assert provider.normalize(failed) == ()


def test_normalize_is_pure_and_re_derivable() -> None:
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml?cities=Elat"
    record = _fetch_record(url)
    assert provider.normalize(record) == provider.normalize(record)
