"""Tests for the OpenWeather current-weather adapter (task t12).

Exercises the OpenWeather 2.5 ``/data/2.5/weather`` adapter against the
synthesized fixture ``tests/fixtures/openweather_current.json`` (no real
OpenWeather credential was available to capture a live response — see that
fixture's own note and ``tests/fixtures/README.md``). The adapter never
touches the network here: requests are only *described*
(:class:`RequestSpec`) and normalization runs over a fetch record built by
hand from the fixture bytes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest

from climate.weather.providers.base import (
    Availability,
    Capability,
    FreshnessStrategy,
    ProviderSettings,
    RequestSpec,
    validate_provider,
)
from climate.weather.providers.openweather import ENV_VAR, OpenWeatherProvider
from climate.weather.store import FetchRecord, SecretLeakError, redact_url
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURE_PATH = "tests/fixtures/openweather_current.json"
FAKE_KEY = "fake-openweather-key-do-not-use"


class _Location:
    """Structural stand-in for ``climate.weather.config.Location``."""

    def __init__(self, label: str, latitude: float, longitude: float) -> None:
        self.label = label
        self.latitude = latitude
        self.longitude = longitude


LOCATION = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _fixture_bytes() -> bytes:
    with open(FIXTURE_PATH, "rb") as fh:
        return fh.read()


@pytest.fixture()
def provider() -> OpenWeatherProvider:
    return OpenWeatherProvider()


@pytest.fixture()
def with_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(ENV_VAR, FAKE_KEY)
    return FAKE_KEY


# --- metadata ---------------------------------------------------------------


def test_env_var_matches_the_brief() -> None:
    assert ENV_VAR == "CLIMATE_OPENWEATHER_API_KEY"


def test_provider_declares_a_complete_contract(provider: OpenWeatherProvider) -> None:
    assert provider.id == "openweather"
    assert Capability.CURRENT_MODEL in provider.capabilities
    assert provider.auth.required is True
    assert provider.auth.env_var == ENV_VAR
    assert provider.default_interval_seconds == 600
    assert provider.freshness is FreshnessStrategy.INTERVAL
    assert provider.quota is not None
    assert provider.quota.calls_per_minute == 60
    assert provider.quota.calls_per_month == 1_000_000
    assert provider.quota.verified is False
    assert provider.quota.source == "issue-5"
    assert provider.attribution is not None
    assert validate_provider(provider) == []


# --- availability -------------------------------------------------------------


def test_disabled_with_a_reason_when_the_key_is_missing(
    provider: OpenWeatherProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    result = provider.availability(env={})
    assert result == Availability(False, result.reason)
    assert result.enabled is False
    assert ENV_VAR in (result.reason or "")


def test_enabled_once_the_key_is_set(provider: OpenWeatherProvider, with_api_key: str) -> None:
    assert provider.availability(env=os.environ).enabled is True


# --- build_requests -----------------------------------------------------------


def test_build_requests_issues_exactly_one_call_to_the_current_weather_endpoint(
    provider: OpenWeatherProvider, with_api_key: str
) -> None:
    requests = provider.build_requests(LOCATION, ProviderSettings())
    assert len(requests) == 1
    spec = requests[0]
    assert isinstance(spec, RequestSpec)
    assert spec.provider_id == "openweather"
    assert spec.location_label == NEUTRAL_LABEL
    parts = urlsplit(spec.url)
    assert parts.scheme == "https"
    assert parts.netloc == "api.openweathermap.org"
    assert parts.path == "/data/2.5/weather"
    query = dict(parse_qsl(parts.query))
    assert query["units"] == "metric"
    assert query["appid"] == with_api_key
    assert float(query["lat"]) == NEUTRAL_POINT[0]
    assert float(query["lon"]) == NEUTRAL_POINT[1]


def test_build_requests_never_touches_a_forecast_or_one_call_path(
    provider: OpenWeatherProvider, with_api_key: str
) -> None:
    requests = provider.build_requests(LOCATION, ProviderSettings())
    for spec in requests:
        assert "forecast" not in spec.url
        assert "onecall" not in spec.url


def test_build_requests_returns_nothing_without_a_key(
    provider: OpenWeatherProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert provider.build_requests(LOCATION, ProviderSettings()) == ()


# --- secret redaction ---------------------------------------------------------


def test_the_key_is_absent_once_the_url_is_redacted(
    provider: OpenWeatherProvider, with_api_key: str
) -> None:
    spec = provider.build_requests(LOCATION, ProviderSettings())[0]
    assert with_api_key in spec.url  # the real request must carry the live key
    redacted = redact_url(spec.url)
    assert with_api_key not in redacted
    assert "REDACTED" in redacted


def test_a_fetch_record_refuses_an_unredacted_endpoint(
    provider: OpenWeatherProvider, with_api_key: str
) -> None:
    spec = provider.build_requests(LOCATION, ProviderSettings())[0]
    with pytest.raises(SecretLeakError):
        FetchRecord(
            provider=provider.id,
            endpoint=spec.url,  # the raw, unredacted URL - must be rejected
            location=NEUTRAL_LABEL,
            requested_at=NOW,
            status=200,
            body=_fixture_bytes(),
        )
    # The redacted endpoint is what actually gets stored.
    record = FetchRecord(
        provider=provider.id,
        endpoint=redact_url(spec.url),
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=_fixture_bytes(),
    )
    assert with_api_key not in record.endpoint


def test_normalize_emits_no_log_line_at_all(
    provider: OpenWeatherProvider, with_api_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The module performs no logging, so there is nothing for a key to leak into."""
    record = FetchRecord(
        provider=provider.id,
        endpoint=redact_url(provider.build_requests(LOCATION, ProviderSettings())[0].url),
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=_fixture_bytes(),
    )
    with caplog.at_level("DEBUG"):
        provider.normalize(record)
    assert with_api_key not in caplog.text
    assert caplog.text == ""


# --- normalize -----------------------------------------------------------------


def _fixture_record(**overrides: object) -> FetchRecord:
    fields = {
        "provider": "openweather",
        "endpoint": "https://api.openweathermap.org/data/2.5/weather?appid=REDACTED",
        "location": NEUTRAL_LABEL,
        "requested_at": NOW,
        "status": 200,
        "body": _fixture_bytes(),
    }
    fields.update(overrides)
    return FetchRecord(**fields)


def test_normalize_derives_a_model_reading_from_the_fixture(
    provider: OpenWeatherProvider,
) -> None:
    readings = provider.normalize(_fixture_record())
    assert len(readings) == 1
    reading = readings[0]
    assert reading.provider == "openweather"
    assert reading.source == "openweather"
    assert reading.model is None
    assert reading.kind == "model"
    assert reading.location == NEUTRAL_LABEL
    assert reading.observed_at == datetime.fromtimestamp(1758131446, tz=UTC)
    assert reading.requested_at == NOW
    assert reading.values["temperature"].value == pytest.approx(285.32)
    assert reading.values["temperature"].unit == "C"
    assert reading.values["humidity"].value == pytest.approx(72)
    assert reading.values["pressure"].value == pytest.approx(1015)
    assert reading.values["wind_speed"].value == pytest.approx(3.6)


def test_normalize_never_uses_requested_at_as_observed_at(
    provider: OpenWeatherProvider,
) -> None:
    readings = provider.normalize(_fixture_record())
    assert readings[0].observed_at != readings[0].requested_at


def test_normalize_is_pure_and_re_derivable(provider: OpenWeatherProvider) -> None:
    record = _fixture_record()
    first = provider.normalize(record)
    second = provider.normalize(record)
    assert [r.values["temperature"].value for r in first] == [
        r.values["temperature"].value for r in second
    ]


def test_normalize_returns_nothing_for_a_failed_fetch(provider: OpenWeatherProvider) -> None:
    record = FetchRecord(
        provider="openweather",
        endpoint="https://api.openweathermap.org/data/2.5/weather?appid=REDACTED",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=401,
        body=b'{"cod":401,"message":"Invalid API key"}',
    )
    assert provider.normalize(record) == ()


def test_normalize_returns_nothing_for_a_304(provider: OpenWeatherProvider) -> None:
    record = FetchRecord(
        provider="openweather",
        endpoint="https://api.openweathermap.org/data/2.5/weather?appid=REDACTED",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=304,
        body=b"",
    )
    assert provider.normalize(record) == ()
