"""The tracker's scheduler: per-provider due logic, isolation, no overlap.

This module is pure orchestration. It performs no I/O of its own and knows
nothing about MongoDB, HTTP or the CLI: every collaborator is injected, so
the whole loop can be driven through hundreds of simulated ticks with fakes
in microseconds and without a socket.

Collaborators (all constructor arguments):

``store``
    A :class:`climate.weather.store.WeatherStore` — the system of record.
``providers``
    A sequence of :class:`~climate.weather.providers.base.WeatherProvider`
    adapters. All of them are sampled on every base tick; there is no
    rotation (spec decision superseding issue 5's single global slot).
``config``
    A :class:`climate.weather.config.WeatherConfig`: the labelled locations
    and the per-provider settings.
``fetch``
    A callable shaped like :func:`climate.weather.http.fetch`.
``lease``
    A :class:`Lease`: exactly one tracker fetches at a time (spec ``c41``).
``clock`` / ``sleep`` / ``rng``
    Time, waiting and jitter, injected for tests.
``env``
    The environment mapping consulted for credential availability.

What one :meth:`Scheduler.tick` does, in order:

1. Hold the lease — acquire it the first time, renew it afterwards. Without
   it the tick issues **zero** provider requests and returns immediately.
2. For every provider and every configured location: skip it when it is
   unavailable (disabled in configuration, or a missing credential — which
   disables only that provider); otherwise ask whether its **own** interval
   has elapsed *and* :meth:`WeatherProvider.is_due` allows a fetch. One
   provider's settings never influence another's schedule.
3. Perform each request the adapter describes and **immediately** store the
   verbatim bytes as a :class:`~climate.weather.store.FetchRecord` — before
   any parsing runs, so a parser bug can never lose a provider field
   (spec ``h2``). Timeouts, transport failures, 429s and 5xx are stored as
   fetch records too, so a gap in the data is distinguishable from a gap in
   collection (spec ``h1``). A ``304`` is an ordinary record with an empty
   body and no readings.
4. Normalize inside its own ``try``/``except``. A normalize failure leaves
   the raw record untouched and is reported on the tick result and logged;
   it never loses the fetch and never fails the tick.

Nothing a provider does — raising, hanging, rate-limiting — escapes the tick
or affects any other provider.

:meth:`Scheduler.run` loops at a base tick (300 s by default) aligned to the
wall clock plus a small random jitter (0–10 s). A tick that overruns is never
overlapped: the next wait targets the next boundary *after* the overrun, the
boundaries that went by are reported as ``missed_ticks`` and logged, and they
are never backfilled — a missed tick is an honest gap (spec ``h24``).
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from climate.cli._errors import EXIT_ENV_ERROR, CliError
from climate.weather import http as weather_http
from climate.weather.config import Location, ProviderSettings, WeatherConfig
from climate.weather.providers.base import Capability, WeatherProvider
from climate.weather.store import FetchError, FetchRecord, WeatherStore, redact_url

__all__ = [
    "CACHE_HEADER_NAMES",
    "DEFAULT_BASE_TICK_SECONDS",
    "DEFAULT_JITTER_SECONDS",
    "ERROR_HTTP",
    "ERROR_RATE_LIMITED",
    "ERROR_TIMEOUT",
    "ERROR_TRANSPORT",
    "NO_LEASE_REASON",
    "STATUS_DISABLED",
    "STATUS_FETCHED",
    "STATUS_NOT_DUE",
    "STATUS_NO_REQUEST",
    "STATUS_PROVIDER_ERROR",
    "FetchOutcome",
    "Lease",
    "NullLease",
    "Scheduler",
    "TickResult",
    "validate",
]

LOGGER = logging.getLogger(__name__)

#: The scheduler's base resolution. Providers are gated by their own
#: intervals on top of this (spec ``c21``).
DEFAULT_BASE_TICK_SECONDS = 300

#: Upper bound of the random jitter added to each wait (spec ``c41``): enough
#: to de-synchronise this tracker from everyone else's five-minute tick.
DEFAULT_JITTER_SECONDS = 10.0

#: Response headers kept on the fetch record so the next tick's conditional
#: request and freshness decision can be made from stored data alone.
CACHE_HEADER_NAMES: tuple[str, ...] = (
    "Expires",
    "Last-Modified",
    "ETag",
    "Date",
    "Cache-Control",
    "Age",
)

# --- FetchError.kind tokens used by this module ---------------------------
ERROR_TIMEOUT = "timeout"
ERROR_TRANSPORT = "transport"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_HTTP = "http_error"

# --- FetchOutcome.status tokens -------------------------------------------
STATUS_FETCHED = "fetched"
STATUS_NOT_DUE = "not-due"
STATUS_DISABLED = "disabled"
STATUS_NO_REQUEST = "no-request"
STATUS_PROVIDER_ERROR = "provider-error"

#: Reported on a tick that did nothing because the lease was not held.
NO_LEASE_REASON = "another tracker holds the fetch lease"

_TIMEOUT_MARKERS = ("timed out", "timeout")


# --- collaborator protocols -----------------------------------------------


@runtime_checkable
class Lease(Protocol):
    """The single-fetcher lease (spec ``c41``).

    The Mongo-backed implementation lives elsewhere; anything structurally
    matching this works, including :class:`NullLease` and test fakes.

    * :meth:`acquire` — take the lease. ``True`` when this process now holds
      it, ``False`` when someone else does. Must not raise for the ordinary
      "someone else holds it" case.
    * :meth:`renew` — extend a lease already held. ``False`` means it was
      lost (expired, or stolen after a stall); the scheduler then stops
      fetching until a later :meth:`acquire` succeeds.
    * :meth:`release` — give it up. Idempotent, and safe to call when the
      lease is not held.
    """

    def acquire(self) -> bool:
        """Take the lease; ``True`` when this process now holds it."""

    def renew(self) -> bool:
        """Extend a held lease; ``False`` when it has been lost."""

    def release(self) -> None:
        """Give up the lease. Idempotent."""


class NullLease:
    """A :class:`Lease` with no coordination: it always grants.

    Used where exactly one tracker is guaranteed by construction (a
    single-process dev run, most tests). It provides no protection against a
    second tracker — that is what the Mongo lease is for.
    """

    def acquire(self) -> bool:
        return True

    def renew(self) -> bool:
        return True

    def release(self) -> None:
        return None


@runtime_checkable
class Rng(Protocol):
    """Just enough of :class:`random.Random` for the tick jitter."""

    def uniform(self, low: float, high: float) -> float: ...


# --- results ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What happened for one provider/location pair on one tick.

    :param status: one of :data:`STATUS_FETCHED`, :data:`STATUS_NOT_DUE`,
        :data:`STATUS_DISABLED`, :data:`STATUS_NO_REQUEST` or
        :data:`STATUS_PROVIDER_ERROR`.
    :param reason: why it was skipped or disabled.
    :param fetch_id: the stored record's id when a fetch was made.
    :param http_status: the HTTP status, ``None`` when no response arrived.
    :param reading_count: how many normalized readings were stored.
    :param error_kind: the :class:`~climate.weather.store.FetchError` kind
        when the fetch failed.
    :param normalize_error: the normalization failure, if any. The raw record
        is stored regardless.
    """

    provider: str
    location: str
    status: str
    reason: str | None = None
    fetch_id: str = ""
    http_status: int | None = None
    reading_count: int = 0
    error_kind: str | None = None
    error_message: str = ""
    normalize_error: str | None = None
    purpose: str = ""

    def log_line(self) -> str:
        """One compact line per fetch, for the tracker's log."""
        parts = [f"{self.provider}/{self.location}", self.status]
        if self.purpose and self.purpose != "fetch":
            parts.append(self.purpose)
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.error_kind:
            parts.append(f"error={self.error_kind}")
        if self.normalize_error:
            parts.append("normalize=failed")
        if self.status == STATUS_FETCHED:
            parts.append(f"readings={self.reading_count}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class TickResult:
    """The structured record of one tick, for logging and for tests."""

    started_at: datetime
    lease_held: bool
    outcomes: tuple[FetchOutcome, ...] = ()
    reason: str | None = None
    missed_ticks: int = 0

    @property
    def fetched(self) -> int:
        """How many requests were made and stored on this tick."""
        return sum(1 for outcome in self.outcomes if outcome.status == STATUS_FETCHED)

    @property
    def errors(self) -> int:
        """Stored fetches that carry a failure (timeout, transport, 4xx/5xx)."""
        return sum(1 for outcome in self.outcomes if outcome.error_kind)

    @property
    def normalize_failures(self) -> int:
        """Fetches whose raw bytes were stored but could not be parsed."""
        return sum(1 for outcome in self.outcomes if outcome.normalize_error)

    @property
    def readings(self) -> int:
        """Normalized readings stored on this tick."""
        return sum(outcome.reading_count for outcome in self.outcomes)

    @property
    def fetch_ids(self) -> tuple[str, ...]:
        """The ids of the raw records stored on this tick."""
        return tuple(outcome.fetch_id for outcome in self.outcomes if outcome.fetch_id)

    def log_lines(self) -> list[str]:
        """One line per provider/location, in outcome order."""
        return [outcome.log_line() for outcome in self.outcomes]


# --- startup validation ----------------------------------------------------


def validate(config: WeatherConfig, providers: Iterable[WeatherProvider]) -> None:
    """Check the configuration against what each adapter declares.

    Raises :class:`~climate.cli._errors.CliError` (exit 2, naming the
    provider) when a configured interval is shorter than the adapter's own
    default/minimum refresh, or when the demand across all configured
    locations exceeds the provider's declared :class:`Quota`.

    Disabled providers are not checked: turning one off is always valid.
    """
    location_count = len(config.locations)
    for provider in providers:
        settings = config.providers.get(provider.id)
        if settings is not None and not settings.enabled:
            continue
        configured = getattr(settings, "interval_seconds", None)
        minimum = int(provider.default_interval_seconds or 0)
        if configured and minimum and int(configured) < minimum:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"provider '{provider.id}' is configured to poll every "
                    f"{int(configured)}s, faster than its minimum refresh of {minimum}s"
                ),
                remediation=(
                    f"raise '{provider.id}' interval_seconds to at least {minimum} "
                    "in the weather config file"
                ),
            )
        interval = provider.interval_seconds(settings)
        quota = provider.quota
        if quota is None or interval <= 0 or location_count == 0:
            continue
        if not quota.fits(interval, location_count):
            demand = quota.daily_calls(interval, location_count)
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"provider '{provider.id}' quota exceeded: {location_count} location(s) "
                    f"at a {interval}s interval need about {demand:.0f} calls/day, "
                    f"but its declared quota is {quota.calls_per_day} calls/day"
                ),
                remediation=(
                    f"raise '{provider.id}' interval_seconds, remove a location, or "
                    f"disable '{provider.id}' in the weather config file"
                ),
            )


# --- the scheduler ---------------------------------------------------------


class Scheduler:
    """Drives the collection loop over injected collaborators."""

    def __init__(
        self,
        *,
        store: WeatherStore,
        providers: Sequence[WeatherProvider],
        config: WeatherConfig,
        fetch: Any = weather_http.fetch,
        lease: Lease | None = None,
        clock: Any = None,
        sleep: Any = time.sleep,
        rng: Rng | None = None,
        env: Mapping[str, str] | None = None,
        base_tick_seconds: int = DEFAULT_BASE_TICK_SECONDS,
        jitter_seconds: float = DEFAULT_JITTER_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if base_tick_seconds <= 0:
            raise ValueError("base_tick_seconds must be positive")
        if jitter_seconds < 0:
            raise ValueError("jitter_seconds must not be negative")
        self._store = store
        self._providers = tuple(providers)
        self._config = config
        self._fetch = fetch
        self._lease: Lease = lease if lease is not None else NullLease()
        self._clock = clock if clock is not None else _utc_now
        self._sleep = sleep
        self._rng: Rng = rng if rng is not None else random.SystemRandom()
        self._env = env
        self._base_tick_seconds = int(base_tick_seconds)
        self._jitter_seconds = float(jitter_seconds)
        self._log = logger if logger is not None else LOGGER
        self._holds_lease = False

    # -- lease ------------------------------------------------------------

    def _hold_lease(self) -> bool:
        """Acquire the lease, or renew one already held."""
        try:
            if self._holds_lease:
                self._holds_lease = bool(self._lease.renew())
                if not self._holds_lease:
                    self._log.warning("fetch lease lost; this tracker stops fetching")
            else:
                self._holds_lease = bool(self._lease.acquire())
        except Exception as exc:  # pragma: no cover - defensive
            self._holds_lease = False
            self._log.warning("fetch lease unavailable: %s", exc)
        return self._holds_lease

    def close(self) -> None:
        """Release the lease. Safe to call more than once."""
        try:
            self._lease.release()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("releasing the fetch lease failed: %s", exc)
        finally:
            self._holds_lease = False

    # -- one tick ---------------------------------------------------------

    def tick(self, now: datetime | None = None, *, missed_ticks: int = 0) -> TickResult:
        """Run one base tick and return what happened.

        Never raises for anything a provider or the network did: every
        failure becomes a stored fetch record and/or an entry in the result.
        """
        started_at = _as_utc(now) if now is not None else _as_utc(self._clock())
        if not self._hold_lease():
            return TickResult(
                started_at=started_at,
                lease_held=False,
                reason=NO_LEASE_REASON,
                missed_ticks=missed_ticks,
            )

        locations = [self._config.locations[label] for label in sorted(self._config.locations)]
        outcomes: list[FetchOutcome] = []
        for provider in self._providers:
            outcomes.extend(self._run_provider(provider, locations, started_at))
        return TickResult(
            started_at=started_at,
            lease_held=True,
            outcomes=tuple(outcomes),
            missed_ticks=missed_ticks,
        )

    def _settings_for(self, provider: WeatherProvider) -> ProviderSettings | None:
        return self._config.providers.get(provider.id)

    def _run_provider(
        self,
        provider: WeatherProvider,
        locations: Sequence[Location],
        now: datetime,
    ) -> list[FetchOutcome]:
        """One provider's whole turn — isolated: nothing here escapes."""
        settings = self._settings_for(provider)
        try:
            availability = provider.availability(settings, self._env)
        except Exception as exc:  # pragma: no cover - defensive
            return [_provider_error(provider.id, "-", exc)]
        if not availability.enabled:
            reason = availability.reason or "unavailable"
            return [
                FetchOutcome(
                    provider=provider.id,
                    location=location.label,
                    status=STATUS_DISABLED,
                    reason=reason,
                )
                for location in (locations or [_NO_LOCATION])
            ]

        outcomes: list[FetchOutcome] = []
        for location in locations:
            try:
                outcomes.extend(self._run_location(provider, settings, location, now))
            except Exception as exc:
                self._log.exception(
                    "provider %s failed for location %s", provider.id, location.label
                )
                outcomes.append(_provider_error(provider.id, location.label, exc))
        return outcomes

    def _run_location(
        self,
        provider: WeatherProvider,
        settings: ProviderSettings | None,
        location: Location,
        now: datetime,
    ) -> list[FetchOutcome]:
        last_fetch = self._store.latest_fetch(provider=provider.id, location=location.label)
        due, reason = self._is_due(provider, settings, last_fetch, now)
        if not due:
            return [
                FetchOutcome(
                    provider=provider.id,
                    location=location.label,
                    status=STATUS_NOT_DUE,
                    reason=reason,
                )
            ]

        specs = list(provider.build_requests(location, settings))
        if not specs:
            return [
                FetchOutcome(
                    provider=provider.id,
                    location=location.label,
                    status=STATUS_NO_REQUEST,
                    reason="the adapter requested nothing this tick",
                )
            ]
        return [self._perform(provider, spec, last_fetch) for spec in specs]

    def _is_due(
        self,
        provider: WeatherProvider,
        settings: ProviderSettings | None,
        last_fetch: FetchRecord | None,
        now: datetime,
    ) -> tuple[bool, str | None]:
        """The provider's *own* interval, then its own refresh policy.

        Both must agree, and neither consults any other provider's settings.
        """
        if last_fetch is not None:
            interval = provider.interval_seconds(settings)
            elapsed = (now - _as_utc(last_fetch.requested_at)).total_seconds()
            if interval > 0 and elapsed < interval:
                return False, f"{elapsed:.0f}s of its {interval}s interval elapsed"
        if not provider.is_due(now, last_fetch, settings):
            return False, "the provider's refresh policy says there is nothing new"
        return True, None

    # -- one request ------------------------------------------------------

    def _perform(
        self,
        provider: WeatherProvider,
        spec: Any,
        last_fetch: FetchRecord | None,
    ) -> FetchOutcome:
        """Fetch, store the raw bytes, then normalize — in that order."""
        headers, if_modified_since = self._conditional(provider, spec, last_fetch)
        requested_at = _as_utc(self._clock())
        result, failure = self._call_fetch(spec, headers, if_modified_since)

        record = _build_record(provider.id, spec, requested_at, result, failure)
        fetch_id = self._store.save_fetch(record)
        stored = replace(record, id=fetch_id)

        outcome = FetchOutcome(
            provider=provider.id,
            location=spec.location_label,
            status=STATUS_FETCHED,
            fetch_id=fetch_id,
            http_status=stored.status,
            error_kind=stored.error.kind if stored.error else None,
            error_message=stored.error.message if stored.error else "",
            purpose=getattr(spec, "purpose", ""),
        )
        if stored.error is not None or stored.status == 304 or not stored.body:
            return outcome
        return self._normalize(provider, stored, outcome)

    def _call_fetch(
        self,
        spec: Any,
        headers: Mapping[str, str],
        if_modified_since: str | None,
    ) -> tuple[Any | None, Exception | None]:
        """Call the injected fetch; an exception from it is a failure, not a crash."""
        try:
            return (
                self._fetch(
                    spec.url,
                    timeout=getattr(spec, "timeout", weather_http.DEFAULT_TIMEOUT),
                    headers=dict(headers),
                    if_modified_since=if_modified_since,
                ),
                None,
            )
        except Exception as exc:
            return None, exc

    def _normalize(
        self,
        provider: WeatherProvider,
        stored: FetchRecord,
        outcome: FetchOutcome,
    ) -> FetchOutcome:
        """Derive and store readings; a failure never touches the raw record."""
        try:
            readings = list(provider.normalize(stored))
            if not readings:
                return outcome
            ids = self._store.save_readings(stored.id, readings)
            return replace(outcome, reading_count=len(ids))
        except Exception as exc:
            self._log.warning(
                "normalize failed for %s/%s (raw record %s kept): %s",
                provider.id,
                stored.location,
                stored.id,
                exc,
            )
            return replace(outcome, normalize_error=f"{type(exc).__name__}: {exc}")

    def _conditional(
        self,
        provider: WeatherProvider,
        spec: Any,
        last_fetch: FetchRecord | None,
    ) -> tuple[dict[str, str], str | None]:
        """Build the request headers, adding conditional-GET hints.

        An adapter that exposes ``conditional_headers(last_fetch)`` owns the
        decision entirely; otherwise a provider declaring
        :attr:`Capability.CONDITIONAL_GET` replays the last record's
        ``Last-Modified`` as ``If-Modified-Since``. The adapter's own
        :class:`RequestSpec` headers always win over both.
        """
        headers: dict[str, str] = {}
        if_modified_since = getattr(spec, "if_modified_since", None)
        helper = getattr(provider, "conditional_headers", None)
        if callable(helper):
            try:
                headers.update(
                    {
                        str(name): str(value)
                        for name, value in dict(helper(last_fetch) or {}).items()
                        if value
                    }
                )
            except Exception as exc:
                self._log.warning(
                    "conditional_headers failed for %s; fetching unconditionally: %s",
                    provider.id,
                    exc,
                )
        elif if_modified_since is None and last_fetch is not None:
            if Capability.CONDITIONAL_GET in provider.capabilities:
                if_modified_since = _header(last_fetch.cache_headers, "Last-Modified")
        headers.update(dict(getattr(spec, "headers", {}) or {}))
        return headers, if_modified_since

    # -- the loop ---------------------------------------------------------

    def run(
        self,
        *,
        stop: Any = None,
        max_ticks: int | None = None,
        start_immediately: bool = True,
    ) -> list[TickResult]:
        """Tick until ``stop`` is set (or ``max_ticks`` ticks have run).

        Waits are aligned to wall-clock multiples of the base tick plus a
        random jitter of up to :attr:`jitter_seconds`. A tick that overruns
        its slot simply misses the boundaries that passed — they are logged
        and counted, never backfilled.

        ``stop`` is anything with ``is_set()``; if it also has ``wait()``
        (a :class:`threading.Event`) that is used for the wait, making the
        loop interruptible. Returns every :class:`TickResult` produced.
        """
        results: list[TickResult] = []
        missed = 0
        period = self._base_tick_seconds
        try:
            if not start_immediately:
                self._wait(self._delay_to_boundary(_epoch(self._clock()), period), stop)
            while not _stopped(stop) and (max_ticks is None or len(results) < max_ticks):
                started = _as_utc(self._clock())
                result = self.tick(started, missed_ticks=missed)
                results.append(result)
                for line in result.log_lines():
                    self._log.info("%s", line)
                if _stopped(stop) or (max_ticks is not None and len(results) >= max_ticks):
                    break
                finished = _epoch(self._clock())
                slot = math.floor(_epoch(started) / period) * period
                boundary = _next_boundary(finished, period)
                missed = max(0, int(round((boundary - slot) / period)) - 1)
                if missed:
                    self._log.warning(
                        "tick overran its %ss slot by %.0fs; %d missed tick(s) are not "
                        "backfilled",
                        period,
                        finished - _epoch(started) - period,
                        missed,
                    )
                self._wait(boundary - finished + self._jitter(), stop)
        finally:
            self.close()
        return results

    def _jitter(self) -> float:
        if self._jitter_seconds <= 0:
            return 0.0
        return float(self._rng.uniform(0.0, self._jitter_seconds))

    def _delay_to_boundary(self, moment: float, period: int) -> float:
        return _next_boundary(moment, period) - moment + self._jitter()

    def _wait(self, delay: float, stop: Any) -> None:
        if delay <= 0:
            return
        waiter = getattr(stop, "wait", None)
        if callable(waiter):
            waiter(delay)
            return
        self._sleep(delay)


# --- helpers ---------------------------------------------------------------

#: Stand-in label used only when a provider is reported disabled and no
#: location is configured at all.
_NO_LOCATION = Location(label="-", latitude=0.0, longitude=0.0)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _epoch(value: datetime) -> float:
    return _as_utc(value).timestamp()


def _next_boundary(moment: float, period: int) -> float:
    """The first wall-clock multiple of ``period`` strictly after ``moment``."""
    return (math.floor(moment / period) + 1) * period


def _stopped(stop: Any) -> bool:
    is_set = getattr(stop, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _provider_error(provider_id: str, location: str, exc: BaseException) -> FetchOutcome:
    return FetchOutcome(
        provider=provider_id,
        location=location,
        status=STATUS_PROVIDER_ERROR,
        error_message=f"{type(exc).__name__}: {exc}",
    )


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _split_content_type(value: str | None) -> tuple[str | None, str | None]:
    """``"application/json; charset=utf-8"`` -> ``("application/json", "utf-8")``."""
    if not value:
        return None, None
    media, _, params = value.partition(";")
    charset: str | None = None
    for param in params.split(";"):
        name, _, raw = param.strip().partition("=")
        if name.strip().lower() == "charset":
            charset = raw.strip().strip('"').lower() or None
    return media.strip() or None, charset


def _classify(status: int | None, message: str) -> FetchError | None:
    """Map one fetch outcome onto a stored :class:`FetchError`, or ``None``."""
    if status is None:
        lowered = message.lower()
        kind = (
            ERROR_TIMEOUT
            if any(marker in lowered for marker in _TIMEOUT_MARKERS)
            else ERROR_TRANSPORT
        )
        return FetchError(kind=kind, message=message or kind)
    if status == 429:
        return FetchError(kind=ERROR_RATE_LIMITED, message=message or "HTTP 429")
    if status >= 400:
        return FetchError(kind=ERROR_HTTP, message=message or f"HTTP {status}")
    return None


def _build_record(
    provider_id: str,
    spec: Any,
    requested_at: datetime,
    result: Any | None,
    failure: Exception | None,
) -> FetchRecord:
    """Assemble the record from whatever came back — success or failure."""
    endpoint = redact_url(weather_http.redact(spec.url) or spec.url)
    if failure is not None:
        message = weather_http.redact(f"{type(failure).__name__}: {failure}") or "fetch failed"
        return FetchRecord(
            provider=provider_id,
            endpoint=endpoint,
            location=spec.location_label,
            requested_at=requested_at,
            status=None,
            error=_classify(None, message),
        )
    status = getattr(result, "status", None)
    headers = dict(getattr(result, "headers", {}) or {})
    content_type, charset = _split_content_type(_header(headers, "Content-Type"))
    message = weather_http.redact(getattr(result, "error", None) or "") or ""
    return FetchRecord(
        provider=provider_id,
        endpoint=endpoint,
        location=spec.location_label,
        requested_at=requested_at,
        status=status,
        body=bytes(getattr(result, "body", b"") or b""),
        content_type=content_type,
        charset=charset,
        cache_headers=_cache_headers(headers),
        error=_classify(status, message),
    )


def _cache_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep the cache-relevant response headers, under their canonical names."""
    wanted = {name.lower(): name for name in CACHE_HEADER_NAMES}
    kept: dict[str, str] = {}
    for key, value in headers.items():
        canonical = wanted.get(key.lower())
        if canonical is not None:
            kept[canonical] = value
    return kept
