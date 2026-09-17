"""Tests for :mod:`climate.weather.rederive`.

Two groups of tests:

* Behavioural tests against a small in-memory fake provider — the counting,
  skipping and per-record failure-isolation logic in :func:`rederive` itself.
* An equality test per real provider adapter, built the way the scheduler
  builds a :class:`~climate.weather.store.FetchRecord` (verbatim fixture
  bytes, response headers turned into ``content_type``/``charset``/
  ``cache_headers`` by :func:`climate.weather.scheduler._build_record`) —
  proving the rebuilt readings equal the originally normalized ones, and
  that every reading any registered adapter produces references a fetch
  record the store can resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pytest

from climate.weather import scheduler as sched
from climate.weather.http import FetchResult
from climate.weather.providers import iter_providers, provider_ids
from climate.weather.providers.base import ProviderSettings, RequestSpec
from climate.weather.providers.ims import ImsProvider
from climate.weather.providers.ims_forecast import ImsForecastProvider
from climate.weather.providers.met_no import MetNoProvider
from climate.weather.providers.metar import MetarProvider
from climate.weather.providers.open_meteo import OpenMeteoProvider
from climate.weather.providers.openweather import ENV_VAR as OPENWEATHER_ENV_VAR
from climate.weather.providers.openweather import OpenWeatherProvider
from climate.weather.rederive import RederiveResult, rederive
from climate.weather.store import FetchRecord, InMemoryWeatherStore, Measurement, Reading
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

REQUESTED_AT = datetime(2026, 9, 17, 18, 5, tzinfo=UTC)

IMS_TOKEN_ENV = "CLIMATE_IMS_API_TOKEN"


@dataclass(frozen=True)
class _Location:
    label: str
    latitude: float
    longitude: float


LOCATION = _Location(NEUTRAL_LABEL, *NEUTRAL_POINT)


# --- a tiny fake provider for the behavioural tests -------------------------


class _FakeProvider:
    """A minimal :class:`~climate.weather.rederive.ProviderLike`.

    ``normalize`` returns one reading per call by default, keyed off the
    fetch record's id so re-derived readings are distinguishable, or raises
    when the fetch id is in ``fails_on``.
    """

    def __init__(self, provider_id: str, *, fails_on: frozenset[str] = frozenset()) -> None:
        self.id = provider_id
        self.fails_on = fails_on
        self.calls: list[str] = []

    def normalize(self, fetch_record: FetchRecord) -> Sequence[Reading]:
        self.calls.append(fetch_record.id)
        if fetch_record.id in self.fails_on:
            raise ValueError(f"boom on {fetch_record.id}")
        return [
            Reading(
                provider=self.id,
                source="fake",
                location=fetch_record.location,
                observed_at=fetch_record.requested_at,
                requested_at=fetch_record.requested_at,
                kind="observation",
                values={"temperature": Measurement(value=1.0, unit="degC")},
            )
        ]


def _save_fetch(
    store: InMemoryWeatherStore,
    provider_id: str,
    *,
    status: int | None = 200,
    body: bytes = b"{}",
    error: Any = None,
    requested_at: datetime = REQUESTED_AT,
) -> str:
    record = FetchRecord(
        provider=provider_id,
        endpoint=f"https://example.test/{provider_id}",
        location=NEUTRAL_LABEL,
        requested_at=requested_at,
        status=status,
        body=body,
        error=error,
    )
    return store.save_fetch(record)


# --- behavioural tests -------------------------------------------------------


def test_rederives_a_healthy_record_and_reports_counts() -> None:
    store = InMemoryWeatherStore()
    fetch_id = _save_fetch(store, "fake")
    provider = _FakeProvider("fake")

    result = rederive(store, [provider])

    assert isinstance(result, RederiveResult)
    assert result.fetches_seen == 1
    assert result.rederived == 1
    assert result.skipped == 0
    assert result.failed == 0
    assert result.readings_written == 1
    assert result.failures == ()
    assert [r.provider for r in store.readings_for_fetch(fetch_id)] == ["fake"]


def test_skips_error_fetch_records() -> None:
    store = InMemoryWeatherStore()
    from climate.weather.store import FetchError

    _save_fetch(store, "fake", status=500, error=FetchError(kind="http_error", message="HTTP 500"))
    provider = _FakeProvider("fake")

    result = rederive(store, [provider])

    assert result.fetches_seen == 1
    assert result.rederived == 0
    assert result.skipped == 1
    assert result.failed == 0
    assert provider.calls == []


def test_skips_304_records() -> None:
    store = InMemoryWeatherStore()
    _save_fetch(store, "fake", status=304, body=b"")
    provider = _FakeProvider("fake")

    result = rederive(store, [provider])

    assert result.skipped == 1
    assert result.rederived == 0
    assert provider.calls == []


def test_skips_empty_body_records() -> None:
    store = InMemoryWeatherStore()
    _save_fetch(store, "fake", status=200, body=b"")
    provider = _FakeProvider("fake")

    result = rederive(store, [provider])

    assert result.skipped == 1
    assert provider.calls == []


def test_skips_records_with_no_matching_adapter() -> None:
    store = InMemoryWeatherStore()
    _save_fetch(store, "unknown-provider")

    result = rederive(store, [_FakeProvider("fake")])

    assert result.fetches_seen == 1
    assert result.skipped == 1
    assert result.rederived == 0
    assert result.failed == 0


def test_never_touches_the_raw_record() -> None:
    store = InMemoryWeatherStore()
    fetch_id = _save_fetch(store, "fake", body=b'{"x": 1}')
    before = store.get_fetch(fetch_id)

    rederive(store, [_FakeProvider("fake")])

    after = store.get_fetch(fetch_id)
    assert after == before


def test_a_failing_normalize_is_isolated_counted_and_leaves_old_readings() -> None:
    store = InMemoryWeatherStore()
    good_id = _save_fetch(store, "fake", requested_at=REQUESTED_AT)
    bad_id = _save_fetch(store, "fake", requested_at=REQUESTED_AT.replace(minute=10))
    # Seed pre-existing readings for the record that will fail to re-derive.
    old_reading = Reading(
        provider="fake",
        source="fake",
        location=NEUTRAL_LABEL,
        observed_at=REQUESTED_AT,
        requested_at=REQUESTED_AT,
        kind="observation",
        values={"temperature": Measurement(value=9.0, unit="degC")},
    )
    store.save_readings(bad_id, [old_reading])

    provider = _FakeProvider("fake", fails_on=frozenset({bad_id}))
    result = rederive(store, [provider])

    assert result.fetches_seen == 2
    assert result.rederived == 1
    assert result.failed == 1
    assert result.skipped == 0
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.fetch_id == bad_id
    assert failure.provider == "fake"
    assert "boom" in failure.error

    # The good record was re-derived...
    assert len(store.readings_for_fetch(good_id)) == 1
    # ...and the bad record's readings are exactly what they were before.
    kept = store.readings_for_fetch(bad_id)
    assert len(kept) == 1
    assert kept[0].values["temperature"].value == 9.0


def test_a_record_that_normalizes_to_zero_readings_still_counts_as_rederived() -> None:
    store = InMemoryWeatherStore()
    fetch_id = _save_fetch(store, "fake")

    class _NoReadingsProvider:
        id = "fake"

        def normalize(self, fetch_record: Any) -> Sequence[Reading]:
            return []

    result = rederive(store, [_NoReadingsProvider()])

    assert result.rederived == 1
    assert result.skipped == 0
    assert result.readings_written == 0
    assert store.readings_for_fetch(fetch_id) == []


def test_filters_are_passed_through_to_iter_fetches() -> None:
    store = InMemoryWeatherStore()
    home_id = _save_fetch(store, "fake")
    other = FetchRecord(
        provider="fake",
        endpoint="https://example.test/fake",
        location="away",
        requested_at=REQUESTED_AT,
        status=200,
        body=b"{}",
    )
    away_id = store.save_fetch(other)

    result = rederive(store, [_FakeProvider("fake")], location=NEUTRAL_LABEL)

    assert result.fetches_seen == 1
    assert store.readings_for_fetch(home_id) != []
    assert store.readings_for_fetch(away_id) == []


# --- real-adapter fixture equality tests ------------------------------------


def _record(
    provider_id: str,
    spec: RequestSpec,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    status: int = 200,
    requested_at: datetime = REQUESTED_AT,
) -> FetchRecord:
    """Build a :class:`FetchRecord` the way the scheduler does.

    Wraps ``body`` and ``headers`` in a :class:`~climate.weather.http.FetchResult`
    and hands it, together with the real :class:`RequestSpec` the adapter
    itself produced, to the scheduler's own record-assembly helper — the
    same function that turns a live HTTP round trip into a stored record.
    """
    result = FetchResult(url=spec.url, status=status, headers=dict(headers or {}), body=body)
    return sched._build_record(provider_id, spec, requested_at, result, None)


def _open_meteo_case() -> tuple[Any, FetchRecord]:
    provider = OpenMeteoProvider()
    (spec,) = provider.build_requests(LOCATION)
    body = (FIXTURES / "open_meteo_forecast.json").read_bytes()
    record = _record(
        provider.id, spec, body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    return provider, record


def _met_no_case() -> tuple[Any, FetchRecord]:
    provider = MetNoProvider()
    (spec,) = provider.build_requests(LOCATION)
    body = (FIXTURES / "met_no_locationforecast.json").read_bytes()
    headers_text = (FIXTURES / "met_no_locationforecast_headers.txt").read_text(encoding="utf-8")
    headers: dict[str, str] = {}
    for line in headers_text.splitlines()[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    record = _record(provider.id, spec, body, headers=headers)
    return provider, record


def _openweather_case(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FetchRecord]:
    monkeypatch.setenv(OPENWEATHER_ENV_VAR, "fake-openweather-key-do-not-use")
    provider = OpenWeatherProvider()
    (spec,) = provider.build_requests(LOCATION)
    body = (FIXTURES / "openweather_current.json").read_bytes()
    record = _record(
        provider.id, spec, body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    return provider, record


def _metar_case() -> tuple[Any, FetchRecord]:
    provider = MetarProvider()
    settings = ProviderSettings(enabled=True, params={"stations": ["LLBG"]})
    (spec,) = provider.build_requests(LOCATION, settings)
    body = (FIXTURES / "metar_llbg.json").read_bytes()
    record = _record(
        provider.id, spec, body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    return provider, record


def _ims_forecast_case() -> tuple[Any, FetchRecord]:
    provider = ImsForecastProvider()
    settings = ProviderSettings(enabled=True, params={"cities": ["Herzliya", "Tel Aviv - Yafo"]})
    (spec,) = provider.build_requests(LOCATION, settings)
    body = (FIXTURES / "ims_isr_cities.xml").read_bytes()
    record = _record(
        provider.id, spec, body, headers={"Content-Type": "text/xml; charset=iso-8859-8"}
    )
    return provider, record


def _ims_case(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FetchRecord]:
    # This provider's fixture equality case uses only the "latest data"
    # response (station id 1, as in ``ims_latest.json``) -- never the
    # "stations" list fixture. ``ImsProvider.normalize`` resolves channel
    # identity from the response's own self-reported ``name`` field when it
    # has no cached station metadata (see ``_channel_identity``), so this
    # case exercises normalization with an *empty* instance-level station
    # cache the whole way through, exactly like a fresh process would if the
    # very first fetch it ever re-derived was a "latest" record.
    monkeypatch.setenv(IMS_TOKEN_ENV, "test-token")
    provider = ImsProvider()
    settings = ProviderSettings(params={"station_ids": [1]})
    (spec,) = provider.build_requests(LOCATION, settings)
    body = (FIXTURES / "ims_latest.json").read_bytes()
    record = _record(
        provider.id, spec, body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    return provider, record


_CASES = {
    "open-meteo": lambda mp: _open_meteo_case(),
    "met-no": lambda mp: _met_no_case(),
    "openweather": _openweather_case,
    "metar": lambda mp: _metar_case(),
    "ims-forecast": lambda mp: _ims_forecast_case(),
    "ims": _ims_case,
}


def test_every_registered_adapter_has_a_rederive_fixture_case() -> None:
    """A new adapter must be given a case here, not silently skipped."""
    assert set(_CASES) == set(provider_ids())


def _strip_id(reading: Reading) -> dict[str, Any]:
    """A reading's document with its generated ``id``/``fetch_id`` removed.

    ``fetch_id`` is store-assigned (both ``save_readings`` and
    ``replace_readings`` bind it), not part of what ``normalize()`` itself
    produces, so it is excluded from this equality check the same way
    ``id`` is; :attr:`Reading.fetch_id` is asserted separately against the
    real fetch id where it matters.
    """
    document = reading.to_document()
    document.pop("id", None)
    document.pop("fetch_id", None)
    return document


@pytest.mark.parametrize("provider_id", sorted(_CASES))
def test_rederived_readings_equal_the_originals_for_every_provider_fixture(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, record = _CASES[provider_id](monkeypatch)
    original_readings = provider.normalize(record)
    assert original_readings, f"{provider_id}'s fixture normalizes to no readings"

    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(record)
    store.save_readings(fetch_id, original_readings)

    result = rederive(store, [provider])

    assert result.fetches_seen == 1
    assert result.rederived == 1
    assert result.failed == 0
    assert result.skipped == 0

    rebuilt_readings = store.readings_for_fetch(fetch_id)
    assert [_strip_id(r) for r in rebuilt_readings] == [_strip_id(r) for r in original_readings]
    for reading in rebuilt_readings:
        assert reading.fetch_id == fetch_id


@pytest.mark.parametrize("provider_id", sorted(_CASES))
def test_every_reading_saved_through_the_store_references_a_resolvable_fetch(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """100% of readings any adapter produces reference an existing fetch record."""
    provider, record = _CASES[provider_id](monkeypatch)
    readings = provider.normalize(record)
    assert readings, f"{provider_id}'s fixture normalizes to no readings"

    store = InMemoryWeatherStore()
    fetch_id = store.save_fetch(record)
    store.save_readings(fetch_id, readings)

    for stored in store.readings_for_fetch(fetch_id):
        assert store.get_fetch(stored.fetch_id) is not None


def test_registry_providers_can_be_handed_straight_to_rederive() -> None:
    """``rederive`` accepts the real registry, not just hand-built fakes."""
    store = InMemoryWeatherStore()
    result = rederive(store, iter_providers())
    assert result == RederiveResult()


def test_ims_newest_first_rederive_order_does_not_break_station_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``iter_fetches`` replays newest first; the IMS adapter tolerates it.

    :class:`ImsProvider` keeps an instance-level station-metadata cache
    populated only by normalizing a "stations" list response. If a
    "latest" record is requested *after* the "stations" record but
    re-derived *before* it (newest first), the cache would not be primed
    yet when that "latest" record's readings are rebuilt. This is not a
    bug: ``_channel_identity`` falls back to the "latest" response's own
    self-reported channel ``name`` whenever no cache entry is available
    (see ``climate/weather/providers/ims.py``), so the rebuilt reading is
    identical whether or not the station cache happened to be warm yet.
    """
    monkeypatch.setenv(IMS_TOKEN_ENV, "test-token")
    provider = ImsProvider()
    stations_spec = RequestSpec(
        provider_id=provider.id,
        location_label=NEUTRAL_LABEL,
        url="https://api.ims.gov.il/v1/envista/stations",
        purpose="stations",
    )
    stations_body = (FIXTURES / "ims_stations.json").read_bytes()
    stations_record = _record(
        provider.id,
        stations_spec,
        stations_body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        requested_at=REQUESTED_AT,
    )

    settings = ProviderSettings(params={"station_ids": [1]})
    (latest_spec,) = provider.build_requests(LOCATION, settings)
    latest_body = (FIXTURES / "ims_latest.json").read_bytes()
    latest_record = _record(
        provider.id,
        latest_spec,
        latest_body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        # Requested strictly after the stations record, so newest-first
        # iteration visits it *first* -- before the stations cache is warm.
        requested_at=REQUESTED_AT.replace(minute=10),
    )

    # The expected reading content: derived once, fresh, with an empty cache.
    fresh_provider = ImsProvider()
    expected = [_strip_id(r) for r in fresh_provider.normalize(latest_record)]
    assert expected, "ims_latest.json must normalize to at least one reading"

    store = InMemoryWeatherStore()
    stations_id = store.save_fetch(stations_record)
    latest_id = store.save_fetch(latest_record)

    result = rederive(store, [provider])

    assert result.fetches_seen == 2
    assert result.rederived == 2
    assert result.failed == 0

    rebuilt = [_strip_id(r) for r in store.readings_for_fetch(latest_id)]
    assert rebuilt == expected
    assert store.readings_for_fetch(stations_id) == []
