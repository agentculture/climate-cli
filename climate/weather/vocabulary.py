"""The canonical weather variable / unit vocabulary.

``docs/weather-api.md`` section 4 ("Vocabulary") and section 4.1 ("Units")
define the shared vocabulary every adapter normalizes into and every API
route validates against. This module is the **single executable copy** of
those two tables: nothing else in ``climate/`` may keep its own list.

``tests/weather/test_vocabulary.py`` asserts this module and the two
markdown tables are *identical*, so the two can never drift again — which
they had: the API's own allowlist had 19 rows against the document's 37,
and every variable missing from it (``showers``, ``snowfall``,
``cloud_cover_low``, ``uv_index_clear_sky``, …) was rejected on an explicit
request and hidden from the defaults, silently burying data the adapters
had already stored.

The vocabulary is a shared vocabulary, **not a filter** (section 4): a
provider value with no row here is still emitted, under the id
``x_<provider_field_name>``, with its original value and unit preserved.
:func:`is_extension` recognises those ids, and :func:`is_known` accepts
them, so an extension variable stays queryable — its unit is then whatever
the adapter stored, not something this table can assign.

Standard library only (in fact, no imports at all): the host CLI must stay
importable with every third-party package blocked.
"""

from __future__ import annotations

__all__ = [
    "EXTENSION_PREFIX",
    "UNITS",
    "VARIABLES",
    "is_extension",
    "is_known",
    "unit_for",
]

#: The prefix an adapter gives a provider value that has no vocabulary row
#: (section 4, "No provider value is dropped").
EXTENSION_PREFIX = "x_"

#: Variable id -> canonical unit id (``docs/weather-api.md`` section 4).
VARIABLES: dict[str, str] = {
    "temperature": "degC",
    "apparent_temperature": "degC",
    "dew_point": "degC",
    "relative_humidity": "percent",
    "pressure_msl": "hPa",
    "pressure_surface": "hPa",
    "wind_speed": "m_s",
    "wind_gust": "m_s",
    "wind_direction": "deg",
    "precipitation": "mm",
    "rain": "mm",
    "precipitation_probability": "percent",
    "cloud_cover": "percent",
    "visibility": "m",
    "shortwave_radiation": "w_m2",
    "direct_radiation": "w_m2",
    "diffuse_radiation": "w_m2",
    "uv_index": "index",
    "weather_code": "code",
    "showers": "mm",
    "snowfall": "mm",
    "cloud_cover_low": "percent",
    "cloud_cover_medium": "percent",
    "cloud_cover_high": "percent",
    "fog": "percent",
    "uv_index_clear_sky": "index",
    "direct_normal_radiation": "w_m2",
    "wind_gust_direction": "deg",
    "wind_speed_max_1min": "m_s",
    "wind_speed_max_10min": "m_s",
    "wind_direction_std": "deg",
    "temperature_max": "degC",
    "temperature_min": "degC",
    "temperature_grass_min": "degC",
    "relative_humidity_max": "percent",
    "relative_humidity_min": "percent",
    "is_day": "index",
}

#: Every unit id the API may report (``docs/weather-api.md`` section 4.1).
#: ``other`` is the documented escape hatch: "a unit outside this table;
#: read ``original_unit``".
UNITS: frozenset[str] = frozenset(
    {
        "degC",
        "percent",
        "hPa",
        "m_s",
        "deg",
        "mm",
        "w_m2",
        "m",
        "index",
        "code",
        "other",
    }
)

#: The unit an extension variable falls back to when nothing better is known.
FALLBACK_UNIT = "other"


def is_extension(variable: str) -> bool:
    """True for an ``x_``-prefixed provider extension id (section 4).

    The prefix alone is not enough: ``"x_"`` with nothing after it names no
    provider field and is not a valid extension id.
    """
    return variable.startswith(EXTENSION_PREFIX) and len(variable) > len(EXTENSION_PREFIX)


def is_known(variable: str) -> bool:
    """True when ``variable`` may be requested: a vocabulary id or an extension."""
    return variable in VARIABLES or is_extension(variable)


def unit_for(variable: str) -> str | None:
    """The canonical unit id for ``variable``, or ``None``.

    ``None`` means "this table assigns no unit" — either an unknown id or an
    ``x_`` extension, whose unit is whatever the adapter stored and must be
    read off the stored measurement rather than guessed here.
    """
    return VARIABLES.get(variable)
