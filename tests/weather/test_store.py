"""Unit tests for the weather storage model and the in-memory store."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from climate.weather.store import (
    REDACTED,
    SCHEMA_VERSION,
    FetchError,
    FetchRecord,
    InMemoryWeatherStore,
    Measurement,
    Reading,
    SecretLeakError,
    SeriesPoint,
    StoreError,
    UnknownFetchError,
    WeatherStore,
    redact_url,
)
from tests.weather.store_contract import (
    JSON_BODY,
    XML_BODY,
    XML_TEXT,
    StoreContractTests,
    make_fetch,
    make_reading,
)

T0 = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


class TestInMemoryStore(StoreContractTests):
    """The in-memory fake must satisfy the whole store contract."""

    def make_store(self) -> WeatherStore:
        return InMemoryWeatherStore()


def test_in_memory_store_is_a_weather_store() -> None:
    assert isinstance(InMemoryWeatherStore(), WeatherStore)


# --- Measurement ----------------------------------------------------------


def test_measurement_keeps_the_original_reading() -> None:
    measurement = Measurement(300.15, "K", original_value="300.15", original_unit="K")
    assert measurement.value == 300.15
    assert measurement.original_value == "300.15"


def test_measurement_requires_a_unit() -> None:
    with pytest.raises(ValueError):
        Measurement(1.0, "")


def test_measurement_defaults_original_to_none() -> None:
    measurement = Measurement(27.4, "degC")
    assert measurement.original_value is None
    assert measurement.original_unit is None


# --- FetchRecord ----------------------------------------------------------


def test_fetch_record_hashes_its_bytes() -> None:
    record = make_fetch(body=XML_BODY)
    assert record.sha256 == hashlib.sha256(XML_BODY).hexdigest()


def test_fetch_record_rejects_a_wrong_digest() -> None:
    with pytest.raises(ValueError, match="sha256"):
        FetchRecord(
            provider="ims",
            endpoint="https://example.test/x",
            location="reference-point",
            requested_at=T0,
            status=200,
            body=XML_BODY,
            sha256="0" * 64,
        )


def test_fetch_record_accepts_a_matching_digest_for_round_trips() -> None:
    digest = hashlib.sha256(JSON_BODY).hexdigest()
    record = FetchRecord(
        provider="open-meteo",
        endpoint="https://example.test/x",
        location="reference-point",
        requested_at=T0,
        status=200,
        body=JSON_BODY,
        sha256=digest,
    )
    assert record.sha256 == digest


def test_fetch_record_rejects_naive_times() -> None:
    naive = datetime(2026, 9, 17, 9, 0)
    with pytest.raises(ValueError, match="UTC"):
        make_fetch(requested_at=naive)


def test_fetch_record_normalizes_offsets_to_utc() -> None:
    record = make_fetch(
        requested_at=datetime(2026, 9, 17, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    )
    assert record.requested_at == T0


def test_fetch_record_requires_a_provider_and_location() -> None:
    with pytest.raises(ValueError):
        make_fetch(provider="")
    with pytest.raises(ValueError):
        make_fetch(location="")


def test_fetch_record_requires_a_status_or_an_error() -> None:
    with pytest.raises(ValueError, match="status"):
        make_fetch(status=None, error=None)


def test_fetch_record_body_must_be_bytes() -> None:
    with pytest.raises(TypeError):
        make_fetch(body=XML_TEXT)  # type: ignore[arg-type]


def test_fetch_record_is_error_classification() -> None:
    assert make_fetch(status=200).is_error is False
    assert make_fetch(status=304).is_error is False
    assert make_fetch(status=429).is_error is True
    assert make_fetch(status=None, error=FetchError("timeout", "slow")).is_error is True


def test_fetch_record_text_decodes_with_the_declared_charset() -> None:
    record = make_fetch(body=XML_BODY, content_type="application/xml", charset="iso-8859-8")
    assert record.text() == XML_TEXT


def test_fetch_record_text_falls_back_to_utf8() -> None:
    assert make_fetch(body=JSON_BODY, charset=None).text() == JSON_BODY.decode()


def test_fetch_record_refuses_an_unredacted_secret_in_the_endpoint() -> None:
    with pytest.raises(SecretLeakError):
        make_fetch(endpoint="https://api.openweathermap.org/data/2.5/weather?q=x&appid=s3cret")


def test_fetch_record_refuses_userinfo_in_the_endpoint() -> None:
    with pytest.raises(SecretLeakError):
        make_fetch(endpoint="https://user:pw@example.test/p")
    assert make_fetch(endpoint=redact_url("https://user:pw@example.test/p"))


def test_fetch_record_accepts_a_redacted_endpoint() -> None:
    url = f"https://api.openweathermap.org/data/2.5/weather?q=x&appid={REDACTED}"
    assert make_fetch(endpoint=url).endpoint == url


def test_fetch_record_defaults_schema_version() -> None:
    assert make_fetch().schema_version == SCHEMA_VERSION


def test_fetch_error_requires_a_kind() -> None:
    with pytest.raises(ValueError):
        FetchError("", "boom")


# --- redact_url -----------------------------------------------------------


@pytest.mark.parametrize(
    "param", ["appid", "APPID", "apikey", "api_key", "key", "token", "access_token", "ApiToken"]
)
def test_redact_url_redacts_known_secret_params(param: str) -> None:
    redacted = redact_url(f"https://example.test/p?lat=0.00&{param}=s3cret")
    assert "s3cret" not in redacted
    assert f"{param}={REDACTED}" in redacted
    assert "lat=0.00" in redacted


def test_redact_url_redacts_extra_named_params_and_values() -> None:
    url = "https://example.test/p?station=1&secretish=abc"
    assert "abc" not in redact_url(url, extra_params=("secretish",))
    assert "abc" not in redact_url(url, secret_values=("abc",))


def test_redact_url_redacts_userinfo() -> None:
    redacted = redact_url("https://user:pw@example.test/p")
    assert "pw" not in redacted


def test_redact_url_leaves_clean_urls_alone() -> None:
    url = "https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=0.00&lon=0.00"
    assert redact_url(url) == url


def test_redact_url_ignores_empty_secret_values() -> None:
    url = "https://example.test/p?a=1"
    assert redact_url(url, secret_values=("", None)) == url  # type: ignore[arg-type]


# --- Reading --------------------------------------------------------------


def test_reading_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        make_reading(kind="guess")


def test_reading_rejects_naive_times() -> None:
    naive = datetime(2026, 9, 17, 9, 0)
    with pytest.raises(ValueError, match="UTC"):
        make_reading(observed_at=naive)
    with pytest.raises(ValueError, match="UTC"):
        make_reading(requested_at=naive)


def test_reading_rejects_a_naive_model_run_at() -> None:
    naive = datetime(2026, 9, 17, 9, 0)
    with pytest.raises(ValueError, match="model_run_at"):
        make_reading(model_run_at=naive)


def test_reading_normalizes_model_run_at_to_utc() -> None:
    run_at = datetime(2026, 9, 17, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    assert make_reading(model_run_at=run_at).model_run_at == T0


def test_reading_model_run_at_defaults_to_none() -> None:
    assert make_reading().model_run_at is None


def test_reading_requires_values() -> None:
    with pytest.raises(ValueError, match="values"):
        Reading(
            provider="ims",
            source="station:1",
            location="reference-point",
            observed_at=T0,
            requested_at=T0,
            values={},
        )


def test_reading_requires_a_provider_source_and_location() -> None:
    with pytest.raises(ValueError):
        make_reading(provider="")
    with pytest.raises(ValueError):
        make_reading(source="")
    with pytest.raises(ValueError):
        make_reading(location="")


def test_reading_age_is_measured_from_observed_at() -> None:
    reading = make_reading(observed_at=T0)
    assert reading.age(now=T0 + timedelta(minutes=7)) == timedelta(minutes=7)


def test_reading_age_defaults_to_the_wall_clock() -> None:
    reading = make_reading(observed_at=datetime.now(UTC) - timedelta(minutes=3))
    assert timedelta(minutes=3) <= reading.age() < timedelta(minutes=4)


def test_reading_values_are_snapshots() -> None:
    values = {"temperature": Measurement(27.4, "degC")}
    reading = make_reading(values=values)
    values["temperature"] = Measurement(99.9, "degC")
    assert reading.values["temperature"].value == 27.4


# --- document (de)serialization used by the Mongo store -------------------


def test_fetch_record_document_round_trip_preserves_bytes() -> None:
    record = make_fetch(
        provider="ims",
        body=XML_BODY,
        content_type="application/xml",
        charset="iso-8859-8",
        cache_headers={"ETag": '"x"'},
        status=None,
        error=FetchError("timeout", "read timed out"),
    )
    document = record.to_document()
    assert isinstance(document["body"], bytes)
    assert document["schema_version"] == SCHEMA_VERSION
    restored = FetchRecord.from_document(document)
    assert restored == record
    assert restored.sha256 == hashlib.sha256(XML_BODY).hexdigest()


def test_fetch_record_document_carries_the_id_when_present() -> None:
    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(make_fetch())
    stored = store.get_fetch(fetch_id)
    assert stored is not None
    assert FetchRecord.from_document(stored.to_document()).id == fetch_id


def test_reading_document_round_trip() -> None:
    reading = make_reading(
        kind="forecast",
        values={
            "temperature": Measurement(27.4, "degC", original_value=81.3, original_unit="degF")
        },
        fetch_id="abc123",
    )
    restored = Reading.from_document(reading.to_document())
    assert restored == reading
    assert restored.values["temperature"].original_unit == "degF"


def test_reading_document_round_trips_model_run_at() -> None:
    run_at = T0 - timedelta(hours=6)
    reading = make_reading(kind="forecast", model_run_at=run_at)
    document = reading.to_document()
    assert document["model_run_at"] == run_at
    assert Reading.from_document(document).model_run_at == run_at


def test_reading_from_a_document_without_model_run_at_reads_as_none() -> None:
    """Documents stored before the field existed (schema_version 1) still load."""
    document = make_reading().to_document()
    del document["model_run_at"]
    assert Reading.from_document(document).model_run_at is None


def test_reading_document_carries_the_id_when_present() -> None:
    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(make_fetch())
    (reading_id,) = store.save_readings(fetch_id, [make_reading()])
    (stored,) = store.readings_for_fetch(fetch_id)
    restored = Reading.from_document(stored.to_document())
    assert restored.id == reading_id
    assert restored.fetch_id == fetch_id


# --- in-memory specifics --------------------------------------------------


def test_unknown_fetch_error_is_a_store_error() -> None:
    assert issubclass(UnknownFetchError, StoreError)
    assert issubclass(SecretLeakError, ValueError)


def test_clear_empties_the_in_memory_store() -> None:
    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(make_fetch())
    store.save_readings(fetch_id, [make_reading()])
    store.clear()
    assert store.count_fetches() == 0
    assert store.count_readings() == 0
    assert store.get_fetch(fetch_id) is None


def test_save_readings_with_no_readings_is_a_no_op() -> None:
    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(make_fetch())
    assert store.save_readings(fetch_id, []) == []
    assert store.count_readings() == 0


def test_series_point_is_a_plain_value() -> None:
    point = SeriesPoint(
        observed_at=T0,
        value=27.4,
        unit="degC",
        provider="ims",
        source="station:1",
        model=None,
        kind="observation",
        fetch_id="f1",
    )
    # model_run_at is a trailing field with a default: the pre-existing
    # positional construction below must keep working unchanged.
    assert point == SeriesPoint(T0, 27.4, "degC", "ims", "station:1", None, "observation", "f1")
    assert point.model_run_at is None


def test_series_point_can_carry_the_provider_model_run_time() -> None:
    run_at = T0 - timedelta(hours=3)
    point = SeriesPoint(T0, 27.4, "degC", "ims", "station:1", None, "forecast", "f1", run_at)
    assert point.model_run_at == run_at
