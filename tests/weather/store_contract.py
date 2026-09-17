"""Reusable conformance suite for :class:`climate.weather.store.WeatherStore`.

Any store implementation (the in-memory fake, the Mongo-backed store) is
expected to pass every test in :class:`StoreContractTests`.  A new
implementation opts in with three lines::

    from tests.weather.store_contract import StoreContractTests

    class TestMongoStore(StoreContractTests):
        def make_store(self):
            return MongoWeatherStore(...)

The suite only uses the public protocol, builds its byte payloads inline
and never touches the network, the filesystem or ``tests/fixtures``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from climate.weather.store import (
    SCHEMA_VERSION,
    FetchError,
    FetchRecord,
    Measurement,
    Reading,
    UnknownFetchError,
    WeatherStore,
)

# --- inline byte payloads (no fixture files: another task owns tests/fixtures) ---

JSON_BODY = (
    b'{"current":{"time":"2026-09-17T09:00","interval":900,'
    b'"temperature_2m":27.4,"relative_humidity_2m":61.0}}'
)

# IMS serves ISO-8859-8 XML; re-encoding it as UTF-8 would change the bytes,
# so the store must keep it verbatim together with its declared charset.
XML_TEXT = (
    '<?xml version="1.0" encoding="ISO-8859-8"?>'
    "<Forecast><Element><Name>מזג אוויר</Name><Value>27.4</Value></Element></Forecast>"
)
XML_BODY = XML_TEXT.encode("iso-8859-8")

T0 = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def make_fetch(
    *,
    provider: str = "open-meteo",
    body: bytes = JSON_BODY,
    content_type: str | None = "application/json",
    charset: str | None = "utf-8",
    location: str = "reference-point",
    requested_at: datetime = T0,
    status: int | None = 200,
    cache_headers: dict[str, str] | None = None,
    error: FetchError | None = None,
    endpoint: str = "https://api.open-meteo.com/v1/forecast?latitude=0.00&longitude=0.00",
) -> FetchRecord:
    """Build a valid :class:`FetchRecord` with overridable fields."""
    return FetchRecord(
        provider=provider,
        endpoint=endpoint,
        location=location,
        requested_at=requested_at,
        status=status,
        body=body,
        content_type=content_type,
        charset=charset,
        cache_headers=cache_headers or {},
        error=error,
    )


def make_reading(
    *,
    provider: str = "open-meteo",
    source: str = "model",
    model: str | None = "best_match",
    location: str = "reference-point",
    observed_at: datetime = T0,
    requested_at: datetime = T0,
    kind: str = "observation",
    values: dict[str, Measurement] | None = None,
    fetch_id: str = "",
) -> Reading:
    """Build a valid :class:`Reading` with overridable fields."""
    return Reading(
        fetch_id=fetch_id,
        provider=provider,
        source=source,
        model=model,
        location=location,
        observed_at=observed_at,
        requested_at=requested_at,
        kind=kind,
        values=values
        or {
            "temperature": Measurement(27.4, "degC"),
            "relative_humidity": Measurement(61.0, "percent"),
        },
    )


class StoreContractTests:
    """Behaviour every :class:`WeatherStore` implementation must exhibit."""

    def make_store(self) -> WeatherStore:  # pragma: no cover - overridden
        raise NotImplementedError("subclasses must return a fresh, empty store")

    @pytest.fixture()
    def store(self) -> WeatherStore:
        return self.make_store()

    # --- raw fetch records -------------------------------------------------

    def test_save_fetch_returns_a_usable_id(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch())
        assert isinstance(fetch_id, str) and fetch_id
        stored = store.get_fetch(fetch_id)
        assert stored is not None
        assert stored.id == fetch_id

    def test_save_fetch_ids_are_unique(self, store: WeatherStore) -> None:
        first = store.save_fetch(make_fetch())
        second = store.save_fetch(make_fetch(requested_at=T0 + timedelta(minutes=5)))
        assert first != second

    def test_bytes_round_trip_verbatim_including_iso_8859_8(self, store: WeatherStore) -> None:
        record = make_fetch(
            provider="ims",
            body=XML_BODY,
            content_type="application/xml",
            charset="iso-8859-8",
            endpoint="https://ims.data.gov.il/ims/v1/envista/stations",
        )
        stored = store.get_fetch(store.save_fetch(record))
        assert stored is not None
        assert stored.body == XML_BODY
        assert stored.charset == "iso-8859-8"
        # the digest survives storage and matches a digest taken of the payload
        assert stored.sha256 == hashlib.sha256(XML_BODY).hexdigest()
        assert stored.body.decode("iso-8859-8") == XML_TEXT

    def test_all_fetch_metadata_round_trips(self, store: WeatherStore) -> None:
        record = make_fetch(
            provider="met-no",
            cache_headers={"Expires": "Thu, 17 Sep 2026 09:30:00 GMT", "ETag": '"abc"'},
        )
        stored = store.get_fetch(store.save_fetch(record))
        assert stored is not None
        assert stored.provider == "met-no"
        assert stored.endpoint == record.endpoint
        assert stored.location == record.location
        assert stored.requested_at == T0
        assert stored.status == 200
        assert stored.content_type == "application/json"
        assert stored.cache_headers == record.cache_headers
        assert stored.schema_version == SCHEMA_VERSION

    def test_get_fetch_unknown_id_returns_none(self, store: WeatherStore) -> None:
        assert store.get_fetch("does-not-exist") is None

    @pytest.mark.parametrize(
        ("status", "error", "body"),
        [
            (None, FetchError("timeout", "read timed out after 10s"), b""),
            (429, FetchError("rate_limited", "quota exceeded"), b'{"message":"rate limit"}'),
            (500, FetchError("http_status", "server error"), b"<html>oops</html>"),
            (404, FetchError("http_status", "not found"), b""),
            (304, None, b""),
        ],
    )
    def test_failed_and_not_modified_fetches_are_stored(
        self, store: WeatherStore, status: int | None, error: FetchError | None, body: bytes
    ) -> None:
        record = make_fetch(status=status, error=error, body=body, content_type=None, charset=None)
        stored = store.get_fetch(store.save_fetch(record))
        assert stored is not None
        assert stored.status == status
        assert stored.error == error
        assert stored.body == body  # no body loss: error pages are kept verbatim
        assert stored.sha256 == hashlib.sha256(body).hexdigest()
        assert stored.is_error is (status is None or status >= 400)
        assert store.readings_for_fetch(stored.id) == []

    def test_count_fetches_filters(self, store: WeatherStore) -> None:
        store.save_fetch(make_fetch(provider="met-no"))
        store.save_fetch(make_fetch(provider="met-no", requested_at=T0 + timedelta(minutes=5)))
        store.save_fetch(
            make_fetch(
                provider="ims",
                status=500,
                error=FetchError("http_status", "boom"),
                requested_at=T0 + timedelta(minutes=10),
            )
        )
        assert store.count_fetches() == 3
        assert store.count_fetches(provider="met-no") == 2
        assert store.count_fetches(errors_only=True) == 1
        assert store.count_fetches(since=T0 + timedelta(minutes=5)) == 2
        assert store.count_fetches(until=T0 + timedelta(minutes=5)) == 2
        assert store.count_fetches(location="nowhere") == 0

    def test_latest_fetch_is_the_newest_requested_at(self, store: WeatherStore) -> None:
        store.save_fetch(make_fetch(provider="met-no", requested_at=T0))
        newest = store.save_fetch(
            make_fetch(provider="met-no", requested_at=T0 + timedelta(hours=1))
        )
        store.save_fetch(make_fetch(provider="ims", requested_at=T0 + timedelta(hours=2)))
        latest = store.latest_fetch(provider="met-no")
        assert latest is not None and latest.id == newest
        any_latest = store.latest_fetch()
        assert any_latest is not None and any_latest.provider == "ims"
        assert store.latest_fetch(provider="openweather") is None

    def test_iter_fetches_is_newest_first_and_honours_limit(self, store: WeatherStore) -> None:
        ids = [
            store.save_fetch(make_fetch(requested_at=T0 + timedelta(minutes=5 * i)))
            for i in range(3)
        ]
        got = [record.id for record in store.iter_fetches()]
        assert got == list(reversed(ids))
        assert [record.id for record in store.iter_fetches(limit=2)] == list(reversed(ids))[:2]

    # --- normalized readings ----------------------------------------------

    def test_readings_require_an_existing_fetch(self, store: WeatherStore) -> None:
        with pytest.raises(UnknownFetchError):
            store.save_readings("no-such-fetch", [make_reading()])
        assert store.count_readings() == 0

    def test_saved_readings_are_bound_to_their_fetch(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch())
        (reading_id,) = store.save_readings(fetch_id, [make_reading()])
        assert isinstance(reading_id, str) and reading_id
        (stored,) = store.readings_for_fetch(fetch_id)
        assert stored.id == reading_id
        assert stored.fetch_id == fetch_id
        assert stored.values["temperature"] == Measurement(27.4, "degC")
        assert stored.schema_version == SCHEMA_VERSION

    def test_saving_a_reading_bound_to_another_fetch_is_refused(self, store: WeatherStore) -> None:
        first = store.save_fetch(make_fetch())
        second = store.save_fetch(make_fetch(requested_at=T0 + timedelta(minutes=5)))
        with pytest.raises(ValueError):
            store.save_readings(second, [make_reading(fetch_id=first)])
        assert store.readings_for_fetch(second) == []

    def test_provenance_round_trips(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch(provider="met-no"))
        reading = make_reading(
            provider="met-no",
            source="locationforecast/2.0/complete",
            model="harmonie",
            kind="model",
            observed_at=T0 - timedelta(minutes=12),
            requested_at=T0,
            values={
                "temperature": Measurement(27.4, "degC", original_value=81.3, original_unit="degF")
            },
        )
        store.save_readings(fetch_id, [reading])
        (stored,) = store.readings_for_fetch(fetch_id)
        assert (stored.provider, stored.source, stored.model) == (
            "met-no",
            "locationforecast/2.0/complete",
            "harmonie",
        )
        assert stored.kind == "model"
        assert stored.observed_at != stored.requested_at
        assert stored.values["temperature"].original_unit == "degF"
        assert stored.values["temperature"].original_value == 81.3

    def test_replace_readings_re_derives_without_touching_raw(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch())
        store.save_readings(fetch_id, [make_reading()])
        new_ids = store.replace_readings(
            fetch_id, [make_reading(values={"temperature": Measurement(27.5, "degC")})]
        )
        readings = store.readings_for_fetch(fetch_id)
        assert [r.id for r in readings] == new_ids
        assert readings[0].values["temperature"].value == 27.5
        assert store.count_readings() == 1
        raw = store.get_fetch(fetch_id)
        assert raw is not None and raw.body == JSON_BODY

    def test_replace_readings_requires_an_existing_fetch(self, store: WeatherStore) -> None:
        with pytest.raises(UnknownFetchError):
            store.replace_readings("nope", [])

    def test_latest_reading_filters_by_provider_location_and_kind(
        self, store: WeatherStore
    ) -> None:
        fetch_id = store.save_fetch(make_fetch())
        store.save_readings(
            fetch_id,
            [
                make_reading(provider="met-no", observed_at=T0),
                make_reading(provider="met-no", observed_at=T0 + timedelta(hours=1)),
                make_reading(provider="ims", observed_at=T0 + timedelta(hours=2)),
                make_reading(
                    provider="met-no",
                    kind="forecast",
                    observed_at=T0 + timedelta(hours=6),
                ),
                make_reading(provider="met-no", location="elsewhere", observed_at=T0),
            ],
        )
        latest = store.latest_reading(
            provider="met-no", location="reference-point", kind="observation"
        )
        assert latest is not None
        assert latest.observed_at == T0 + timedelta(hours=1)
        assert latest.kind == "observation"
        forecast = store.latest_reading(provider="met-no", kind="forecast")
        assert forecast is not None and forecast.observed_at == T0 + timedelta(hours=6)
        newest_anywhere = store.latest_reading()
        assert newest_anywhere is not None and newest_anywhere.observed_at == T0 + timedelta(
            hours=6
        )
        assert store.latest_reading(provider="unknown") is None

    def test_series_returns_points_in_time_order_with_provenance(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch())
        store.save_readings(
            fetch_id,
            [
                make_reading(
                    observed_at=T0 + timedelta(hours=2),
                    values={"temperature": Measurement(29.0, "degC")},
                ),
                make_reading(observed_at=T0, values={"temperature": Measurement(27.0, "degC")}),
                make_reading(
                    observed_at=T0 + timedelta(hours=1),
                    values={"relative_humidity": Measurement(55.0, "percent")},
                ),
            ],
        )
        points = store.series("temperature")
        assert [point.value for point in points] == [27.0, 29.0]
        assert [point.observed_at for point in points] == [T0, T0 + timedelta(hours=2)]
        assert points[0].unit == "degC"
        assert points[0].provider == "open-meteo"
        assert points[0].fetch_id == fetch_id
        assert store.series("temperature", since=T0 + timedelta(hours=1)) == points[1:]
        assert store.series("temperature", until=T0) == points[:1]
        assert store.series("temperature", limit=1) == points[:1]
        assert store.series("no_such_variable") == []

    def test_count_readings_filters(self, store: WeatherStore) -> None:
        fetch_id = store.save_fetch(make_fetch())
        store.save_readings(
            fetch_id,
            [
                make_reading(provider="met-no"),
                make_reading(provider="ims", kind="forecast"),
            ],
        )
        assert store.count_readings() == 2
        assert store.count_readings(provider="met-no") == 1
        assert store.count_readings(kind="forecast") == 1
        assert store.count_readings(location="elsewhere") == 0

    def test_stored_times_are_utc(self, store: WeatherStore) -> None:
        tz = timezone(timedelta(hours=3))
        fetch_id = store.save_fetch(
            make_fetch(requested_at=datetime(2026, 9, 17, 12, 0, tzinfo=tz))
        )
        stored = store.get_fetch(fetch_id)
        assert stored is not None
        assert stored.requested_at.tzinfo is UTC
        assert stored.requested_at == datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
        store.save_readings(
            fetch_id, [make_reading(observed_at=datetime(2026, 9, 17, 12, 0, tzinfo=tz))]
        )
        (reading,) = store.readings_for_fetch(fetch_id)
        assert reading.observed_at.tzinfo is UTC
        assert reading.observed_at == datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

    def test_stored_records_are_snapshots_not_live_references(self, store: WeatherStore) -> None:
        headers = {"Expires": "now"}
        record = make_fetch(cache_headers=headers)
        fetch_id = store.save_fetch(record)
        headers["Expires"] = "later"
        stored = store.get_fetch(fetch_id)
        assert stored is not None
        assert stored.cache_headers == {"Expires": "now"}
