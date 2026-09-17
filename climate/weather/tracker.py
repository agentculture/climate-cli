"""Tracker service entry point: ``python -m climate.weather.tracker``.

Thin wiring only — every decision lives elsewhere:

* :func:`climate.weather.config.load_tracker_config` owns "is there at
  least one location configured".
* :func:`climate.weather.providers.iter_providers` owns adapter discovery.
* :func:`climate.weather.scheduler.validate` owns interval/quota checks.
* :class:`climate.weather.providers.base.WeatherProvider.availability`
  owns whether one provider's credential is present.
* :mod:`climate.weather.mongo` owns the store and the tracker lease
  primitives (``build_store``, ``lease_collection``,
  ``acquire_lease``/``renew_lease``/``release_lease``).
* :class:`climate.weather.scheduler.Scheduler` owns the fetch loop itself
  — due logic, isolation between providers, wall-clock alignment and
  jitter, missed-tick accounting.

This module's own job is: load configuration, validate it against the
registered adapters, validate secrets *eagerly* (log the disabled ones,
fail only when nothing at all is usable), acquire the single-tracker
lease before issuing a single provider request, record a heartbeat so
``doctor``/the health route can see this process is alive, and drive the
scheduler in the foreground until asked to stop.

Heartbeat wiring
----------------
:class:`~climate.weather.scheduler.Scheduler` has no per-tick hook to
attach to without editing it, and this task's brief says not to touch
that module. The least invasive seam is Python's own polymorphism:
:class:`_HeartbeatScheduler` below subclasses ``Scheduler`` and overrides
only :meth:`~climate.weather.scheduler.Scheduler.tick`, calling the base
implementation and then writing a heartbeat. ``Scheduler.run`` calls
``self.tick(...)`` — through ``self``, so the overridden method runs on
every tick :func:`~climate.weather.scheduler.Scheduler.run` drives,
without changing a single line of ``scheduler.py``. A heartbeat is also
written once before the loop starts ("on start").

The lease
---------
:class:`MongoLease` adapts the three free functions in
:mod:`climate.weather.mongo` (``acquire_lease``/``renew_lease``/
``release_lease``) to the :class:`~climate.weather.scheduler.Lease`
protocol the scheduler already knows how to hold. Its holder id mixes the
hostname, pid and a random suffix so two trackers racing to start at the
same instant on the same host still get distinguishable identities. Its
TTL is three times the scheduler's base tick, so one missed renewal never
loses the lease to a transient stall.

This module also performs its own acquire attempt *before* the scheduler
exists, so a lease held by another tracker is caught, reported and exited
on before a single :class:`~climate.weather.providers.base.RequestSpec`
is built or fetched — the ``Scheduler`` would otherwise only discover the
same fact on its first tick and keep retrying forever rather than exiting.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

from climate import __version__ as PACKAGE_VERSION
from climate.cli._errors import EXIT_ENV_ERROR, EXIT_SUCCESS, CliError
from climate.cli._output import emit_error
from climate.weather import http as weather_http
from climate.weather import mongo
from climate.weather import scheduler as weather_scheduler
from climate.weather.config import load_tracker_config
from climate.weather.providers import iter_providers

__all__ = ["MongoLease", "main"]

LOGGER = logging.getLogger("climate.weather.tracker")

#: The lease is held for three base ticks; one missed renewal never loses it.
LEASE_TTL_MULTIPLIER = 3

_NO_PROVIDER_MESSAGE = "no weather provider is available"
_NO_PROVIDER_REMEDIATION = (
    "enable a keyless provider (open-meteo, met-no, metar or ims-forecast) in the "
    "weather config file, or set the required API key/token environment variable "
    "for a provider you want to use"
)
_LEASE_HELD_MESSAGE = "another tracker holds the fetch lease"
_LEASE_HELD_REMEDIATION = (
    "wait for the other tracker to exit and its lease to expire, or stop it, "
    "before starting a new one"
)


def _configure_logging(logger: logging.Logger) -> None:
    """Attach a stderr handler once; idempotent across repeated ``main()`` calls."""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _holder_id() -> str:
    """``hostname-pid-suffix``: distinguishable even for two trackers racing to start."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class MongoLease:
    """Adapts :mod:`climate.weather.mongo`'s lease functions to the scheduler's
    :class:`~climate.weather.scheduler.Lease` protocol.

    ``collection`` is whatever :func:`climate.weather.mongo.lease_collection`
    (or a test's fake) returns; this class performs no connection of its
    own. ``now`` is injectable for tests; it defaults to the real UTC clock.
    """

    def __init__(
        self,
        collection: Any,
        holder_id: str,
        ttl_seconds: float,
        *,
        now: Any = None,
    ) -> None:
        self._collection = collection
        self._holder_id = holder_id
        self._ttl_seconds = ttl_seconds
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def acquire(self) -> bool:
        return mongo.acquire_lease(
            self._collection, self._holder_id, self._ttl_seconds, now=self._now()
        )

    def renew(self) -> bool:
        return mongo.renew_lease(
            self._collection, self._holder_id, self._ttl_seconds, now=self._now()
        )

    def release(self) -> None:
        mongo.release_lease(self._collection, self._holder_id)


def _save_heartbeat(store: Any) -> None:
    """Best-effort heartbeat: tolerate a store that does not implement it."""
    save = getattr(store, "save_heartbeat", None)
    if callable(save):
        save(PACKAGE_VERSION, datetime.now(UTC))


class _HeartbeatScheduler(weather_scheduler.Scheduler):
    """A :class:`~climate.weather.scheduler.Scheduler` that heartbeats every tick.

    See the module docstring: overriding ``tick`` is the seam that lets
    ``run()`` (unmodified, in ``scheduler.py``) drive a heartbeat on every
    tick it performs, since ``run()`` calls ``self.tick(...)``.
    """

    def __init__(self, *args: Any, heartbeat: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._heartbeat = heartbeat

    def tick(self, *args: Any, **kwargs: Any) -> weather_scheduler.TickResult:
        result = super().tick(*args, **kwargs)
        self._heartbeat()
        return result


def main(
    argv: list[str] | None = None,
    *,
    store_factory: Any = None,
    lease_collection_factory: Any = None,
    fetch: Any = None,
    stop: Any = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Load config, validate secrets, acquire the lease, run the scheduler.

    Every collaborator is injectable so tests never touch a socket, a
    database or the real environment: ``store_factory``/
    ``lease_collection_factory`` stand in for
    :func:`climate.weather.mongo.build_store` /
    :func:`climate.weather.mongo.lease_collection`, ``fetch`` for
    :func:`climate.weather.http.fetch`, and ``stop`` for the
    :class:`threading.Event` that SIGTERM/SIGINT set in production.
    """
    del argv  # no CLI flags of its own today; kept for a stable entry-point shape
    environ: Mapping[str, str] = env if env is not None else os.environ
    _configure_logging(LOGGER)

    try:
        config = load_tracker_config()
        providers = list(iter_providers())
        weather_scheduler.validate(config, providers)

        any_available = False
        for provider in providers:
            settings = config.providers.get(provider.id)
            availability = provider.availability(settings, environ)
            if availability.enabled:
                any_available = True
            else:
                LOGGER.info(
                    "provider %s disabled: %s", provider.id, availability.reason or "unavailable"
                )
        if not any_available:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=_NO_PROVIDER_MESSAGE,
                remediation=_NO_PROVIDER_REMEDIATION,
            )

        store = store_factory() if store_factory is not None else mongo.build_store()

        base_tick_seconds = weather_scheduler.DEFAULT_BASE_TICK_SECONDS
        lease_collection = (
            lease_collection_factory()
            if lease_collection_factory is not None
            else mongo.lease_collection()
        )
        lease = MongoLease(lease_collection, _holder_id(), base_tick_seconds * LEASE_TTL_MULTIPLIER)
        if not lease.acquire():
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=_LEASE_HELD_MESSAGE,
                remediation=_LEASE_HELD_REMEDIATION,
            )

        _save_heartbeat(store)

        stop_event = stop if stop is not None else threading.Event()

        def _on_signal(signum: int, frame: Any) -> None:
            del signum, frame
            stop_event.set()

        previous_sigterm = signal.signal(signal.SIGTERM, _on_signal)
        previous_sigint = signal.signal(signal.SIGINT, _on_signal)
        try:
            scheduler = _HeartbeatScheduler(
                store=store,
                providers=providers,
                config=config,
                fetch=fetch if fetch is not None else weather_http.fetch,
                lease=lease,
                env=environ,
                base_tick_seconds=base_tick_seconds,
                logger=LOGGER,
                heartbeat=lambda: _save_heartbeat(store),
            )
            scheduler.run(stop=stop_event)
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    except CliError as exc:
        emit_error(exc, json_mode=False)
        return exc.code
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
