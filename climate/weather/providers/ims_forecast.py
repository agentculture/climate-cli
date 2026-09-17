"""IMS keyless city-forecast XML adapter (spec ``c8``).

The Israel Meteorological Service publishes a keyless, no-token daily
forecast feed for roughly fifteen Israeli cities as one ISO-8859-8-encoded
XML document. This adapter fetches that single feed and, on
:meth:`ImsForecastProvider.normalize`, picks **one** city out of it —
the first name from the user's ordered ``params["cities"]`` candidate list
that is actually present in that day's feed.

The ordered-candidate-list shape (rather than a single city name) exists
because the feed only ever lists about fifteen cities: a user names their
own town first and a larger, more likely-to-be-listed city as a fallback.
Matching is case-insensitive and tolerant of the feed's `` - `` / `-`
spacing variants (e.g. ``"Tel Aviv-Yafo"`` matches the feed's
``"Tel Aviv - Yafo"``).

There is no default city anywhere in this module. With no candidates
configured, :meth:`build_requests` makes no request at all — mirroring
``metar``'s "nothing configured, nothing fetched" behaviour, since a feed
fetch that can never normalize into a reading is pointless. When candidates
*are* configured but none of them is present in a given day's feed,
:meth:`normalize` returns no readings for that fetch and logs a clear,
credential-free warning naming the candidates that were tried — the
provider/store contract gives ``normalize`` no side channel for a
"reason" alongside its readings, so a log line is the honest way to surface
that gap without inventing a return shape the merged contract does not
define (see this task's final report for the full note).

Contract note — carrying the candidate list from ``build_requests`` to
``normalize``: :meth:`WeatherProvider.normalize` takes only a
``fetch_record``, and the merged :class:`~climate.weather.store.FetchRecord`
has no field for :class:`~climate.weather.providers.base.RequestSpec.context`
(context lives on the *request*, not the stored record) — so a setting that
only matters at normalize time, like which city to pick out of an
all-cities feed, cannot ride along on the fetch record any other way. This
adapter appends the configured candidates to the **fragment** of the
*stored* request URL (``...isr_cities.xml#cities=<urlencoded candidates>``),
never the query string. A location/city name is private data in this
project (see the repo's privacy rule), and a URL fragment is a purely
client-side annotation: ``urllib.request.Request`` (and every HTTP client)
strips it before building the wire request — ``Request(url).selector`` is
the path *without* the fragment, so it never reaches ``ims.gov.il``'s
access logs — while :class:`~climate.weather.store.FetchRecord` still
stores the full URL including the fragment as ``endpoint``, which is what
lets :meth:`ImsForecastProvider.normalize` read the candidates back and
stay pure/re-derivable from the one stored fetch record (spec ``h2``),
without editing the merged store or provider contracts. See
:func:`_candidate_cities`.

The raw response bytes are handed to :mod:`xml.etree.ElementTree` exactly as
received — never decoded/re-encoded by this module — so the ``ISO-8859-8``
encoding declared in the XML prolog is what actually parses the document.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET  # nosec B405 - see normalize(): stdlib-only, fixed gov feed
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from climate.weather.providers.base import (
    Attribution,
    AuthRequirement,
    Availability,
    Capability,
    FetchRecordLike,
    FreshnessStrategy,
    LocationLike,
    ProviderSettingsLike,
    Quota,
    ReadingLike,
    RequestSpec,
    WeatherProvider,
)
from climate.weather.store import Measurement, Reading

_ENDPOINT = "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml"

_LOGGER = logging.getLogger(__name__)

#: Element name -> (vocabulary id, unit), from ``docs/weather-api.md``
#: section 4 (``temperature_max``/``_min``, ``relative_humidity_max``/
#: ``_min``, ``weather_code``). Elements with no row in that table (e.g.
#: "Wind direction and speed") fall through to :func:`_extra_element_value`
#: instead, per the "no provider value is dropped" rule.
_ELEMENT_MAP: dict[str, tuple[str, str]] = {
    "Minimum temperature": ("temperature_min", "degC"),
    "Maximum temperature": ("temperature_max", "degC"),
    "Minimum relative humidity": ("relative_humidity_min", "percent"),
    "Maximum relative humidity": ("relative_humidity_max", "percent"),
}

#: "Weather code" is a coded value (vocabulary says its value type is a
#: string, not a number), and "Wind direction and speed" has no vocabulary
#: row at all and is not a single number either
#: (e.g. ``"270-45/15-25"`` = direction range / speed range). Both are
#: opaque provider codes rather than numbers.
_CODE_ELEMENTS: dict[str, str] = {
    "Weather code": "weather_code",
}


class ImsForecastProvider(WeatherProvider):
    """Keyless IMS daily city-forecast XML feed, normalized for one city."""

    id = "ims-forecast"
    capabilities = frozenset({Capability.FORECAST})
    auth = AuthRequirement(required=False)
    default_interval_seconds = 21600
    quota = Quota(
        unlimited=True,
        source="ims.gov.il keyless isr_cities.xml feed (no published rate limit)",
        notes="keyless, no credential, issued a few times a day",
    )
    freshness = FreshnessStrategy.INTERVAL
    attribution = Attribution(
        text="Weather forecast data by the Israel Meteorological Service (IMS)",
        url="https://ims.gov.il/en/termOfuse",
        licence="IMS Terms of Use",
    )

    def availability(
        self,
        settings: ProviderSettingsLike | None = None,
        env: Any = None,
    ) -> Availability:
        """Disabled unless explicitly ``enabled: true`` in provider settings.

        The base implementation treats a *missing* settings entry as
        enabled; this adapter needs the opposite default, so it is checked
        here before delegating to the base behaviour.
        """
        if settings is None or not getattr(settings, "enabled", False):
            return Availability(
                False,
                "ims-forecast is disabled until 'enabled: true' is set in its provider settings",
            )
        return super().availability(settings, env)

    def build_requests(
        self,
        location: LocationLike,
        settings: ProviderSettingsLike | None = None,
    ) -> tuple[RequestSpec, ...]:
        params = dict(getattr(settings, "params", None) or {})
        cities = [str(city).strip() for city in (params.get("cities") or []) if city]
        if not cities:
            return ()
        fragment = urlencode({"cities": ",".join(cities)})
        url = f"{_ENDPOINT}#{fragment}"
        return (
            RequestSpec(
                provider_id=self.id,
                location_label=location.label,
                url=url,
                purpose="isr_cities",
                context={"cities": tuple(cities)},
            ),
        )

    def normalize(self, fetch_record: FetchRecordLike) -> tuple[ReadingLike, ...]:
        status = getattr(fetch_record, "status", None)
        if status is None or status >= 300:
            return ()
        body = getattr(fetch_record, "body", b"")
        if not body:
            return ()
        try:
            # This task's brief requires stdlib-only (no defusedxml), and
            # the source is a single fixed government URL configured by the
            # adapter itself, not an attacker-controlled endpoint; billion-
            # laughs/entity-expansion risk is accepted for that reason.
            root = ET.fromstring(body)  # nosec B314
        except ET.ParseError:
            return ()

        cities = _candidate_cities(fetch_record)
        if not cities:
            _LOGGER.warning(
                "ims-forecast: fetch record %r carries no city candidates in its endpoint "
                "fragment; nothing was configured when this fetch was made",
                getattr(fetch_record, "endpoint", ""),
            )
            return ()

        matched = _match_city(root, cities)
        if matched is None:
            _LOGGER.warning(
                "ims-forecast: none of the configured city candidates %r were found "
                "in this feed's cities",
                cities,
            )
            return ()
        matched_name, location_element = matched

        location_label = getattr(fetch_record, "location", "")
        requested_at = fetch_record.requested_at
        readings: list[Reading] = []
        location_data = location_element.find("LocationData")
        if location_data is None:
            return ()
        for time_unit in location_data.findall("TimeUnitData"):
            date_text = time_unit.findtext("Date")
            observed_at = _parse_date(date_text)
            if observed_at is None:
                continue
            values = _extract_values(time_unit)
            if not values:
                continue
            readings.append(
                Reading(
                    provider=self.id,
                    source=f"ims-forecast/{matched_name}",
                    location=location_label,
                    observed_at=observed_at,
                    requested_at=requested_at,
                    kind="forecast",
                    values=values,
                )
            )
        return tuple(readings)


def _candidate_cities(fetch_record: FetchRecordLike) -> tuple[str, ...]:
    """The city candidates configured when this fetch's request was built.

    The stored :class:`~climate.weather.store.FetchRecord` has no field for
    :class:`~climate.weather.providers.base.RequestSpec.context`, so
    :meth:`ImsForecastProvider.build_requests` carries the candidates on the
    request URL's **fragment** (see the module docstring's contract note) —
    never the query string, because a city/location name is private data
    and a fragment is the one part of a URL no HTTP client ever transmits.
    Reading it back here keeps ``normalize`` re-derivable from the one
    stored fetch record alone. A record with no fragment (nothing
    configured when it was fetched, or an older/foreign record) yields no
    candidates, which ``normalize`` also treats as "no readings."
    """
    endpoint = getattr(fetch_record, "endpoint", "") or ""
    fragment = urlsplit(endpoint).fragment
    for name, value in parse_qsl(fragment):
        if name == "cities" and value:
            return tuple(city for city in value.split(",") if city)
    return ()


def _normalize_city_name(name: str) -> str:
    collapsed = re.sub(r"\s*-\s*", "-", name.strip())
    return re.sub(r"\s+", " ", collapsed).casefold()


def _match_city(root: ET.Element, candidates: tuple[str, ...]) -> tuple[str, ET.Element] | None:
    by_normalized: dict[str, tuple[str, ET.Element]] = {}
    for location_element in root.findall("Location"):
        meta = location_element.find("LocationMetaData")
        if meta is None:
            continue
        name = meta.findtext("LocationNameEng")
        if not name:
            continue
        by_normalized[_normalize_city_name(name)] = (name, location_element)

    for candidate in candidates:
        hit = by_normalized.get(_normalize_city_name(candidate))
        if hit is not None:
            return hit
    return None


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _extra_element_key(name: str) -> str:
    """``"Wind direction and speed"`` -> ``"x_wind_direction_and_speed"``."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return f"x_{slug}"


def _extract_values(time_unit: ET.Element) -> dict[str, Measurement]:
    """Every ``Element`` in one ``TimeUnitData``, so no value is dropped.

    Vocabulary-mapped numeric elements use their canonical id; "Weather
    code" is stored as a string (the vocabulary's declared value type for
    it); anything else — currently only "Wind direction and speed", a
    compound range string with no single numeric or vocabulary meaning —
    is still emitted, as an opaque ``x_<element>`` code.
    """
    values: dict[str, Measurement] = {}
    for element in time_unit.findall("Element"):
        name = element.findtext("ElementName")
        raw_value = element.findtext("ElementValue")
        if not name or raw_value is None:
            continue
        if name in _ELEMENT_MAP:
            key, unit = _ELEMENT_MAP[name]
            try:
                values[key] = Measurement(value=float(raw_value), unit=unit)
            except ValueError:
                continue
        elif name in _CODE_ELEMENTS:
            values[_CODE_ELEMENTS[name]] = Measurement(
                value=raw_value, unit="code", original_value=raw_value
            )
        else:
            values[_extra_element_key(name)] = Measurement(
                value=raw_value, unit="code", original_value=raw_value
            )
    return values
