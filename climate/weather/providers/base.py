"""The weather provider contract.

Every provider adapter under :mod:`climate.weather.providers` subclasses
:class:`WeatherProvider`. The contract is deliberately small and pure: an
adapter *describes* itself (capabilities, auth, quota, freshness policy,
licence/attribution), *describes* the HTTP requests it wants made
(:class:`RequestSpec`), decides whether it is due, and turns a stored fetch
record into normalized readings. It never performs I/O itself — the HTTP
helper (``climate.weather.http``) makes the calls, the store
(``climate.weather.store``) persists them and the scheduler
(``climate.weather.scheduler``) drives the loop.

Spec targets: ``c21`` (per-provider cadence and configuration), ``c46``
(provider surface: capabilities, auth, quota, freshness, attribution) and
``c47`` (attribution shown wherever the data is).

Cross-module types
------------------
Three types come from sibling modules. To keep this module import-light and
stdlib-only they are expressed structurally, as :class:`typing.Protocol`:

``LocationLike``
    A configured location from ``climate.weather.config`` — a ``label`` plus
    already-rounded ``latitude`` / ``longitude``.
``FetchRecordLike``
    One stored fetch from ``climate.weather.store`` — at minimum
    ``requested_at`` (UTC), ``status`` (``None`` for a transport failure),
    response ``headers`` and the verbatim ``body`` bytes.
``ReadingLike``
    A normalized reading from ``climate.weather.store``. ``normalize()``
    returns a sequence of them; this module never inspects one, so it is
    typed as an opaque protocol.

Anything that structurally matches works, which is what lets the adapters,
the store and this contract be built in parallel.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Protocol, Sequence, runtime_checkable

from climate.weather.http import redact, redact_headers

__all__ = [
    "Attribution",
    "AuthRequirement",
    "Availability",
    "Capability",
    "FetchRecordLike",
    "FreshnessStrategy",
    "LocationLike",
    "ProviderSettings",
    "ProviderSettingsLike",
    "Quota",
    "ReadingLike",
    "RequestSpec",
    "WeatherProvider",
    "validate_provider",
]

_DAY_SECONDS = 86_400


# --- structural types owned by sibling modules ----------------------------


@runtime_checkable
class LocationLike(Protocol):
    """A configured location (see ``climate.weather.config``)."""

    label: str
    latitude: float
    longitude: float


@runtime_checkable
class FetchRecordLike(Protocol):
    """One stored fetch (see ``climate.weather.store``).

    Only the fields this contract actually reads are declared. The store's
    record carries more (endpoint, content hash, charset, error detail, …);
    adapters are free to read those in :meth:`WeatherProvider.normalize`.
    """

    requested_at: datetime
    status: int | None
    headers: Mapping[str, str] | None
    body: bytes


@runtime_checkable
class ReadingLike(Protocol):
    """A normalized reading (see ``climate.weather.store``).

    Opaque here on purpose: the store owns its shape (values with units,
    provider/source/model, ``observed_at`` vs ``requested_at``, kind, and a
    reference to the fetch record it was derived from).
    """


@runtime_checkable
class ProviderSettingsLike(Protocol):
    """Per-provider user configuration (see ``climate.weather.config``)."""

    enabled: bool
    interval_seconds: int | None
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderSettings:
    """A minimal, concrete :class:`ProviderSettingsLike`.

    The config task owns the real settings type. This one exists so adapters
    and their tests can be written and exercised without it; anything that
    satisfies :class:`ProviderSettingsLike` is accepted everywhere.

    ``params`` holds per-provider request parameters (forecast horizon,
    variables, station ids, ``cadence_seconds``, …).
    """

    enabled: bool = True
    interval_seconds: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    quota: "Quota | None" = None


# --- metadata value types -------------------------------------------------


class Capability(StrEnum):
    """What a provider can supply. Reported by ``climate providers``."""

    CURRENT_OBSERVATION = "current-observation"
    """Measured station/airport observations."""

    CURRENT_MODEL = "current-model"
    """A 'now' value taken from a model slice, not a measurement."""

    FORECAST = "forecast"
    """A forward-looking series stored in the same response."""

    RADIATION = "radiation"
    """Solar radiation channels (shortwave/direct/diffuse, Grad/DiffR/NIP)."""

    CONDITIONAL_GET = "conditional-get"
    """Honours ``If-Modified-Since`` / ``Expires`` (304-capable)."""

    STATION_METADATA = "station-metadata"
    """Needs a station/channel discovery call before data requests."""


class FreshnessStrategy(StrEnum):
    """How a provider decides that new data may exist (spec ``c21``)."""

    INTERVAL = "interval"
    """Poll on the provider's own configured interval."""

    HTTP_EXPIRES = "http-expires"
    """Wait for the ``Expires`` of the last response (met-no)."""

    STATION_CADENCE = "station-cadence"
    """Follow the station's publication cadence (ims, METAR)."""


@dataclass(frozen=True)
class AuthRequirement:
    """Whether the provider needs a credential, and where it comes from.

    Secrets live in environment variables only (never in the config file, the
    repo, fetch records or logs). ``env_var`` names the variable, e.g.
    ``CLIMATE_OPENWEATHER_API_KEY`` or ``CLIMATE_IMS_API_TOKEN``.
    """

    required: bool = False
    env_var: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready metadata for the ``providers`` verb."""
        return {"required": self.required, "env_var": self.env_var, "note": self.note}


@dataclass(frozen=True)
class Quota:
    """Declared request budget for a provider (spec ``c46``).

    All limits are optional; a provider with no published limit sets
    ``unlimited=True`` rather than leaving every field ``None`` (an empty
    quota is treated as missing metadata by :func:`validate_provider`).

    ``call_weight`` is how many API calls one request costs — Open-Meteo
    charges a wide request as several calls (``variables * models / 10``,
    minimum 1), so an adapter computes its weight and declares it here.

    ``verified`` / ``source`` record provenance: several of these limits come
    from issue 5 and third-party pages rather than the provider's own docs
    (a recorded non-blocking unknown), and the CLI should be able to say so.
    """

    calls_per_minute: int | None = None
    calls_per_day: int | None = None
    calls_per_month: int | None = None
    call_weight: float = 1.0
    unlimited: bool = False
    verified: bool = False
    source: str = ""
    notes: str = ""

    def daily_calls(self, interval_seconds: int, locations: int = 1) -> float:
        """Weighted API calls per day at ``interval_seconds`` for N locations."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        fetches = (_DAY_SECONDS / interval_seconds) * max(locations, 0)
        return fetches * self.call_weight

    def fits(self, interval_seconds: int, locations: int = 1) -> bool:
        """Whether that polling plan stays inside the declared daily budget."""
        if self.unlimited or self.calls_per_day is None:
            return True
        return self.daily_calls(interval_seconds, locations) <= self.calls_per_day

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready metadata for ``climate providers --limits``."""
        return {
            "calls_per_minute": self.calls_per_minute,
            "calls_per_day": self.calls_per_day,
            "calls_per_month": self.calls_per_month,
            "call_weight": self.call_weight,
            "unlimited": self.unlimited,
            "verified": self.verified,
            "source": self.source,
            "notes": self.notes,
        }

    def is_declared(self) -> bool:
        """False when the quota carries neither a limit nor an 'unlimited' flag."""
        return self.unlimited or any(
            value is not None
            for value in (self.calls_per_minute, self.calls_per_day, self.calls_per_month)
        )


@dataclass(frozen=True)
class Attribution:
    """Licence credit the provider's terms require (spec ``c46``, ``c47``).

    ``text`` is shown verbatim in the dashboard footer and the ``providers``
    verb; ``url`` links the licence or terms. Both are mandatory — an adapter
    cannot ship without credit.
    """

    text: str
    url: str
    licence: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("attribution text is required")
        if not self.url.strip():
            raise ValueError("attribution url is required")

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready metadata for the ``providers`` verb and the dashboard."""
        return {"text": self.text, "url": self.url, "licence": self.licence}


@dataclass(frozen=True)
class Availability:
    """Whether a provider may run right now, and why not when it may not.

    A missing credential disables only that provider, with a reason the CLI
    and ``doctor`` can print — it never fails the tick (spec ``c45``).
    """

    enabled: bool
    reason: str | None = None


@dataclass(frozen=True)
class RequestSpec:
    """One HTTP request an adapter wants made — a description, not a call.

    The scheduler hands this to ``climate.weather.http``, stores the result
    as a fetch record (with ``url`` redacted) and passes that record back to
    :meth:`WeatherProvider.normalize`.

    Attributes:
        provider_id: The adapter's :attr:`WeatherProvider.id`.
        location_label: The configured label (never coordinates in output).
        url: Full https URL including query string. http is rejected.
        method: Always ``GET`` today; kept explicit for the fetch record.
        headers: Extra request headers (e.g. met-no's identifying User-Agent).
        timeout: Per-fetch timeout in seconds (spec ``c41``).
        if_modified_since: RFC 7231 date for a conditional GET, or ``None``.
        purpose: Short label, e.g. ``"current"``, ``"stations"`` — stored with
            the fetch record so normalize can tell responses apart.
        context: Opaque adapter-owned data carried alongside the request
            (station ids, requested variables, …). Immutable.
    """

    provider_id: str
    location_label: str
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 20.0
    if_modified_since: str | None = None
    purpose: str = "fetch"
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url.lower().startswith("https://"):
            raise ValueError(f"provider requests must use https: {redact(self.url)!r}")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))

    @property
    def redacted_url(self) -> str:
        """``url`` with API keys and tokens removed - the only form to store or log."""
        return redact(self.url) or ""

    def __repr__(self) -> str:
        # ``url`` and ``headers`` may carry a live API key (OpenWeather's
        # ``appid``, IMS's Authorization header): never let a stray
        # ``repr``/``str``/log call print it.
        return (
            f"RequestSpec(provider_id={self.provider_id!r}, "
            f"location_label={self.location_label!r}, url={self.redacted_url!r}, "
            f"method={self.method!r}, headers={redact_headers(self.headers)!r}, "
            f"purpose={self.purpose!r})"
        )


# --- the provider contract ------------------------------------------------


class WeatherProvider(ABC):
    """Base class for every weather provider adapter.

    A concrete adapter sets the seven class-level metadata attributes and
    implements :meth:`build_requests` and :meth:`normalize`. :meth:`is_due`
    has a working default for all three freshness strategies; override it
    only when a provider needs something the default cannot express.

    Two more hooks have defaults an adapter rarely needs to touch:
    :meth:`credential` reads this provider's key out of the environment
    mapping it is given (the one :meth:`availability` was resolved against,
    and the one :meth:`build_requests` receives as ``env``), and
    :meth:`requests_per_tick` declares how many HTTP requests one due tick
    issues for one location — override it when the adapter fans out over
    stations, so startup quota validation counts what will really be sent.

    Subclasses live in their own module under
    :mod:`climate.weather.providers` and are discovered automatically — see
    that package's docstring. Nothing needs to be edited to add one.

    Example::

        class OpenMeteoProvider(WeatherProvider):
            id = "open-meteo"
            capabilities = frozenset({Capability.CURRENT_MODEL, Capability.FORECAST})
            auth = AuthRequirement(required=False)
            default_interval_seconds = 900
            quota = Quota(calls_per_day=10_000, source="issue 5")
            freshness = FreshnessStrategy.INTERVAL
            attribution = Attribution(
                text="Weather data by Open-Meteo.com (CC BY 4.0)",
                url="https://open-meteo.com/en/license",
                licence="CC BY 4.0",
            )

            def build_requests(self, location, settings=None, *, env=None): ...
            def normalize(self, fetch_record): ...
    """

    #: Stable provider id, e.g. ``"met-no"``. Used everywhere as the key.
    id: ClassVar[str] = ""

    #: What the provider supplies.
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    #: Credential requirement; keyless providers use the default.
    auth: ClassVar[AuthRequirement] = AuthRequirement()

    #: Poll interval when the user configures none, in seconds.
    default_interval_seconds: ClassVar[int] = 0

    #: Declared request budget.
    quota: ClassVar[Quota | None] = None

    #: How the adapter decides new data may exist.
    freshness: ClassVar[FreshnessStrategy | None] = None

    #: Licence credit. Mandatory.
    attribution: ClassVar[Attribution | None] = None

    # -- required of every adapter ----------------------------------------

    @abstractmethod
    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Sequence[RequestSpec]:
        """Describe the requests to issue for ``location`` on this tick.

        Returns zero or more :class:`RequestSpec`. Returning an empty
        sequence is legitimate (nothing to ask for right now). Must not
        perform I/O and must never embed a secret in a stored field without
        redaction downstream.

        ``env`` is the environment mapping the caller resolved this
        provider's availability against; ``None`` means :data:`os.environ`.
        An adapter that needs a credential reads it with
        :meth:`credential` (passing ``env`` straight through) rather than
        from :data:`os.environ` directly, so that the key which decided
        "enabled" is the key that builds the request. The scheduler passes
        the keyword only to adapters whose signature accepts it, so an
        adapter that does not need a credential may keep the two-argument
        form.
        """
        raise NotImplementedError

    @abstractmethod
    def normalize(self, fetch_record: FetchRecordLike) -> Sequence[ReadingLike]:
        """Derive normalized readings from one stored fetch record.

        Pure and re-derivable: the same record always gives the same
        readings (spec ``h2``). A failed or ``304`` fetch yields no readings.
        ``observed_at`` comes from the provider's own timestamp, never from
        ``requested_at``, and provider/source/model stay distinct.
        """
        raise NotImplementedError

    # -- shared behaviour --------------------------------------------------

    def interval_seconds(self, settings: ProviderSettingsLike | None = None) -> int:
        """The effective poll interval: user setting, else adapter default."""
        configured = getattr(settings, "interval_seconds", None)
        if configured:
            return int(configured)
        return int(self.default_interval_seconds)

    def credential(self, env: Mapping[str, str] | None = None) -> str | None:
        """This provider's credential read from ``env`` (default: ``os.environ``).

        The single place a secret enters an adapter: it reads
        :attr:`AuthRequirement.env_var` from the *same* mapping
        :meth:`availability` was resolved against, so an injected key can
        never be declared "enabled" and then be missing when the request is
        built. Returns ``None`` when the provider needs no credential or the
        variable is unset or empty.
        """
        if not self.auth.env_var:
            return None
        environ = os.environ if env is None else env
        return environ.get(self.auth.env_var) or None

    def requests_per_tick(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
    ) -> int:
        """How many HTTP requests one due tick issues for one location.

        The default is ``1`` — one location, one request. An adapter that
        fans out (a request per selected station, a metadata call before the
        data call) overrides this so that startup quota validation
        (:func:`climate.weather.scheduler.validate`) counts what will really
        be sent rather than one call per location. Pure and cheap: it must
        not perform I/O, and it should agree with what
        :meth:`build_requests` produces for the same arguments.
        """
        del location, settings
        return 1

    def availability(
        self,
        settings: ProviderSettingsLike | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Availability:
        """Whether this provider may be fetched, with a reason when it may not.

        Disabled in configuration wins; otherwise a required credential that
        is absent from ``env`` (default: ``os.environ``) disables it.
        """
        if settings is not None and not getattr(settings, "enabled", True):
            return Availability(False, "disabled in configuration")
        if self.auth.required and self.auth.env_var and not self.credential(env):
            name = self.id or "this provider"
            return Availability(
                False,
                f"{self.auth.env_var} is not set; set it to enable {name}",
            )
        return Availability(True, None)

    def is_due(
        self,
        now: datetime,
        last_fetch: FetchRecordLike | None,
        settings: ProviderSettingsLike | None = None,
    ) -> bool:
        """Whether a fetch may be made at ``now``, given the last fetch.

        This is the *refresh policy* half of spec ``c21``; the scheduler
        additionally gates on the provider's own interval, so both must
        agree. With no previous fetch the answer is always ``True``.

        * ``interval`` — due once the effective interval has elapsed.
        * ``http-expires`` — due once the interval has elapsed *and* the last
          response's ``Expires`` has passed (an unparseable or absent header
          falls back to the interval alone).
        * ``station-cadence`` — due once ``params["cadence_seconds"]`` (or
          the effective interval) has elapsed.
        """
        if last_fetch is None:
            return True
        last_at = _as_utc(getattr(last_fetch, "requested_at", None))
        if last_at is None:
            return True
        elapsed = (_as_utc(now) - last_at).total_seconds()

        if self.freshness is FreshnessStrategy.STATION_CADENCE:
            params = getattr(settings, "params", None) or {}
            cadence = int(params.get("cadence_seconds") or self.interval_seconds(settings))
            return elapsed >= cadence

        if elapsed < self.interval_seconds(settings):
            return False

        if self.freshness is FreshnessStrategy.HTTP_EXPIRES:
            # The store's FetchRecord names them ``cache_headers``.
            headers = getattr(last_fetch, "cache_headers", None)
            if headers is None:
                headers = getattr(last_fetch, "headers", None)
            expires = _header_date(headers, "Expires")
            if expires is not None:
                return _as_utc(now) >= expires
        return True

    def describe(
        self,
        settings: ProviderSettingsLike | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """JSON-ready metadata row for ``climate providers`` (spec ``c46``)."""
        availability = self.availability(settings, env)
        return {
            "id": self.id,
            "capabilities": sorted(str(c) for c in self.capabilities),
            "auth": self.auth.as_dict(),
            "default_interval_seconds": self.default_interval_seconds,
            "interval_seconds": self.interval_seconds(settings),
            "freshness": str(self.freshness) if self.freshness else None,
            "quota": self.quota.as_dict() if self.quota else None,
            "attribution": self.attribution.as_dict() if self.attribution else None,
            "enabled": availability.enabled,
            "reason": availability.reason,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"


# --- validation -----------------------------------------------------------


def validate_provider(provider: WeatherProvider) -> list[str]:
    """Return the contract problems of ``provider`` (empty list when sound).

    Enforces spec ``c46``'s honesty condition: every adapter has attribution
    text and URL, declared quota metadata and a freshness strategy. The
    registry's own test and the ``providers`` verb's test both run this over
    every discovered adapter, so a new adapter cannot ship without them.
    """
    problems: list[str] = []
    if not getattr(provider, "id", ""):
        problems.append("id is empty")
    if not provider.capabilities:
        problems.append("capabilities are empty")
    if not provider.freshness:
        problems.append("freshness strategy is missing")
    elif provider.freshness not in tuple(FreshnessStrategy):
        problems.append(f"freshness strategy {provider.freshness!r} is not a FreshnessStrategy")
    if provider.quota is None:
        problems.append("quota metadata is missing")
    elif not provider.quota.is_declared():
        problems.append("quota metadata declares no limit and is not marked unlimited")
    if provider.attribution is None:
        problems.append("attribution is missing")
    else:
        if not provider.attribution.text.strip():
            problems.append("attribution text is empty")
        if not provider.attribution.url.strip():
            problems.append("attribution url is empty")
    if provider.default_interval_seconds <= 0:
        problems.append("default interval must be a positive number of seconds")
    return problems


# --- helpers --------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat a naive datetime as UTC; everything stored here is UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _header_date(headers: Mapping[str, str] | None, name: str) -> datetime | None:
    """Parse an HTTP date header case-insensitively; ``None`` when unusable."""
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            try:
                return _as_utc(parsedate_to_datetime(value))
            except (TypeError, ValueError):
                return None
    return None
