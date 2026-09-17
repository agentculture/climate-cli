"""Tests for the weather provider contract and the adapter registry.

These tests pin the contract every adapter (open-meteo, met-no, openweather,
ims, metar, ims-forecast) binds against, plus the pkgutil-driven registry the
host ``climate providers`` verb and the scheduler read.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from climate.weather import providers as registry
from climate.weather.providers import base
from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Availability,
    Capability,
    FreshnessStrategy,
    ProviderSettings,
    Quota,
    RequestSpec,
    WeatherProvider,
    validate_provider,
)
from tests.weather.neutral import fake_secret

# --- helpers --------------------------------------------------------------


@dataclass(frozen=True)
class _Location:
    """Stand-in for the config task's location type (structural match)."""

    label: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class _FetchRecord:
    """Stand-in for the storage task's fetch record (structural match)."""

    requested_at: datetime
    status: int | None = 200
    headers: dict[str, str] | None = None
    body: bytes = b""
    provider: str = "demo"
    location_label: str = "home"
    endpoint: str = "https://example.invalid/v1"
    error: str | None = None


def _provider_class(**overrides: Any) -> type[WeatherProvider]:
    attrs: dict[str, Any] = {
        "id": "demo",
        "capabilities": frozenset({Capability.CURRENT_OBSERVATION}),
        "auth": AuthRequirement(required=False),
        "default_interval_seconds": 600,
        "quota": Quota(calls_per_day=1000, source="docs"),
        "freshness": FreshnessStrategy.INTERVAL,
        "attribution": Attribution(
            text="Demo data",
            url="https://example.invalid/licence",
            licence="CC BY 4.0",
        ),
        "build_requests": lambda self, location, settings=None: (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url="https://example.invalid/v1?lat=%s" % location.latitude,
            ),
        ),
        "normalize": lambda self, fetch_record: (),
    }
    attrs.update(overrides)
    return type("DemoProvider", (WeatherProvider,), attrs)


_NEUTRAL_POINT = (51.48, -0.0)  # Royal Observatory Greenwich, neutral test point
LOCATION = _Location("home", *_NEUTRAL_POINT)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


# --- value types ----------------------------------------------------------


def test_freshness_strategies_are_exactly_the_three_the_spec_names() -> None:
    assert {s.value for s in FreshnessStrategy} == {
        "interval",
        "http-expires",
        "station-cadence",
    }


def test_attribution_requires_text_and_url() -> None:
    with pytest.raises(ValueError):
        Attribution(text="", url="https://example.invalid")
    with pytest.raises(ValueError):
        Attribution(text="Demo", url="   ")


def test_quota_reports_daily_call_budget_and_fit() -> None:
    quota = Quota(calls_per_day=1000, call_weight=5)
    # 2 locations, every 900 s => 96 fetches/day/location, weight 5 => 960.
    assert quota.daily_calls(interval_seconds=900, locations=2) == pytest.approx(960.0)
    assert quota.fits(interval_seconds=900, locations=2) is True
    assert quota.fits(interval_seconds=900, locations=3) is False


def test_quota_without_limits_must_declare_itself_unlimited() -> None:
    assert Quota(unlimited=True).fits(interval_seconds=300, locations=99) is True
    assert Quota(unlimited=True).daily_calls(interval_seconds=300, locations=1) > 0


def test_request_spec_rejects_non_https_urls() -> None:
    with pytest.raises(ValueError):
        RequestSpec(provider_id="demo", location_label="home", url="http://example.invalid")


def test_request_spec_headers_and_context_are_immutable() -> None:
    spec = RequestSpec(
        provider_id="demo",
        location_label="home",
        url="https://example.invalid/v1",
        headers={"User-Agent": "climate-cli"},
        context={"block": "current"},
    )
    with pytest.raises(TypeError):
        spec.headers["User-Agent"] = "other"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.context["block"] = "other"  # type: ignore[index]


def test_provider_settings_defaults_are_usable_without_the_config_module() -> None:
    settings = ProviderSettings()
    assert settings.enabled is True
    assert settings.interval_seconds is None
    assert settings.params == {}


# --- provider contract surface -------------------------------------------


def test_provider_exposes_the_contract_the_brief_names() -> None:
    provider = _provider_class()()
    assert provider.id == "demo"
    assert Capability.CURRENT_OBSERVATION in provider.capabilities
    assert provider.auth.required is False
    assert provider.default_interval_seconds == 600
    assert provider.quota.calls_per_day == 1000
    assert provider.freshness is FreshnessStrategy.INTERVAL
    assert provider.attribution.text
    assert provider.attribution.url
    requests = provider.build_requests(LOCATION, ProviderSettings())
    expected = f"https://example.invalid/v1?lat={_NEUTRAL_POINT[0]}"
    assert [r.url for r in requests] == [expected]
    assert provider.normalize(_FetchRecord(requested_at=NOW)) == ()


def test_abstract_methods_cannot_be_skipped() -> None:
    with pytest.raises(TypeError):
        WeatherProvider()  # type: ignore[abstract]

    class Partial(WeatherProvider):
        id = "partial"

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_interval_seconds_prefers_settings_over_the_adapter_default() -> None:
    provider = _provider_class()()
    assert provider.interval_seconds(None) == 600
    assert provider.interval_seconds(ProviderSettings(interval_seconds=120)) == 120


# --- availability ---------------------------------------------------------


def test_keyless_provider_is_available_without_environment() -> None:
    provider = _provider_class()()
    assert provider.availability(env={}) == Availability(enabled=True, reason=None)


def test_provider_missing_its_key_is_disabled_with_a_reason_not_an_error() -> None:
    provider = _provider_class(auth=AuthRequirement(required=True, env_var="CLIMATE_DEMO_KEY"))()
    unavailable = provider.availability(env={})
    assert unavailable.enabled is False
    assert "CLIMATE_DEMO_KEY" in (unavailable.reason or "")
    assert provider.availability(env={"CLIMATE_DEMO_KEY": "x"}).enabled is True


def test_settings_disabled_flag_wins_and_explains_itself() -> None:
    provider = _provider_class()()
    result = provider.availability(settings=ProviderSettings(enabled=False), env={})
    assert result.enabled is False
    assert "configuration" in (result.reason or "")


# --- the credential seam --------------------------------------------------


def test_credential_reads_the_declared_variable_from_the_given_mapping() -> None:
    provider = _provider_class(auth=AuthRequirement(required=True, env_var="CLIMATE_DEMO_KEY"))()
    secret = fake_secret("demo-key")
    assert provider.credential({"CLIMATE_DEMO_KEY": secret}) == secret


def test_credential_is_none_when_the_variable_is_absent_or_empty() -> None:
    provider = _provider_class(auth=AuthRequirement(required=True, env_var="CLIMATE_DEMO_KEY"))()
    assert provider.credential({}) is None
    assert provider.credential({"CLIMATE_DEMO_KEY": ""}) is None


def test_a_keyless_provider_has_no_credential_whatever_the_environment_holds() -> None:
    provider = _provider_class()()
    assert provider.credential({"CLIMATE_DEMO_KEY": fake_secret("demo-key")}) is None


def test_availability_and_credential_agree_on_the_same_mapping() -> None:
    """The point of the seam: one mapping decides both answers."""
    provider = _provider_class(auth=AuthRequirement(required=True, env_var="CLIMATE_DEMO_KEY"))()
    env = {"CLIMATE_DEMO_KEY": fake_secret("demo-key")}
    assert provider.availability(env=env).enabled is True
    assert provider.credential(env) is not None
    assert provider.availability(env={}).enabled is False
    assert provider.credential({}) is None


# --- the request-cost hook ------------------------------------------------


def test_one_location_costs_one_request_by_default() -> None:
    provider = _provider_class()()
    assert provider.requests_per_tick(LOCATION) == 1
    assert provider.requests_per_tick(LOCATION, ProviderSettings()) == 1


def test_an_adapter_can_declare_a_higher_request_cost() -> None:
    provider = _provider_class(
        requests_per_tick=lambda self, location, settings=None: len(
            (getattr(settings, "params", None) or {}).get("station_ids") or ["one"]
        )
    )()
    settings = ProviderSettings(params={"station_ids": ["a", "b", "c"]})
    assert provider.requests_per_tick(LOCATION, settings) == 3
    assert provider.requests_per_tick(LOCATION) == 1


def test_build_requests_accepts_the_env_keyword_in_the_contract() -> None:
    """The scheduler calls ``build_requests(location, settings, env=...)``."""
    import inspect

    parameters = inspect.signature(WeatherProvider.build_requests).parameters
    assert "env" in parameters
    assert parameters["env"].default is None


# --- is_due ---------------------------------------------------------------


def test_is_due_is_true_when_nothing_was_ever_fetched() -> None:
    assert _provider_class()().is_due(NOW, None) is True


def test_interval_strategy_waits_for_its_own_interval() -> None:
    provider = _provider_class()()
    recent = _FetchRecord(requested_at=NOW - timedelta(seconds=300))
    stale = _FetchRecord(requested_at=NOW - timedelta(seconds=601))
    assert provider.is_due(NOW, recent) is False
    assert provider.is_due(NOW, stale) is True


def test_http_expires_strategy_holds_off_until_the_stored_expires() -> None:
    provider = _provider_class(
        freshness=FreshnessStrategy.HTTP_EXPIRES, default_interval_seconds=300
    )()
    last = _FetchRecord(
        requested_at=NOW - timedelta(seconds=900),
        headers={"Expires": "Thu, 17 Sep 2026 12:30:00 GMT"},
    )
    assert provider.is_due(NOW, last) is False
    assert provider.is_due(NOW + timedelta(minutes=31), last) is True


def test_http_expires_falls_back_to_the_interval_without_a_usable_header() -> None:
    provider = _provider_class(
        freshness=FreshnessStrategy.HTTP_EXPIRES, default_interval_seconds=300
    )()
    assert provider.is_due(NOW, _FetchRecord(requested_at=NOW - timedelta(seconds=60))) is False
    assert provider.is_due(NOW, _FetchRecord(requested_at=NOW - timedelta(seconds=301))) is True
    garbled = _FetchRecord(requested_at=NOW - timedelta(seconds=301), headers={"Expires": "soon"})
    assert provider.is_due(NOW, garbled) is True


def test_station_cadence_strategy_reads_its_cadence_from_settings() -> None:
    provider = _provider_class(
        freshness=FreshnessStrategy.STATION_CADENCE, default_interval_seconds=600
    )()
    settings = ProviderSettings(params={"cadence_seconds": 3600})
    last = _FetchRecord(requested_at=NOW - timedelta(seconds=1800))
    assert provider.is_due(NOW, last, settings=settings) is False
    assert provider.is_due(NOW + timedelta(seconds=1900), last, settings=settings) is True


def test_naive_last_fetch_timestamps_are_treated_as_utc() -> None:
    provider = _provider_class()()
    naive = _FetchRecord(requested_at=datetime(2026, 9, 17, 11, 59))
    assert provider.is_due(NOW, naive) is False


# --- describe / validate --------------------------------------------------


def test_describe_is_json_ready_metadata_for_the_providers_verb() -> None:
    provider = _provider_class()()
    row = provider.describe(settings=ProviderSettings(), env={})
    assert row["id"] == "demo"
    assert row["freshness"] == "interval"
    assert row["capabilities"] == ["current-observation"]
    assert row["auth"]["required"] is False
    assert row["quota"]["calls_per_day"] == 1000
    assert row["attribution"]["text"] == "Demo data"
    assert row["enabled"] is True
    assert row["interval_seconds"] == 600


def test_validate_provider_accepts_a_complete_adapter() -> None:
    assert validate_provider(_provider_class()()) == []


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"attribution": None}, "attribution"),
        ({"quota": None}, "quota"),
        ({"freshness": None}, "freshness"),
        ({"id": ""}, "id"),
        ({"capabilities": frozenset()}, "capabilities"),
        ({"default_interval_seconds": 0}, "interval"),
        ({"quota": Quota()}, "quota"),
    ],
)
def test_validate_provider_reports_an_incomplete_adapter(
    overrides: dict[str, Any], needle: str
) -> None:
    problems = validate_provider(_provider_class(**overrides)())
    assert problems, f"expected a problem mentioning {needle!r}"
    assert any(needle in p for p in problems)


# --- registry -------------------------------------------------------------

_FAKE_ADAPTER = textwrap.dedent('''
    """A throwaway adapter used to prove pkgutil discovery."""

    from climate.weather.providers.base import (
        Attribution,
        AuthRequirement,
        Capability,
        FreshnessStrategy,
        Quota,
        RequestSpec,
        WeatherProvider,
    )


    class FakeProvider(WeatherProvider):
        id = "fake"
        capabilities = frozenset({Capability.FORECAST})
        auth = AuthRequirement(required=False)
        default_interval_seconds = 900
        quota = Quota(unlimited=True, source="test")
        freshness = FreshnessStrategy.INTERVAL
        attribution = Attribution(text="Fake", url="https://example.invalid/terms")

        def build_requests(self, location, settings=None):
            return (
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url="https://example.invalid/fake",
                ),
            )

        def normalize(self, fetch_record):
            return ()
    ''')


@pytest.fixture
def fake_adapter(tmp_path: Path) -> Any:
    """Drop a fake adapter module onto the providers package search path."""
    (tmp_path / "fake_adapter.py").write_text(_FAKE_ADAPTER, encoding="utf-8")
    registry.__path__.append(str(tmp_path))
    registry.clear_cache()
    try:
        yield
    finally:
        registry.__path__.remove(str(tmp_path))
        sys.modules.pop("climate.weather.providers.fake_adapter", None)
        registry.clear_cache()


def test_registry_is_empty_and_quiet_with_no_adapters_installed() -> None:
    registry.clear_cache()
    assert isinstance(registry.provider_ids(), tuple)
    assert registry.describe_providers() == [p.describe() for p in registry.iter_providers()]


def test_registry_discovers_a_new_adapter_module_with_no_edits_here(fake_adapter: Any) -> None:
    assert "fake" in registry.provider_ids()
    provider = registry.get_provider("fake")
    assert provider.attribution.text == "Fake"
    assert isinstance(provider, WeatherProvider)


def test_registry_results_are_cached_until_cleared(fake_adapter: Any) -> None:
    first = registry.iter_providers()
    assert registry.iter_providers() is first


def test_get_provider_raises_a_typed_error_for_an_unknown_id() -> None:
    registry.clear_cache()
    with pytest.raises(registry.UnknownProviderError) as excinfo:
        registry.get_provider("no-such-provider")
    assert "no-such-provider" in str(excinfo.value)


def test_registry_imports_no_third_party_module(fake_adapter: Any) -> None:
    before = set(sys.modules)
    registry.clear_cache()
    registry.iter_providers()
    added = {name.split(".")[0] for name in set(sys.modules) - before}
    foreign = {
        name
        for name in added
        if name not in sys.stdlib_module_names and name != "climate" and not name.startswith("_")
    }
    assert foreign == set()


def test_every_registered_provider_declares_attribution_quota_and_freshness(
    fake_adapter: Any,
) -> None:
    problems: list[str] = []
    for provider in registry.iter_providers():
        problems.extend(f"{provider.id}: {p}" for p in validate_provider(provider))
    assert problems == []


def test_a_registered_provider_without_attribution_fails_the_registry_check(
    tmp_path: Path,
) -> None:
    broken = (
        _FAKE_ADAPTER.replace(
            'attribution = Attribution(text="Fake", url="https://example.invalid/terms")',
            "attribution = None",
        )
        .replace("FakeProvider", "BrokenProvider")
        .replace('id = "fake"', 'id = "broken"')
    )
    (tmp_path / "broken_adapter.py").write_text(broken, encoding="utf-8")
    registry.__path__.append(str(tmp_path))
    registry.clear_cache()
    try:
        problems = [
            f"{p.id}: {msg}" for p in registry.iter_providers() for msg in validate_provider(p)
        ]
        assert any("broken" in p and "attribution" in p for p in problems)
    finally:
        registry.__path__.remove(str(tmp_path))
        sys.modules.pop("climate.weather.providers.broken_adapter", None)
        registry.clear_cache()


def test_base_module_is_never_treated_as_an_adapter() -> None:
    registry.clear_cache()
    assert base.WeatherProvider not in {type(p) for p in registry.iter_providers()}
    assert "base" not in registry.provider_ids()
