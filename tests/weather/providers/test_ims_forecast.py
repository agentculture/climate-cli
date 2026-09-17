"""Tests for the keyless IMS city-forecast XML adapter (spec ``c8``).

Uses the verbatim ``tests/fixtures/ims_isr_cities.xml`` fixture, captured as
raw ISO-8859-8 bytes. This module never re-encodes it — it is passed
straight to :mod:`xml.etree.ElementTree`, which reads the declared encoding
from the XML prolog itself.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from climate.weather.providers import base
from climate.weather.providers.base import Availability, ProviderSettings
from climate.weather.providers.ims_forecast import ImsForecastProvider, _normalize_city_name
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


def _fetch_record(
    url: str,
    body: bytes = FIXTURE_BODY,
    status: int | None = 200,
    location: str = NEUTRAL_LABEL,
) -> FetchRecord:
    return FetchRecord(
        provider="ims-forecast",
        endpoint=url,
        location=location,
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
    assert list(provider.build_requests(LOCATION, ProviderSettings(enabled=True))) == []
    assert list(provider.build_requests(LOCATION, ProviderSettings(enabled=True, params={}))) == []


def test_configured_cities_build_one_request_carrying_the_candidates_in_the_fragment() -> None:
    provider = ImsForecastProvider()
    settings = ProviderSettings(enabled=True, params={"cities": ["Herzliya", "Tel Aviv - Yafo"]})
    requests = provider.build_requests(LOCATION, settings)
    assert len(requests) == 1
    spec = requests[0]
    assert spec.url.startswith(
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#"
    )
    assert "cities=" in spec.url.split("#", 1)[1]


def test_the_candidates_are_never_sent_on_the_wire() -> None:
    # Location/city names are private data. A URL fragment is never
    # transmitted by an HTTP client; prove it for the real url this
    # adapter builds, at the level urllib actually uses to issue the GET.
    provider = ImsForecastProvider()
    settings = ProviderSettings(enabled=True, params={"cities": ["Herzliya", "Tel Aviv - Yafo"]})
    spec = provider.build_requests(LOCATION, settings)[0]
    request = urllib.request.Request(spec.url, method="GET")
    assert "cities=" not in request.selector
    assert "Herzliya" not in request.selector
    assert "Tel Aviv" not in request.selector
    assert "Yafo" not in request.selector


def test_a_cities_mapping_gives_each_location_only_its_own_candidates() -> None:
    """Regression (qodo-8, ims-forecast half): candidates are per location.

    With a mapping, each configured location's request carries only the
    candidates listed under its own label, and normalizing the *same* feed
    bytes therefore stores each location's own city — never the other's.
    """
    provider = ImsForecastProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(
        enabled=True,
        params={"cities": {NEUTRAL_LABEL: ["Elat"], other.label: ["Tel Aviv - Yafo"]}},
    )

    (home_spec,) = provider.build_requests(LOCATION, settings)
    (away_spec,) = provider.build_requests(other, settings)

    assert home_spec.context["cities"] == ("Elat",)
    assert away_spec.context["cities"] == ("Tel Aviv - Yafo",)

    home_readings = provider.normalize(_fetch_record(home_spec.url))
    away_readings = provider.normalize(_fetch_record(away_spec.url, location=other.label))
    assert home_readings and away_readings
    assert {r.source for r in home_readings} == {"ims-forecast/Elat"}
    assert {r.source for r in away_readings} == {"ims-forecast/Tel Aviv - Yafo"}


def test_a_cities_mapping_without_this_location_emits_no_request() -> None:
    provider = ImsForecastProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(enabled=True, params={"cities": {other.label: ["Elat"]}})
    assert list(provider.build_requests(LOCATION, settings)) == []


def test_a_plain_cities_list_still_serves_every_location() -> None:
    """The documented single-location convenience keeps working."""
    provider = ImsForecastProvider()
    other = _Location("away", *NEUTRAL_POINT)
    settings = ProviderSettings(enabled=True, params={"cities": ["Elat"]})
    for location in (LOCATION, other):
        (spec,) = provider.build_requests(location, settings)
        assert spec.location_label == location.label
        assert spec.context["cities"] == ("Elat",)


def test_build_requests_accepts_the_env_keyword_although_it_needs_no_credential() -> None:
    provider = ImsForecastProvider()
    settings = ProviderSettings(enabled=True, params={"cities": ["Elat"]})
    assert provider.build_requests(LOCATION, settings, env={}) == provider.build_requests(
        LOCATION, settings
    )


# --- normalize: city matching --------------------------------------------------


def test_fetch_record_accepts_the_fragment_bearing_endpoint_and_normalize_recovers_it() -> None:
    # Round-trip through the real build_requests()-produced URL (not a
    # hand-written literal) to prove store.FetchRecord accepts a fragment
    # in its endpoint field and that normalize() recovers the ordered
    # candidates from it byte-for-byte, including names with spaces and
    # " - ".
    provider = ImsForecastProvider()
    settings = ProviderSettings(
        enabled=True, params={"cities": ["My Home Town", "Tel Aviv - Yafo"]}
    )
    spec = provider.build_requests(LOCATION, settings)[0]

    record = FetchRecord(
        provider="ims-forecast",
        endpoint=spec.url,
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=FIXTURE_BODY,
    )
    assert record.endpoint == spec.url  # accepted verbatim, fragment and all

    readings = provider.normalize(record)
    # "My Home Town" is not a real feed city; "Tel Aviv - Yafo" is, and
    # must still be found even though the first candidate has a space and
    # the matched feed name has " - ".
    assert readings
    assert all(r.source == "ims-forecast/Tel Aviv - Yafo" for r in readings)


def test_normalize_picks_the_first_candidate_present_in_the_feed() -> None:
    # "Herzliya" is not one of the ~15 feed cities; "Tel Aviv - Yafo" is,
    # exercising the fallback-candidate use case from the brief.
    provider = ImsForecastProvider()
    url = (
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
        "#cities=Herzliya,Tel+Aviv+-+Yafo"
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
        "#cities=tel+aviv-yafo"
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
        "#cities=Nowhereville"
    )
    with caplog.at_level(logging.WARNING):
        readings = provider.normalize(_fetch_record(url))
    assert readings == ()
    assert any("Nowhereville" in message for message in caplog.messages)


def test_no_fragment_on_the_record_returns_no_readings_and_logs_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A record whose endpoint carries no "cities" fragment at all — nothing
    # was configured when it was fetched, or it is a foreign/older record.
    # normalize() must not raise; it returns nothing and says why.
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
    with caplog.at_level(logging.WARNING):
        readings = provider.normalize(_fetch_record(url))
    assert readings == ()
    assert any("no city candidates" in message for message in caplog.messages)


# --- normalize: values ---------------------------------------------------------


def test_normalize_emits_one_forecast_reading_per_date_with_daily_values() -> None:
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
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
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
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


# --- model_run_at (qodo-14) ----------------------------------------------------


def test_model_run_at_is_the_feeds_own_issue_time_not_the_fetch_time() -> None:
    """The feed states ``Identification/IssueDateTime``; that is the run time."""
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
    readings = provider.normalize(_fetch_record(url))
    assert readings
    issued = datetime(2026, 9, 17, 17, 40, tzinfo=UTC)
    assert {r.model_run_at for r in readings} == {issued}
    assert all(r.model_run_at != r.requested_at for r in readings)


def test_model_run_at_is_none_when_the_feed_states_no_issue_time() -> None:
    provider = ImsForecastProvider()
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
    stripped = FIXTURE_BODY.replace(b"<IssueDateTime>2026-09-17 17:40</IssueDateTime>", b"")
    assert stripped != FIXTURE_BODY
    readings = provider.normalize(_fetch_record(url, body=stripped))
    assert readings
    assert all(r.model_run_at is None for r in readings)


# --- city-name normalization ----------------------------------------------------


def test_city_name_normalization_stays_linear_on_an_adversarial_name() -> None:
    """Sonar S8786: the old ``\\s*-\\s*`` pattern backtracked super-linearly.

    A long *interior* run of whitespace with no dash after it is the worst
    case (leading/trailing runs are cut by ``strip()`` first). The rewritten
    matcher scans each character once, so a name with 150 000 interior
    spaces resolves immediately; the old pattern took over ten seconds on
    the same input, and grew quadratically from there.
    """
    provider = ImsForecastProvider()
    adversarial = "a" + " " * 150_000 + "b"
    url = (
        "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"
        "#cities=a" + "+" * 150_000 + "b"
    )
    started = time.monotonic()
    assert provider.normalize(_fetch_record(url)) == ()
    assert _normalize_city_name(adversarial) == "a b"
    assert time.monotonic() - started < 2.0


def test_city_name_normalization_keeps_its_matching_behaviour() -> None:
    assert _normalize_city_name("Tel Aviv - Yafo") == _normalize_city_name("tel aviv-yafo")
    assert _normalize_city_name("  Some   Town  ") == "some town"


def test_normalize_returns_nothing_for_a_failed_fetch() -> None:
    provider = ImsForecastProvider()
    failed = FetchRecord(
        provider="ims-forecast",
        endpoint=(
            "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
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
    url = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml#cities=Elat"
    record = _fetch_record(url)
    first = provider.normalize(record)
    second = provider.normalize(record)
    assert first, "the fixture must normalize to at least one reading"
    assert second == first
