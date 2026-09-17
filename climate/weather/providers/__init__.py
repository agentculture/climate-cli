"""Weather provider adapters and the registry that finds them.

Adding an adapter requires **no edit to this file**. Drop a module into this
package that defines a concrete :class:`~climate.weather.providers.base.WeatherProvider`
subclass with a non-empty ``id``::

    # climate/weather/providers/met_no.py
    from climate.weather.providers.base import WeatherProvider

    class MetNoProvider(WeatherProvider):
        id = "met-no"
        ...

The registry discovers it with :mod:`pkgutil` over this package's
``__path__`` and instantiates it with no arguments. Modules whose name starts
with ``_`` and the ``base`` module itself are skipped. A class can opt out of
discovery by leaving ``id`` empty, and a class defined outside this package
can opt *in* with the :func:`register` decorator.

Discovery imports adapter modules, which are standard-library only (the whole
service is), so reading provider metadata — what ``climate providers`` does —
never pulls in a third-party module, a network client or MongoDB.

Results are cached; call :func:`clear_cache` after adding modules at runtime
(tests do this).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

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

__all__ = [
    "Attribution",
    "AuthRequirement",
    "Availability",
    "Capability",
    "FreshnessStrategy",
    "ProviderSettings",
    "Quota",
    "RequestSpec",
    "UnknownProviderError",
    "WeatherProvider",
    "clear_cache",
    "describe_providers",
    "get_provider",
    "iter_providers",
    "provider_ids",
    "register",
    "validate_provider",
]

_SKIP_MODULES = frozenset({"base"})

_extra: dict[str, type[WeatherProvider]] = {}
_cache: tuple[WeatherProvider, ...] | None = None


class UnknownProviderError(LookupError):
    """Raised when no registered adapter carries the requested id."""


def register(provider_cls: type[WeatherProvider]) -> type[WeatherProvider]:
    """Explicitly register an adapter class (decorator).

    Only needed for a class defined outside this package; adapters that live
    in their own module here are found automatically.
    """
    if not getattr(provider_cls, "id", ""):
        raise ValueError("a registered provider must set a non-empty id")
    _extra[provider_cls.id] = provider_cls
    clear_cache()
    return provider_cls


def clear_cache() -> None:
    """Forget the discovered providers so the next call re-scans."""
    global _cache
    _cache = None


def iter_providers() -> tuple[WeatherProvider, ...]:
    """Every registered adapter, instantiated, ordered by id."""
    global _cache
    if _cache is None:
        classes: dict[str, type[WeatherProvider]] = dict(_discover())
        classes.update(_extra)
        _cache = tuple(classes[pid]() for pid in sorted(classes))
    return _cache


def provider_ids() -> tuple[str, ...]:
    """The ids of every registered adapter, sorted."""
    return tuple(provider.id for provider in iter_providers())


def get_provider(provider_id: str) -> WeatherProvider:
    """The adapter registered under ``provider_id``.

    Raises:
        UnknownProviderError: when no adapter carries that id.
    """
    for provider in iter_providers():
        if provider.id == provider_id:
            return provider
    known = ", ".join(provider_ids()) or "none"
    raise UnknownProviderError(f"unknown provider {provider_id!r} (registered: {known})")


def describe_providers(
    settings: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One JSON-ready metadata row per adapter, for ``climate providers``.

    ``settings`` maps provider id to that provider's settings object; ids
    without an entry are described with their adapter defaults.
    """
    per_provider = settings or {}
    return [provider.describe(per_provider.get(provider.id), env) for provider in iter_providers()]


def _discover() -> dict[str, type[WeatherProvider]]:
    """Import sibling modules and collect concrete provider classes."""
    found: dict[str, type[WeatherProvider]] = {}
    for info in pkgutil.iter_modules(list(__path__)):
        if info.name.startswith("_") or info.name in _SKIP_MODULES:
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, WeatherProvider) or obj is WeatherProvider:
                continue
            if inspect.isabstract(obj) or not getattr(obj, "id", ""):
                continue
            found.setdefault(obj.id, obj)
    return found
