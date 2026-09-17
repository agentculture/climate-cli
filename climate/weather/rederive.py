"""Re-derivation of normalized readings from stored raw fetch records.

``normalize()`` on every provider adapter is meant to be pure and
re-derivable (spec ``h2``): the same stored :class:`~climate.weather.store.FetchRecord`
always yields the same readings. This module is the tool that exercises
that promise on demand — after a bug fix in an adapter's normalization
logic, or after the shared vocabulary in ``docs/weather-api.md`` gains a
new mapping, the *readings* can be rebuilt from the *raw* bytes already on
file without a single new HTTP request.

:func:`rederive` never touches a stored :class:`FetchRecord`: it only reads
raw records through :meth:`~climate.weather.store.WeatherStore.iter_fetches`
and replaces their readings through
:meth:`~climate.weather.store.WeatherStore.replace_readings`. A record that
carries no usable body (an error, a ``304``, or an empty body) is skipped —
there is nothing to normalize. A record whose provider id is not among the
adapters handed in is skipped too — this module has no registry of its own
and works against whatever adapters the caller passes it.

Isolation is per record: one adapter raising on one record is counted and
reported as a failure, and that record's *existing* readings are left
exactly as they were (no partial or empty replacement) — a bad record never
takes the whole run down and never destroys already-good data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from climate.weather.store import WeatherStore

__all__ = ["ProviderLike", "RederiveFailure", "RederiveResult", "rederive"]


@runtime_checkable
class ProviderLike(Protocol):
    """The only two things :func:`rederive` needs from a provider adapter."""

    id: str

    def normalize(self, fetch_record: Any) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class RederiveFailure:
    """One record whose adapter raised while normalizing it.

    The record's previously stored readings are left untouched; this is
    only a report of what could not be rebuilt.
    """

    fetch_id: str
    provider: str
    error: str


@dataclass(frozen=True, slots=True)
class RederiveResult:
    """Counts describing one :func:`rederive` run.

    ``fetches_seen`` is every record :meth:`WeatherStore.iter_fetches`
    returned for the given filters; ``rederived`` + ``skipped`` + ``failed``
    always sums to ``fetches_seen``. ``readings_written`` is the total
    number of readings saved across every re-derived record (a record can
    legitimately re-derive to zero readings, which still counts as
    re-derived, not skipped or failed).
    """

    fetches_seen: int = 0
    rederived: int = 0
    skipped: int = 0
    failed: int = 0
    readings_written: int = 0
    failures: tuple[RederiveFailure, ...] = field(default_factory=tuple)


def rederive(
    store: WeatherStore,
    providers: Iterable[ProviderLike],
    *,
    provider: str | None = None,
    location: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> RederiveResult:
    """Rebuild normalized readings for stored fetch records in ``store``.

    Walks ``store.iter_fetches(provider=provider, location=location,
    since=since, until=until)`` and, for each record that carries a usable
    response body, calls the matching adapter's (matched by
    ``FetchRecord.provider == adapter.id``) ``normalize(record)`` and
    replaces that record's readings with the result via
    :meth:`~climate.weather.store.WeatherStore.replace_readings`. The raw
    record itself is never read for anything but its id/provider/status/
    body, and is never modified.

    A record is skipped (not re-derived, not failed) when it is an error
    fetch, a ``304``, carries an empty body, or names a provider id with no
    matching adapter in ``providers`` — there is nothing to normalize in
    any of those cases. A record whose adapter raises while normalizing it
    is counted as failed and reported in
    :attr:`RederiveResult.failures`; its previously stored readings are
    left exactly as they were.
    """
    by_id = {adapter.id: adapter for adapter in providers}

    fetches_seen = 0
    rederived = 0
    skipped = 0
    failed = 0
    readings_written = 0
    failures: list[RederiveFailure] = []

    for record in store.iter_fetches(
        provider=provider, location=location, since=since, until=until
    ):
        fetches_seen += 1

        if record.is_error or record.status == 304 or not record.body:
            skipped += 1
            continue

        adapter = by_id.get(record.provider)
        if adapter is None:
            skipped += 1
            continue

        try:
            readings = adapter.normalize(record)
        except Exception as exc:  # pylint: disable=broad-except
            # One misbehaving adapter/record must never take the whole
            # re-derivation run down, and must never wipe out readings that
            # were already stored for this record.
            failed += 1
            failures.append(
                RederiveFailure(fetch_id=record.id, provider=record.provider, error=str(exc))
            )
            continue

        store.replace_readings(record.id, readings)
        rederived += 1
        readings_written += len(readings)

    return RederiveResult(
        fetches_seen=fetches_seen,
        rederived=rederived,
        skipped=skipped,
        failed=failed,
        readings_written=readings_written,
        failures=tuple(failures),
    )
