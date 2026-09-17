"""Tests for the met-no (MET Norway Locationforecast) adapter.

Uses the verbatim fixture body and its recorded response headers — the
adapter never touches the network here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import climate
from climate.weather.providers.base import (
    Capability,
    FreshnessStrategy,
    ProviderSettings,
    RequestSpec,
    validate_provider,
)
from climate.weather.providers.met_no import _INSTANT_VARIABLES, MetNoProvider
from climate.weather.store import FetchError, FetchRecord
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_BODY = (_FIXTURES / "met_no_locationforecast.json").read_bytes()
_HEADERS_TEXT = (_FIXTURES / "met_no_locationforecast_headers.txt").read_text(encoding="utf-8")


def _parse_headers(text: str) -> dict[str, str]:
    """Turn the recorded ``curl -D`` output into a header mapping."""
    headers: dict[str, str] = {}
    for line in text.splitlines()[1:]:  # skip the HTTP/2 200 status line
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    return headers


_CACHE_HEADERS = _parse_headers(_HEADERS_TEXT)


@dataclass(frozen=True)
class _Location:
    """Stand-in for ``climate.weather.config.Location`` (structural match)."""

    label: str
    latitude: float
    longitude: float


LOCATION = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)

# The fixture's own recorded 'date' header, as the moment it was requested.
NOW = datetime(2026, 9, 17, 18, 27, 16, tzinfo=UTC)


def _fetch_record(**overrides: object) -> FetchRecord:
    defaults: dict[str, object] = dict(
        provider="met-no",
        endpoint=f"{'https://api.met.no/weatherapi/locationforecast/2.0/complete'}",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=_BODY,
        cache_headers=_CACHE_HEADERS,
    )
    defaults.update(overrides)
    return FetchRecord(**defaults)  # type: ignore[arg-type]


# --- provider metadata ------------------------------------------------------


def test_provider_declares_a_complete_contract() -> None:
    provider = MetNoProvider()
    assert provider.id == "met-no"
    assert provider.freshness is FreshnessStrategy.HTTP_EXPIRES
    assert provider.default_interval_seconds == 1800
    assert provider.quota is not None
    assert provider.quota.unlimited is True
    assert provider.attribution is not None
    assert provider.attribution.text == "MET Norway"
    assert provider.attribution.licence == "CC BY 4.0 / NLOD"
    assert provider.attribution.url == "https://api.met.no/doc/License"
    assert Capability.CONDITIONAL_GET in provider.capabilities
    assert validate_provider(provider) == []


# --- build_requests ----------------------------------------------------------


def test_build_requests_sends_an_identifying_user_agent() -> None:
    provider = MetNoProvider()
    requests = provider.build_requests(LOCATION, ProviderSettings())
    assert len(requests) == 1
    spec = requests[0]
    assert isinstance(spec, RequestSpec)
    assert spec.provider_id == "met-no"
    assert spec.headers["User-Agent"] == (
        f"climate-cli/{climate.__version__} https://github.com/agentculture/climate-cli"
    )


def test_build_requests_has_no_conditional_header_with_no_prior_fetch() -> None:
    provider = MetNoProvider()
    spec = provider.build_requests(LOCATION, ProviderSettings())[0]
    assert spec.if_modified_since is None


def test_build_requests_rounds_coordinates_to_at_most_4_decimals() -> None:
    """Even a location configured with more precision is clamped to 4 decimals."""
    provider = MetNoProvider()
    extra_precise = _Location(
        NEUTRAL_LABEL,
        NEUTRAL_POINT[0] + 0.123456789,
        NEUTRAL_POINT[1] - 0.987654321,
    )
    spec = provider.build_requests(extra_precise, ProviderSettings())[0]
    query = spec.url.split("?", 1)[1]
    for part in query.split("&"):
        _, _, value = part.partition("=")
        decimals = value.split(".")[-1] if "." in value else ""
        assert len(decimals) <= 4, f"{part!r} carries more than 4 decimals"


def test_build_requests_carries_if_modified_since_from_the_last_fetch() -> None:
    provider = MetNoProvider()
    last = _fetch_record()
    spec = provider.build_requests(LOCATION, ProviderSettings(), last_fetch=last)[0]
    assert spec.if_modified_since == _CACHE_HEADERS["last-modified"]


def test_conditional_headers_returns_a_mapping_the_scheduler_merges_in() -> None:
    """The scheduler calls this directly (not through build_requests) and
    merges its return value into the request headers it sends."""
    assert MetNoProvider.conditional_headers(None) == {}

    last = _fetch_record()
    assert MetNoProvider.conditional_headers(last) == {
        "If-Modified-Since": _CACHE_HEADERS["last-modified"]
    }


def test_conditional_headers_is_empty_for_a_failed_fetch_with_no_headers() -> None:
    failed = FetchRecord(
        provider="met-no",
        endpoint="https://api.met.no/weatherapi/locationforecast/2.0/complete",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=None,
        error=FetchError(kind="timeout"),
    )
    assert MetNoProvider.conditional_headers(failed) == {}


def test_conditional_headers_reads_a_real_store_fetch_record_case_insensitively() -> None:
    """Exercises the exact shape the scheduler hands in: a real
    store.FetchRecord whose cache_headers carries Last-Modified."""
    record = FetchRecord(
        provider="met-no",
        endpoint="https://api.met.no/weatherapi/locationforecast/2.0/complete",
        location=NEUTRAL_LABEL,
        requested_at=NOW,
        status=200,
        body=b"{}",
        cache_headers={"Last-Modified": "Thu, 17 Sep 2026 18:27:16 GMT"},
    )
    assert MetNoProvider.conditional_headers(record) == {
        "If-Modified-Since": "Thu, 17 Sep 2026 18:27:16 GMT"
    }


# --- is_due (Expires-driven refresh) ----------------------------------------


def test_is_due_is_false_before_the_stored_expires_and_true_after() -> None:
    provider = MetNoProvider()
    last = _fetch_record()
    before_expiry = NOW + timedelta(minutes=20)
    after_expiry = NOW + timedelta(minutes=35)
    assert provider.is_due(before_expiry, last) is False
    assert provider.is_due(after_expiry, last) is True


# --- normalize ----------------------------------------------------------------


def test_a_304_response_normalizes_to_no_readings() -> None:
    provider = MetNoProvider()
    not_modified = _fetch_record(status=304, body=b"")
    assert provider.normalize(not_modified) == ()


def test_a_failed_fetch_normalizes_to_no_readings() -> None:
    provider = MetNoProvider()
    failed = _fetch_record(status=None, body=b"", error=FetchError(kind="timeout"))
    assert provider.normalize(failed) == ()


def test_model_run_at_is_the_feeds_updated_at_on_every_reading() -> None:
    """qodo-14: ``properties.meta.updated_at`` is met-no's model issue time.

    It is the provider's own statement of when this forecast was produced,
    so it rides on every reading of the fetch — and it is never the time we
    downloaded the response.
    """
    provider = MetNoProvider()
    readings = provider.normalize(_fetch_record())
    payload = json.loads(_BODY)
    updated_at = datetime.fromisoformat(
        payload["properties"]["meta"]["updated_at"].replace("Z", "+00:00")
    )

    assert readings
    assert {r.model_run_at for r in readings} == {updated_at}
    assert all(r.model_run_at != r.requested_at for r in readings)


def test_model_run_at_is_none_when_the_response_states_no_updated_at() -> None:
    provider = MetNoProvider()
    payload = json.loads(_BODY)
    del payload["properties"]["meta"]["updated_at"]
    readings = provider.normalize(_fetch_record(body=json.dumps(payload).encode("utf-8")))

    assert readings
    assert all(r.model_run_at is None for r in readings)


def test_build_requests_accepts_the_env_keyword_although_it_needs_no_credential() -> None:
    provider = MetNoProvider()
    assert provider.build_requests(LOCATION, ProviderSettings(), env={}) == provider.build_requests(
        LOCATION, ProviderSettings()
    )


def test_normalize_treats_the_first_entry_as_the_current_model_value() -> None:
    provider = MetNoProvider()
    record = _fetch_record()
    readings = provider.normalize(record)

    payload = json.loads(_BODY)
    timeseries = payload["properties"]["timeseries"]
    assert len(readings) == len(timeseries)

    first = readings[0]
    assert first.kind == "model"
    assert first.provider == "met-no"
    assert first.source != first.provider
    assert first.source == "locationforecast/2.0/complete"
    assert first.requested_at == NOW
    assert first.observed_at != first.requested_at

    expected_first_time = timeseries[0]["time"].replace("Z", "+00:00")
    assert first.observed_at == datetime.fromisoformat(expected_first_time)

    assert all(reading.kind == "forecast" for reading in readings[1:])
    # every later entry's observed_at differs from the model entry's.
    assert all(reading.observed_at != first.observed_at for reading in readings[1:])


def test_normalize_maps_instant_variables_to_their_documented_units() -> None:
    provider = MetNoProvider()
    readings = provider.normalize(_fetch_record())
    first = readings[0]

    assert first.values["temperature"].unit == "degC"
    assert first.values["apparent_temperature"].unit == "degC"
    assert first.values["dew_point"].unit == "degC"
    assert first.values["relative_humidity"].unit == "percent"
    assert first.values["pressure_msl"].unit == "hPa"
    assert first.values["wind_speed"].unit == "m_s"
    assert first.values["wind_direction"].unit == "deg"
    assert first.values["cloud_cover"].unit == "percent"
    assert first.values["cloud_cover_low"].unit == "percent"
    assert first.values["cloud_cover_medium"].unit == "percent"
    assert first.values["cloud_cover_high"].unit == "percent"
    assert first.values["fog"].unit == "percent"
    assert first.values["uv_index_clear_sky"].unit == "index"
    assert first.values["precipitation"].unit == "mm"
    assert first.values["weather_code"].unit == "code"
    assert isinstance(first.values["weather_code"].value, str)

    payload = json.loads(_BODY)
    first_entry = payload["properties"]["timeseries"][0]
    instant = first_entry["data"]["instant"]["details"]
    assert first.values["temperature"].value == instant["air_temperature"]
    assert first.values["temperature"].original_unit == "celsius"


def test_normalize_maps_the_next_6_hours_min_max_and_disambiguates_precipitation() -> None:
    """air_temperature_max/min map directly; precipitation_amount repeats across
    next_1_hours and next_6_hours in the same entry, so the 6-hour figure is
    kept under a disambiguated fallback id instead of being overwritten."""
    provider = MetNoProvider()
    readings = provider.normalize(_fetch_record())

    payload = json.loads(_BODY)
    timeseries = payload["properties"]["timeseries"]
    entry_with_both = next(
        (index, entry)
        for index, entry in enumerate(timeseries)
        if "precipitation_amount" in entry["data"].get("next_1_hours", {}).get("details", {})
        and "precipitation_amount" in entry["data"].get("next_6_hours", {}).get("details", {})
    )
    index, entry = entry_with_both
    reading = readings[index]

    assert reading.values["temperature_max"].unit == "degC"
    assert reading.values["temperature_min"].unit == "degC"
    assert (
        reading.values["temperature_max"].value
        == entry["data"]["next_6_hours"]["details"]["air_temperature_max"]
    )

    one_hour = entry["data"]["next_1_hours"]["details"]["precipitation_amount"]
    six_hour = entry["data"]["next_6_hours"]["details"]["precipitation_amount"]
    assert reading.values["precipitation"].value == one_hour
    fallback = reading.values["x_precipitation_amount_next_6_hours"]
    assert fallback.value == six_hour
    assert fallback.original_value == six_hour
    assert fallback.unit == "mm"


def test_normalize_drops_no_provider_value_from_instant_or_next_1_hours() -> None:
    """Every key in the fixture's first instant.details (and next_1_hours.details)
    appears in the reading's values under some id — known vocabulary id or a
    x_<name> fallback — per docs/weather-api.md section 4's 'no value dropped'
    rule."""
    provider = MetNoProvider()
    readings = provider.normalize(_fetch_record())
    first_values = readings[0].values

    payload = json.loads(_BODY)
    first_entry = payload["properties"]["timeseries"][0]
    instant_details = first_entry["data"]["instant"]["details"]
    next_1h_details = first_entry["data"].get("next_1_hours", {}).get("details", {})

    for raw_key, raw_value in instant_details.items():
        expected_id, _ = _INSTANT_VARIABLES.get(raw_key, (f"x_{raw_key}", None))
        assert expected_id in first_values, f"{raw_key!r} (as {expected_id!r}) was dropped"
        assert first_values[expected_id].original_value == raw_value

    for raw_key, raw_value in next_1h_details.items():
        expected_id = "precipitation" if raw_key == "precipitation_amount" else f"x_{raw_key}"
        assert expected_id in first_values, f"{raw_key!r} (as {expected_id!r}) was dropped"
        assert first_values[expected_id].original_value == raw_value


def test_normalize_is_pure_and_re_derivable() -> None:
    provider = MetNoProvider()
    record = _fetch_record()
    first_pass = provider.normalize(record)
    second_pass = provider.normalize(record)
    assert first_pass == second_pass
