"""The config, store and provider contracts were built in parallel.

These tests pin the seams between them, so an adapter can take a real
``config.ProviderSettings`` and a real ``store.FetchRecord`` as-is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from climate.weather import config as weather_config
from climate.weather.providers import base
from climate.weather.store import FetchRecord


class _ExpiresProvider(base.WeatherProvider):
    id = "seam-test"
    capabilities = frozenset({base.Capability.FORECAST})
    auth = base.AuthRequirement()
    default_interval_seconds = 1800
    quota = base.Quota(unlimited=True)
    freshness = base.FreshnessStrategy.HTTP_EXPIRES
    attribution = base.Attribution(text="seam", url="https://example.test/terms")

    def build_requests(self, location, settings=None):
        return []

    def normalize(self, fetch_record):
        return []


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _record(expires: str) -> FetchRecord:
    return FetchRecord(
        provider="seam-test",
        endpoint="https://example.test/forecast",
        location="home",
        requested_at=NOW - timedelta(hours=2),
        status=200,
        body=b"{}",
        cache_headers={"Expires": expires},
    )


def test_config_settings_satisfy_the_provider_settings_protocol() -> None:
    settings = weather_config.ProviderSettings(provider_id="seam-test", request_params={"a": 1})
    assert isinstance(settings, base.ProviderSettingsLike)
    assert settings.params == {"a": 1}


def test_unset_interval_falls_back_to_the_adapter_default() -> None:
    settings = weather_config.ProviderSettings(provider_id="seam-test")
    assert settings.interval_seconds is None
    assert _ExpiresProvider().interval_seconds(settings) == 1800


def test_configured_interval_overrides_the_adapter_default() -> None:
    settings = weather_config.ProviderSettings(provider_id="seam-test", interval_seconds=3600)
    assert _ExpiresProvider().interval_seconds(settings) == 3600


def test_is_due_reads_expires_from_a_real_fetch_record() -> None:
    provider = _ExpiresProvider()
    not_yet = _record("Thu, 01 Jan 2026 12:30:00 GMT")
    passed = _record("Thu, 01 Jan 2026 11:30:00 GMT")
    assert provider.is_due(NOW, not_yet) is False
    assert provider.is_due(NOW, passed) is True


def test_request_spec_never_prints_a_live_secret() -> None:
    spec = base.RequestSpec(
        provider_id="seam-test",
        location_label="home",
        url="https://example.test/data?appid=s3cr3tkey&units=metric",
        headers={"Authorization": "ApiToken t0k3nvalue"},
    )
    for rendered in (repr(spec), str(spec), f"{spec}"):
        assert "s3cr3tkey" not in rendered
        assert "t0k3nvalue" not in rendered
    assert "s3cr3tkey" in spec.url  # the real fetch still gets the key
    assert "s3cr3tkey" not in spec.redacted_url


def test_heartbeat_round_trips_through_both_stores_and_the_health_route() -> None:
    from climate.weather.mongo import MongoWeatherStore
    from climate.weather.store import InMemoryWeatherStore
    from climate.weather.web import api
    from tests.weather.test_mongo import FakeCollection

    stores = [
        InMemoryWeatherStore(),
        MongoWeatherStore(FakeCollection(), FakeCollection(), meta=FakeCollection()),
    ]
    for store in stores:
        assert store.latest_heartbeat() is None
        store.save_heartbeat("9.9.9", NOW)
        assert store.latest_heartbeat()["version"] == "9.9.9"
        config = weather_config.WeatherConfig(locations={}, providers={})
        status, body = api.health(store, config, [], {}, NOW)
        assert status == 200
        assert body["tracker_version"] == "9.9.9"


def test_web_service_store_factory_exists() -> None:
    from climate.weather import mongo

    assert callable(mongo.build_store)
    assert callable(mongo.lease_collection)
