"""Tests for the weather scheduler: due logic, isolation, no overlap, jitter.

Everything here runs against fakes — a fake clock, a fake ``sleep``, a fake
``fetch`` and in-process provider adapters — so many simulated ticks cost no
wall-clock time and no socket is ever opened.

The four acceptance criteria of this task are covered verbatim by the tests
in the "acceptance criteria" section at the bottom of this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from climate.cli._errors import EXIT_ENV_ERROR, CliError
from climate.weather import scheduler as sched
from climate.weather.config import Location, ProviderSettings, WeatherConfig
from climate.weather.http import FetchResult
from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Capability,
    FreshnessStrategy,
    Quota,
    RequestSpec,
    WeatherProvider,
)
from climate.weather.store import InMemoryWeatherStore
from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT, fake_secret

START = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


# --- fakes ----------------------------------------------------------------


class FakeClock:
    """A callable clock the tests move by hand."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeSleep:
    """A ``sleep`` that advances the fake clock instead of waiting."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(max(seconds, 0.0))


class FixedRng:
    """A deterministic stand-in for ``random.Random``."""

    def __init__(self, value: float = 3.0) -> None:
        self.value = value
        self.calls: list[tuple[float, float]] = []

    def uniform(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return min(max(self.value, low), high)


class FakeLease:
    """An in-memory lease whose answers the test controls."""

    def __init__(self, *, grants: bool = True, renews: bool = True) -> None:
        self.grants = grants
        self.renews = renews
        self.acquired = 0
        self.renewed = 0
        self.released = 0

    def acquire(self) -> bool:
        self.acquired += 1
        return self.grants

    def renew(self) -> bool:
        self.renewed += 1
        return self.renews

    def release(self) -> None:
        self.released += 1


@dataclass
class FakeFetch:
    """A ``climate.weather.http.fetch``-shaped callable driven by a script.

    ``responses`` maps a provider id to a list of outcomes consumed in order
    (the last one repeats). An outcome is either a :class:`FetchResult` or an
    exception instance, which is raised.
    """

    responses: dict[str, list[Any]] = field(default_factory=dict)
    default: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    clock: FakeClock | None = None

    def __call__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        self.calls.append(
            {
                "url": url,
                "timeout": timeout,
                "headers": dict(headers or {}),
                "if_modified_since": if_modified_since,
            }
        )
        if self.duration and self.clock is not None:
            self.clock.advance(self.duration)
        provider_id = url.split("/")[3].split("?")[0]
        queue = self.responses.get(provider_id)
        if queue:
            outcome = queue[0] if len(queue) == 1 else queue.pop(0)
        else:
            outcome = self.default
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return ok_result(url)
        if callable(outcome):
            return outcome(url)
        return outcome


def ok_result(url: str, *, body: bytes = b'{"ok":true}', status: int = 200) -> FetchResult:
    return FetchResult(
        url=url,
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Expires": "Thu, 17 Sep 2026 12:30:00 GMT",
            "Last-Modified": "Thu, 17 Sep 2026 11:55:00 GMT",
            "ETag": '"abc"',
            "Date": "Thu, 17 Sep 2026 12:00:00 GMT",
            "Cache-Control": "max-age=1800",
            "X-Irrelevant": "ignored",
        },
        body=body,
    )


class _Base(WeatherProvider):
    """Shared metadata so each fake adapter only sets what matters."""

    capabilities = frozenset({Capability.CURRENT_MODEL})
    auth = AuthRequirement()
    default_interval_seconds = 300
    quota = Quota(unlimited=True)
    freshness = FreshnessStrategy.INTERVAL
    attribution = Attribution(text="fake data", url="https://example.test/licence")

    def __init__(self) -> None:
        self.normalize_calls: list[Any] = []
        self.build_calls: list[Any] = []

    def build_requests(self, location, settings=None):
        self.build_calls.append(location)
        return [
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=f"https://example.test/{self.id}?place={location.label}",
                timeout=7.5,
                purpose="current",
            )
        ]

    def normalize(self, fetch_record):
        self.normalize_calls.append(fetch_record)
        return []


class AlphaProvider(_Base):
    id = "alpha"


class BetaProvider(_Base):
    id = "beta"
    default_interval_seconds = 900


def make_config(
    *,
    providers: dict[str, ProviderSettings] | None = None,
    locations: dict[str, Location] | None = None,
) -> WeatherConfig:
    if locations is None:
        locations = {NEUTRAL_LABEL: Location(NEUTRAL_LABEL, *NEUTRAL_POINT)}
    return WeatherConfig(locations=locations, providers=providers or {})


def make_scheduler(
    *,
    providers: list[WeatherProvider],
    config: WeatherConfig | None = None,
    store: InMemoryWeatherStore | None = None,
    fetch: FakeFetch | None = None,
    clock: FakeClock | None = None,
    sleep: FakeSleep | None = None,
    lease: Any = None,
    rng: Any = None,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> sched.Scheduler:
    clock = clock or FakeClock()
    return sched.Scheduler(
        store=store if store is not None else InMemoryWeatherStore(),
        providers=providers,
        config=config if config is not None else make_config(),
        fetch=fetch or FakeFetch(),
        lease=lease if lease is not None else FakeLease(),
        clock=clock,
        sleep=sleep or FakeSleep(clock),
        rng=rng or FixedRng(),
        env=env or {},
        **kwargs,
    )


# --- due logic ------------------------------------------------------------


def test_first_tick_fetches_every_enabled_provider() -> None:
    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[AlphaProvider(), BetaProvider()], store=store)

    result = scheduler.tick()

    assert [outcome.status for outcome in result.outcomes] == ["fetched", "fetched"]
    assert store.count_fetches() == 2
    assert result.fetched == 2


def test_a_provider_is_not_refetched_before_its_own_interval() -> None:
    clock = FakeClock()
    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[AlphaProvider()], store=store, clock=clock)

    scheduler.tick()
    clock.advance(299)
    second = scheduler.tick()

    assert [outcome.status for outcome in second.outcomes] == ["not-due"]
    assert store.count_fetches() == 1

    clock.advance(1)
    third = scheduler.tick()
    assert [outcome.status for outcome in third.outcomes] == ["fetched"]
    assert store.count_fetches() == 2


def test_a_configured_interval_overrides_the_adapter_default() -> None:
    clock = FakeClock()
    store = InMemoryWeatherStore()
    config = make_config(
        providers={"alpha": ProviderSettings(provider_id="alpha", interval_seconds=1800)}
    )
    scheduler = make_scheduler(providers=[AlphaProvider()], store=store, config=config, clock=clock)

    scheduler.tick()
    clock.advance(600)
    assert scheduler.tick().outcomes[0].status == "not-due"
    clock.advance(1200)
    assert scheduler.tick().outcomes[0].status == "fetched"


def test_is_due_can_veto_even_when_the_interval_elapsed() -> None:
    class NeverDue(AlphaProvider):
        id = "never"

        def is_due(self, now, last_fetch, settings=None):
            return last_fetch is None

    clock = FakeClock()
    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[NeverDue()], store=store, clock=clock)

    scheduler.tick()
    clock.advance(100_000)
    second = scheduler.tick()

    assert second.outcomes[0].status == "not-due"
    assert store.count_fetches() == 1


def test_a_disabled_provider_is_reported_with_its_reason_and_never_fetched() -> None:
    fetch = FakeFetch()
    config = make_config(providers={"alpha": ProviderSettings(provider_id="alpha", enabled=False)})
    scheduler = make_scheduler(
        providers=[AlphaProvider(), BetaProvider()], config=config, fetch=fetch
    )

    result = scheduler.tick()
    by_provider = {outcome.provider: outcome for outcome in result.outcomes}

    assert by_provider["alpha"].status == "disabled"
    assert by_provider["alpha"].reason == "disabled in configuration"
    assert by_provider["beta"].status == "fetched"
    assert [call["url"] for call in fetch.calls] == ["https://example.test/beta?place=home"]


def test_a_missing_credential_disables_only_that_provider() -> None:
    class KeyedProvider(_Base):
        id = "keyed"
        auth = AuthRequirement(required=True, env_var="CLIMATE_FAKE_KEY")

    scheduler = make_scheduler(providers=[KeyedProvider(), AlphaProvider()], env={})
    result = scheduler.tick()
    by_provider = {outcome.provider: outcome for outcome in result.outcomes}

    assert by_provider["keyed"].status == "disabled"
    assert "CLIMATE_FAKE_KEY" in (by_provider["keyed"].reason or "")
    assert by_provider["alpha"].status == "fetched"


def test_every_configured_location_is_fetched_independently() -> None:
    store = InMemoryWeatherStore()
    locations = {
        "home": Location("home", *NEUTRAL_POINT),
        "away": Location("away", *NEUTRAL_POINT),
    }
    scheduler = make_scheduler(
        providers=[AlphaProvider()], store=store, config=make_config(locations=locations)
    )

    result = scheduler.tick()

    assert sorted(outcome.location for outcome in result.outcomes) == ["away", "home"]
    assert store.count_fetches(location="home") == 1
    assert store.count_fetches(location="away") == 1


def test_an_adapter_that_wants_no_request_is_reported_not_fetched() -> None:
    class Quiet(AlphaProvider):
        id = "quiet"

        def build_requests(self, location, settings=None):
            return []

    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[Quiet()], store=store)

    assert scheduler.tick().outcomes[0].status == "no-request"
    assert store.count_fetches() == 0


# --- storage of the raw bytes --------------------------------------------


def test_the_raw_record_is_stored_before_normalize_runs() -> None:
    store = InMemoryWeatherStore()
    seen: list[int] = []

    class Watcher(AlphaProvider):
        id = "watcher"

        def normalize(self, fetch_record):
            seen.append(store.count_fetches())
            return []

    make_scheduler(providers=[Watcher()], store=store).tick()

    assert seen == [1]


def test_the_stored_record_keeps_the_exact_bytes_and_cache_headers() -> None:
    store = InMemoryWeatherStore()
    body = b'{"temperature": 21.5}'
    fetch = FakeFetch(default=lambda url: ok_result(url, body=body))
    make_scheduler(providers=[AlphaProvider()], store=store, fetch=fetch).tick()

    record = store.latest_fetch()
    assert record is not None
    assert record.body == body
    assert record.content_type == "application/json"
    assert record.charset == "utf-8"
    assert set(record.cache_headers) == {
        "Expires",
        "Last-Modified",
        "ETag",
        "Date",
        "Cache-Control",
    }
    assert record.endpoint == "https://example.test/alpha?place=home"
    assert record.location == "home"
    assert record.status == 200


def test_a_secret_in_the_request_url_is_redacted_in_the_stored_endpoint() -> None:
    class Keyed(AlphaProvider):
        id = "secretive"

        def build_requests(self, location, settings=None):
            return [
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url="https://example.test/secretive?appid=s3cret&units=metric",
                )
            ]

    store = InMemoryWeatherStore()
    make_scheduler(providers=[Keyed()], store=store).tick()

    record = store.latest_fetch()
    assert record is not None
    assert "s3cret" not in record.endpoint
    assert "units=metric" in record.endpoint


def test_identical_bytes_are_stored_again_with_no_dedup() -> None:
    clock = FakeClock()
    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[AlphaProvider()], store=store, clock=clock)

    for _ in range(4):
        scheduler.tick()
        clock.advance(300)

    records = list(store.iter_fetches())
    assert len(records) == 4
    assert len({record.sha256 for record in records}) == 1


def test_readings_are_saved_against_the_record_they_came_from() -> None:
    from climate.weather.store import Measurement, Reading

    store = InMemoryWeatherStore()

    class Normalizing(AlphaProvider):
        id = "normalizing"

        def normalize(self, fetch_record):
            return [
                Reading(
                    provider=self.id,
                    source="model",
                    location=fetch_record.location,
                    observed_at=fetch_record.requested_at,
                    requested_at=fetch_record.requested_at,
                    kind="model",
                    values={"temperature": Measurement(21.5, "degC")},
                )
            ]

    result = make_scheduler(providers=[Normalizing()], store=store).tick()

    outcome = result.outcomes[0]
    assert outcome.reading_count == 1
    assert store.readings_for_fetch(outcome.fetch_id)[0].provider == "normalizing"
    assert result.readings == 1


def test_a_normalize_failure_leaves_the_raw_record_intact_and_is_recorded() -> None:
    store = InMemoryWeatherStore()

    class Broken(AlphaProvider):
        id = "broken"

        def normalize(self, fetch_record):
            raise ValueError("parser bug")

    result = make_scheduler(providers=[Broken()], store=store).tick()
    outcome = result.outcomes[0]

    assert outcome.status == "fetched"
    assert outcome.normalize_error is not None
    assert "parser bug" in outcome.normalize_error
    assert result.normalize_failures == 1
    record = store.latest_fetch()
    assert record is not None
    assert record.body == b'{"ok":true}'
    assert store.readings_for_fetch(outcome.fetch_id) == []


# --- failure handling -----------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "kind", "status"),
    [
        (FetchResult(url="u", error="<urlopen error timed out>"), "timeout", None),
        (FetchResult(url="u", error="<urlopen error [Errno -2] name lookup>"), "transport", None),
        (FetchResult(url="u", status=429, error="HTTP Error 429"), "rate_limited", 429),
        (FetchResult(url="u", status=503, error="HTTP Error 503"), "http_error", 503),
        (TimeoutError("timed out"), "timeout", None),
        (OSError("boom"), "transport", None),
    ],
)
def test_every_failure_shape_is_stored_as_a_fetch_record(
    outcome: Any, kind: str, status: int | None
) -> None:
    store = InMemoryWeatherStore()
    fetch = FakeFetch(default=outcome)
    result = make_scheduler(providers=[AlphaProvider()], store=store, fetch=fetch).tick()

    record = store.latest_fetch()
    assert record is not None
    assert record.error is not None
    assert record.error.kind == kind
    assert record.status == status
    assert store.count_fetches(errors_only=True) == 1
    assert result.outcomes[0].error_kind == kind
    assert result.errors == 1


def test_a_304_is_a_normal_record_with_no_readings() -> None:
    store = InMemoryWeatherStore()
    fetch = FakeFetch(default=FetchResult(url="https://example.test/alpha", status=304))
    provider = AlphaProvider()
    result = make_scheduler(providers=[provider], store=store, fetch=fetch).tick()

    record = store.latest_fetch()
    assert record is not None
    assert record.status == 304
    assert record.error is None
    assert record.is_error is False
    assert provider.normalize_calls == []
    assert result.outcomes[0].reading_count == 0
    assert result.errors == 0


def test_a_provider_raising_in_build_requests_never_escapes_the_tick() -> None:
    class Exploding(AlphaProvider):
        id = "exploding"

        def build_requests(self, location, settings=None):
            raise RuntimeError("adapter bug")

    result = make_scheduler(providers=[Exploding(), AlphaProvider()]).tick()
    by_provider = {outcome.provider: outcome for outcome in result.outcomes}

    assert by_provider["exploding"].status == "provider-error"
    assert "adapter bug" in (by_provider["exploding"].error_message or "")
    assert by_provider["alpha"].status == "fetched"


def test_a_failed_fetch_still_gates_the_next_tick() -> None:
    clock = FakeClock()
    store = InMemoryWeatherStore()
    fetch = FakeFetch(default=TimeoutError("timed out"))
    scheduler = make_scheduler(providers=[AlphaProvider()], store=store, fetch=fetch, clock=clock)

    scheduler.tick()
    clock.advance(60)
    assert scheduler.tick().outcomes[0].status == "not-due"
    assert len(fetch.calls) == 1


# --- conditional requests -------------------------------------------------


def test_conditional_headers_helper_is_used_when_the_adapter_exposes_one() -> None:
    class Conditional(AlphaProvider):
        id = "conditional"
        capabilities = frozenset({Capability.CONDITIONAL_GET})
        freshness = FreshnessStrategy.INTERVAL

        def conditional_headers(self, last_fetch):
            if last_fetch is None:
                return {}
            return {"If-None-Match": last_fetch.cache_headers.get("ETag", "")}

    clock = FakeClock()
    fetch = FakeFetch()
    scheduler = make_scheduler(providers=[Conditional()], fetch=fetch, clock=clock)

    scheduler.tick()
    assert fetch.calls[0]["headers"].get("If-None-Match") is None
    clock.advance(300)
    scheduler.tick()
    assert fetch.calls[1]["headers"]["If-None-Match"] == '"abc"'


def test_last_modified_is_replayed_when_the_provider_declares_conditional_get() -> None:
    class Conditional(AlphaProvider):
        id = "cond-default"
        capabilities = frozenset({Capability.CONDITIONAL_GET})

    clock = FakeClock()
    fetch = FakeFetch()
    scheduler = make_scheduler(providers=[Conditional()], fetch=fetch, clock=clock)

    scheduler.tick()
    clock.advance(300)
    scheduler.tick()

    assert fetch.calls[0]["if_modified_since"] is None
    assert fetch.calls[1]["if_modified_since"] == "Thu, 17 Sep 2026 11:55:00 GMT"


def test_a_provider_without_conditional_get_never_sends_if_modified_since() -> None:
    clock = FakeClock()
    fetch = FakeFetch()
    scheduler = make_scheduler(providers=[AlphaProvider()], fetch=fetch, clock=clock)

    scheduler.tick()
    clock.advance(300)
    scheduler.tick()

    assert [call["if_modified_since"] for call in fetch.calls] == [None, None]


def test_the_requests_own_timeout_is_passed_to_fetch() -> None:
    fetch = FakeFetch()
    make_scheduler(providers=[AlphaProvider()], fetch=fetch).tick()
    assert fetch.calls[0]["timeout"] == 7.5


# --- the lease ------------------------------------------------------------


def test_the_lease_is_renewed_rather_than_reacquired_on_later_ticks() -> None:
    lease = FakeLease()
    clock = FakeClock()
    scheduler = make_scheduler(providers=[AlphaProvider()], lease=lease, clock=clock)

    scheduler.tick()
    clock.advance(300)
    scheduler.tick()

    assert lease.acquired == 1
    assert lease.renewed == 1


def test_losing_the_lease_mid_run_stops_fetching() -> None:
    lease = FakeLease(renews=False)
    clock = FakeClock()
    fetch = FakeFetch()
    scheduler = make_scheduler(providers=[AlphaProvider()], lease=lease, fetch=fetch, clock=clock)

    scheduler.tick()
    clock.advance(300)
    second = scheduler.tick()

    assert second.lease_held is False
    assert second.outcomes == ()
    assert len(fetch.calls) == 1


def test_the_null_lease_always_grants() -> None:
    lease = sched.NullLease()
    assert lease.acquire() is True
    assert lease.renew() is True
    lease.release()
    assert lease.acquire() is True


def test_close_releases_the_lease() -> None:
    lease = FakeLease()
    scheduler = make_scheduler(providers=[AlphaProvider()], lease=lease)
    scheduler.tick()
    scheduler.close()
    assert lease.released == 1


# --- run() loop -----------------------------------------------------------


def test_run_waits_to_the_next_aligned_boundary_plus_jitter() -> None:
    clock = FakeClock(datetime(2026, 9, 17, 12, 0, 7, tzinfo=UTC))
    sleep = FakeSleep(clock)
    rng = FixedRng(4.0)
    scheduler = make_scheduler(providers=[AlphaProvider()], clock=clock, sleep=sleep, rng=rng)

    results = scheduler.run(max_ticks=3)

    assert len(results) == 3
    # 12:00:07 -> next boundary 12:05:00 is 293 s away, plus 4 s of jitter.
    assert sleep.delays == [pytest.approx(297.0), pytest.approx(300.0)]
    assert rng.calls == [(0.0, 10.0), (0.0, 10.0)]


def test_run_stops_cleanly_on_the_stop_event() -> None:
    clock = FakeClock()
    store = InMemoryWeatherStore()

    class Stop:
        """Set once two ticks' worth of records exist."""

        def is_set(self) -> bool:
            return store.count_fetches() >= 2

    lease = FakeLease()
    scheduler = make_scheduler(
        providers=[AlphaProvider()],
        store=store,
        lease=lease,
        clock=clock,
        sleep=FakeSleep(clock),
    )

    results = scheduler.run(stop=Stop())

    assert len(results) == 2
    assert store.count_fetches() == 2
    assert lease.released == 1


def test_run_releases_the_lease_when_it_finishes() -> None:
    lease = FakeLease()
    clock = FakeClock()
    scheduler = make_scheduler(
        providers=[AlphaProvider()], lease=lease, clock=clock, sleep=FakeSleep(clock)
    )
    scheduler.run(max_ticks=2)
    assert lease.released == 1


def test_validate_rejects_an_interval_below_the_provider_minimum() -> None:
    config = make_config(
        providers={"beta": ProviderSettings(provider_id="beta", interval_seconds=300)}
    )
    providers = [BetaProvider()]
    with pytest.raises(CliError) as excinfo:
        sched.validate(config, providers)
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "beta" in excinfo.value.message
    assert excinfo.value.remediation


def test_validate_accepts_an_interval_at_or_above_the_minimum() -> None:
    config = make_config(
        providers={"beta": ProviderSettings(provider_id="beta", interval_seconds=900)}
    )
    sched.validate(config, [BetaProvider()])


def test_validate_rejects_demand_beyond_the_declared_quota() -> None:
    class Budgeted(_Base):
        id = "budgeted"
        default_interval_seconds = 300
        quota = Quota(calls_per_day=100)

    locations = {"home": Location("home", *NEUTRAL_POINT), "away": Location("away", *NEUTRAL_POINT)}
    config = make_config(locations=locations)
    providers = [Budgeted()]
    with pytest.raises(CliError) as excinfo:
        sched.validate(config, providers)
    assert "budgeted" in excinfo.value.message
    assert excinfo.value.code == EXIT_ENV_ERROR


def test_validate_ignores_disabled_providers() -> None:
    class Budgeted(_Base):
        id = "budgeted"
        quota = Quota(calls_per_day=1)

    config = make_config(
        providers={"budgeted": ProviderSettings(provider_id="budgeted", enabled=False)}
    )
    sched.validate(config, [Budgeted()])


def test_validate_multiplies_the_quota_demand_by_the_adapter_request_cost() -> None:
    """One location is not always one request.

    An adapter that fans out (a call per station) declares the real cost
    through ``requests_per_tick``; validation has to count requests, not
    locations, or a plan that blows the daily quota passes startup.
    """

    class Fanout(_Base):
        id = "fanout"
        default_interval_seconds = 300
        quota = Quota(calls_per_day=300)
        cost = 1

        def requests_per_tick(self, location, settings=None):
            return self.cost

    config = make_config()  # one location; 86400/300 = 288 ticks a day

    cheap = Fanout()
    sched.validate(config, [cheap])  # 288 calls/day at one request per tick

    expensive = Fanout()
    expensive.cost = 2
    providers = [expensive]
    with pytest.raises(CliError) as excinfo:
        sched.validate(config, providers)
    assert "fanout" in excinfo.value.message
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "576" in excinfo.value.message


def test_the_default_request_cost_leaves_quota_validation_unchanged() -> None:
    class Budgeted(_Base):
        id = "budgeted"
        default_interval_seconds = 300
        quota = Quota(calls_per_day=290)

    # The default cost is one request per location: 288 calls/day, inside 290.
    sched.validate(make_config(), [Budgeted()])


# --- the credential environment -------------------------------------------


class KeyedProvider(_Base):
    """An adapter that needs a credential and records the env it was handed."""

    id = "keyed"
    auth = AuthRequirement(required=True, env_var="CLIMATE_KEYED_TOKEN")

    def __init__(self) -> None:
        super().__init__()
        self.build_envs: list[Any] = []

    def build_requests(self, location, settings=None, *, env=None):
        self.build_envs.append(env)
        token = self.credential(env)
        return [
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=f"https://example.test/{self.id}?place={location.label}&appid={token}",
            )
        ]


def test_build_requests_is_handed_the_env_availability_was_resolved_against() -> None:
    """Regression: 'enabled' and 'has a key' must read the same mapping.

    With the credential injected rather than exported, an adapter reading
    ``os.environ`` inside ``build_requests`` was declared enabled and then
    built a keyless request (or none at all).
    """
    provider = KeyedProvider()
    token = fake_secret("keyed-token")
    env = {provider.auth.env_var: token}
    fetch = FakeFetch()
    scheduler = make_scheduler(providers=[provider], fetch=fetch, env=env)

    assert provider.availability(None, env).enabled is True

    result = scheduler.tick()

    assert [outcome.status for outcome in result.outcomes] == ["fetched"]
    assert provider.build_envs == [env]
    assert token in fetch.calls[0]["url"]


def test_an_adapter_without_the_env_keyword_is_still_called_the_old_way() -> None:
    """The keyword is optional: the six adapters migrate one at a time."""
    provider = AlphaProvider()  # build_requests(self, location, settings=None)
    scheduler = make_scheduler(providers=[provider], env={"UNUSED": "value"})

    result = scheduler.tick()

    assert [outcome.status for outcome in result.outcomes] == ["fetched"]
    assert provider.build_calls


# --- acceptance criteria (verbatim) ---------------------------------------


def test_ac1_each_provider_is_fetched_on_its_own_interval_and_is_due_only() -> None:
    """on each base tick every enabled provider is fetched only when its own
    configured interval has elapsed AND its is_due() allows it; one provider's
    settings never alter another's schedule."""
    clock = FakeClock()
    store = InMemoryWeatherStore()
    fetch = FakeFetch()
    config = make_config(
        providers={
            "alpha": ProviderSettings(provider_id="alpha", interval_seconds=300),
            "beta": ProviderSettings(provider_id="beta", interval_seconds=900),
        }
    )
    scheduler = make_scheduler(
        providers=[AlphaProvider(), BetaProvider()],
        store=store,
        config=config,
        fetch=fetch,
        clock=clock,
    )

    # Twelve base ticks of 300 s = one hour.
    statuses: list[dict[str, str]] = []
    for _ in range(12):
        result = scheduler.tick()
        statuses.append({o.provider: o.status for o in result.outcomes})
        clock.advance(300)

    alpha = [row["alpha"] for row in statuses]
    beta = [row["beta"] for row in statuses]
    assert alpha == ["fetched"] * 12
    assert beta == ["fetched", "not-due", "not-due"] * 4
    assert store.count_fetches(provider="alpha") == 12
    assert store.count_fetches(provider="beta") == 4

    # is_due() can still veto a provider whose interval has elapsed, and that
    # veto changes nothing for the other provider.
    class Vetoing(BetaProvider):
        id = "beta"

        def is_due(self, now, last_fetch, settings=None):
            return False

    store2 = InMemoryWeatherStore()
    clock2 = FakeClock()
    scheduler2 = make_scheduler(
        providers=[AlphaProvider(), Vetoing()],
        store=store2,
        config=config,
        clock=clock2,
    )
    for _ in range(6):
        scheduler2.tick()
        clock2.advance(300)
    assert store2.count_fetches(provider="alpha") == 6
    assert store2.count_fetches(provider="beta") == 0


def test_ac2_one_bad_provider_never_blocks_the_others_and_is_stored() -> None:
    """one provider raising, timing out or returning 429 still leaves every
    other provider fetched and stored in the same tick, and the failure is
    stored as a fetch record."""

    class Raiser(_Base):
        id = "raiser"

        def build_requests(self, location, settings=None):
            raise RuntimeError("adapter exploded")

    class Timeouter(_Base):
        id = "timeouter"

    class Limited(_Base):
        id = "limited"

    class Healthy(_Base):
        id = "healthy"

    store = InMemoryWeatherStore()
    fetch = FakeFetch(
        responses={
            "timeouter": [TimeoutError("timed out")],
            "limited": [FetchResult(url="https://example.test/limited", status=429)],
        }
    )
    scheduler = make_scheduler(
        providers=[Raiser(), Timeouter(), Limited(), Healthy()], store=store, fetch=fetch
    )

    result = scheduler.tick()
    by_provider = {outcome.provider: outcome for outcome in result.outcomes}

    assert by_provider["healthy"].status == "fetched"
    assert by_provider["healthy"].error_kind is None
    assert store.count_fetches(provider="healthy") == 1

    assert by_provider["raiser"].status == "provider-error"
    assert by_provider["timeouter"].error_kind == "timeout"
    assert by_provider["limited"].error_kind == "rate_limited"
    assert store.count_fetches(provider="timeouter", errors_only=True) == 1
    assert store.count_fetches(provider="limited", errors_only=True) == 1
    assert store.latest_fetch(provider="limited").status == 429  # type: ignore[union-attr]


def test_ac3_an_overrunning_tick_is_never_overlapped_and_nothing_is_backfilled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """a tick that overruns is never overlapped by the next; tick start carries
    a small random jitter; missed ticks are not backfilled."""
    clock = FakeClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    sleep = FakeSleep(clock)
    store = InMemoryWeatherStore()
    # Each fetch burns 700 s of wall clock: every tick overruns the 300 s base.
    fetch = FakeFetch(duration=700.0, clock=clock)
    rng = FixedRng(2.0)
    scheduler = make_scheduler(
        providers=[AlphaProvider()],
        store=store,
        fetch=fetch,
        clock=clock,
        sleep=sleep,
        rng=rng,
    )

    with caplog.at_level(logging.WARNING, logger="climate.weather.scheduler"):
        results = scheduler.run(max_ticks=3)

    starts = [result.started_at for result in results]
    # No overlap: each tick begins only after the previous one finished.
    assert starts[1] >= starts[0] + timedelta(seconds=700)
    assert starts[2] >= starts[1] + timedelta(seconds=700)
    # Jitter is applied to every wait, and each wait lands on the next
    # boundary after the overrun rather than firing immediately.
    assert sleep.delays == [pytest.approx(202.0), pytest.approx(200.0)]
    assert rng.calls == [(0.0, 10.0), (0.0, 10.0)]
    # No backfill: three ticks ran over ~30 min, not the ~6 base ticks that
    # the wall clock passed; the skipped ones are reported and logged.
    assert len(results) == 3
    assert store.count_fetches() == 3
    assert results[1].missed_ticks == 2
    assert any("missed" in record.message for record in caplog.records)


def test_ac4_without_the_lease_the_scheduler_issues_zero_requests() -> None:
    """without the lease the scheduler issues zero provider requests."""
    store = InMemoryWeatherStore()
    fetch = FakeFetch()
    lease = FakeLease(grants=False)
    clock = FakeClock()
    provider = AlphaProvider()
    scheduler = make_scheduler(
        providers=[provider],
        store=store,
        fetch=fetch,
        lease=lease,
        clock=clock,
        sleep=FakeSleep(clock),
    )

    results = scheduler.run(max_ticks=5)

    assert fetch.calls == []
    assert provider.build_calls == []
    assert store.count_fetches() == 0
    assert all(result.lease_held is False for result in results)
    assert all(result.outcomes == () for result in results)
    assert results[0].reason == sched.NO_LEASE_REASON


# --- structured result, for the tracker's log -----------------------------


def test_the_tick_result_carries_one_log_line_per_outcome() -> None:
    class Broken(AlphaProvider):
        id = "broken"

        def normalize(self, fetch_record):
            raise ValueError("parser bug")

    fetch = FakeFetch(
        responses={"beta": [FetchResult(url="https://example.test/beta", status=429)]}
    )
    result = make_scheduler(providers=[Broken(), BetaProvider()], fetch=fetch).tick()

    lines = result.log_lines()
    assert len(lines) == 2
    assert "broken/home fetched" in lines[0]
    assert "normalize=failed" in lines[0]
    assert "error=rate_limited" in lines[1]
    assert "http=429" in lines[1]
    assert len(result.fetch_ids) == 2


def test_a_not_due_outcome_explains_itself() -> None:
    clock = FakeClock()
    scheduler = make_scheduler(providers=[AlphaProvider()], clock=clock)
    scheduler.tick()
    clock.advance(30)
    line = scheduler.tick().log_lines()[0]
    assert "not-due" in line
    assert "interval" in line


# --- loop mechanics -------------------------------------------------------


def test_run_can_align_before_the_first_tick() -> None:
    clock = FakeClock(datetime(2026, 9, 17, 12, 0, 7, tzinfo=UTC))
    sleep = FakeSleep(clock)
    scheduler = make_scheduler(
        providers=[AlphaProvider()], clock=clock, sleep=sleep, rng=FixedRng(1.0)
    )

    results = scheduler.run(max_ticks=1, start_immediately=False)

    assert sleep.delays == [pytest.approx(294.0)]
    assert results[0].started_at == datetime(2026, 9, 17, 12, 5, 1, tzinfo=UTC)


def test_an_interruptible_stop_event_is_used_for_the_wait() -> None:
    clock = FakeClock()

    class WaitingStop:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return len(self.waits) >= 1

        def wait(self, seconds: float) -> None:
            self.waits.append(seconds)
            clock.advance(seconds)

    sleep = FakeSleep(clock)
    stop = WaitingStop()
    scheduler = make_scheduler(providers=[AlphaProvider()], clock=clock, sleep=sleep)

    results = scheduler.run(stop=stop)

    assert len(stop.waits) == 1
    assert sleep.delays == []
    assert len(results) == 1


def test_jitter_can_be_switched_off() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    rng = FixedRng(5.0)
    scheduler = make_scheduler(
        providers=[AlphaProvider()], clock=clock, sleep=sleep, rng=rng, jitter_seconds=0
    )

    scheduler.run(max_ticks=2)

    assert rng.calls == []
    assert sleep.delays == [pytest.approx(300.0)]


def test_the_base_tick_must_be_positive() -> None:
    providers = [AlphaProvider()]
    with pytest.raises(ValueError):
        make_scheduler(providers=providers, base_tick_seconds=0)


def test_the_jitter_must_not_be_negative() -> None:
    providers = [AlphaProvider()]
    with pytest.raises(ValueError):
        make_scheduler(providers=providers, jitter_seconds=-1)


def test_a_broken_conditional_headers_helper_falls_back_to_an_unconditional_fetch() -> None:
    class Rude(AlphaProvider):
        id = "rude"

        def conditional_headers(self, last_fetch):
            raise RuntimeError("helper bug")

    fetch = FakeFetch()
    result = make_scheduler(providers=[Rude()], fetch=fetch).tick()

    assert result.outcomes[0].status == "fetched"
    assert fetch.calls[0]["headers"] == {}


def test_a_lease_that_raises_is_treated_as_not_held() -> None:
    class AngryLease:
        def acquire(self) -> bool:
            raise RuntimeError("mongo is down")

        def renew(self) -> bool:
            raise RuntimeError("mongo is down")

        def release(self) -> None:
            raise RuntimeError("mongo is down")

    store = InMemoryWeatherStore()
    scheduler = make_scheduler(providers=[AlphaProvider()], store=store, lease=AngryLease())

    result = scheduler.tick()
    scheduler.close()

    assert result.lease_held is False
    assert store.count_fetches() == 0
