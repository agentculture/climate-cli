"""Vocabulary/unit conformance suite for every registered provider adapter.

``climate.weather.vocabulary`` is the single source of truth for which
variable ids and unit ids an adapter's ``normalize()`` is allowed to emit.
This suite imports it rather than keeping a copy — and rather than parsing
``docs/weather-api.md``, which it used to do: the document and the code are
now held identical by ``tests/weather/test_vocabulary.py``, so importing the
module is importing the document, and the *runtime* table is what the API
actually validates against.

For every adapter returned by ``climate.weather.providers.iter_providers()``,
every ``Measurement`` in every ``Reading`` produced from that adapter's own
fixture (built the same way ``tests/weather/test_rederive.py`` builds a
``FetchRecord``, whose helpers this module imports and reuses) must satisfy:

* its variable id is either a row in the vocabulary table or starts with
  ``x_`` (the documented escape hatch for a provider value with no
  vocabulary row — never silently dropped, never silently invented under a
  vocabulary-looking id);
* its unit id is a row in the unit table;
* when the variable id *is* a vocabulary id, its unit is *exactly* the unit
  the table assigns that variable (no adapter may normalize a vocabulary
  variable into a unit the table does not say it uses);
* a value with unit ``degC`` lies within a physically plausible range for
  Earth's surface, ``-90 <= value <= 60`` — the bound that would have caught
  the OpenWeather Kelvin bug (285.32 "degC" instead of ~12.17 degC) before
  it shipped.

This test is expected to fail against a broken adapter today (OpenWeather
was emitting ``feels_like``/``humidity``/``pressure``/``wind_deg`` — not
vocabulary or ``x_``-prefixed ids — and units ``"C"``/``"%"``/``"m/s"``, not
table unit ids, with the fixture itself in Kelvin despite requesting
``units=metric``).
"""

from __future__ import annotations

import pytest

from climate.weather import vocabulary
from climate.weather.providers import iter_providers, provider_ids
from tests.weather.test_rederive import _CASES

VOCABULARY = vocabulary.VARIABLES
UNITS = vocabulary.UNITS

_TEMPERATURE_MIN_DEGC = -90.0
_TEMPERATURE_MAX_DEGC = 60.0


def test_the_vocabulary_table_is_something_real() -> None:
    """A canary against an empty or truncated table reaching the adapters."""
    assert VOCABULARY.get("temperature") == "degC"
    assert VOCABULARY.get("relative_humidity") == "percent"
    assert VOCABULARY.get("wind_speed") == "m_s"
    assert "degC" in UNITS
    assert "percent" in UNITS
    assert "m_s" in UNITS
    assert "other" in UNITS
    assert len(VOCABULARY) >= 30


def test_every_registered_adapter_is_covered_by_this_suite() -> None:
    assert set(_CASES) == set(provider_ids())


def _readings_for(provider_id: str, monkeypatch: pytest.MonkeyPatch) -> tuple[object, ...]:
    provider, record = _CASES[provider_id](monkeypatch)
    return tuple(provider.normalize(record))


@pytest.mark.parametrize("provider_id", sorted(provider_ids()))
def test_every_emitted_variable_id_is_in_vocabulary_or_x_prefixed(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = _readings_for(provider_id, monkeypatch)
    assert readings, f"{provider_id}: fixture produced no readings to check"
    for reading in readings:
        for variable_id in reading.values:
            assert variable_id in VOCABULARY or variable_id.startswith("x_"), (
                f"{provider_id}: variable id {variable_id!r} is neither a vocabulary id "
                "nor x_-prefixed (climate.weather.vocabulary / docs section 4)"
            )


@pytest.mark.parametrize("provider_id", sorted(provider_ids()))
def test_every_emitted_unit_id_is_in_the_unit_table(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = _readings_for(provider_id, monkeypatch)
    for reading in readings:
        for variable_id, measurement in reading.values.items():
            assert measurement.unit in UNITS, (
                f"{provider_id}: variable {variable_id!r} carries unit "
                f"{measurement.unit!r}, which is not a climate.weather.vocabulary unit id"
            )


@pytest.mark.parametrize("provider_id", sorted(provider_ids()))
def test_vocabulary_variables_carry_exactly_the_documented_unit(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = _readings_for(provider_id, monkeypatch)
    for reading in readings:
        for variable_id, measurement in reading.values.items():
            expected_unit = VOCABULARY.get(variable_id)
            if expected_unit is None:
                continue  # an x_ variable: no fixed unit assignment to check
            assert measurement.unit == expected_unit, (
                f"{provider_id}: vocabulary variable {variable_id!r} carries unit "
                f"{measurement.unit!r}, but climate.weather.vocabulary assigns it "
                f"{expected_unit!r}"
            )


@pytest.mark.parametrize("provider_id", sorted(provider_ids()))
def test_degc_values_are_within_a_physically_plausible_range(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would have caught the OpenWeather Kelvin bug (285.32 tagged "degC")."""
    readings = _readings_for(provider_id, monkeypatch)
    for reading in readings:
        for variable_id, measurement in reading.values.items():
            if measurement.unit != "degC":
                continue
            value = measurement.value
            if not isinstance(value, (int, float)):
                continue
            assert _TEMPERATURE_MIN_DEGC <= value <= _TEMPERATURE_MAX_DEGC, (
                f"{provider_id}: {variable_id!r} = {value!r} degC is outside "
                f"[{_TEMPERATURE_MIN_DEGC}, {_TEMPERATURE_MAX_DEGC}] -- looks unconverted "
                "(e.g. Kelvin mislabeled as degC)"
            )


def test_iter_providers_is_non_empty() -> None:
    """Sanity: the registry actually discovered adapters, or the suite above is vacuous."""
    assert len(iter_providers()) >= 5
