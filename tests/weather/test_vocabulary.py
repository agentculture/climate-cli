"""The vocabulary module and ``docs/weather-api.md`` must stay identical.

``climate/weather/vocabulary.py`` is the executable copy of the contract's
section 4 ("Vocabulary") and section 4.1 ("Units") tables. They drifted once
already — the API kept its own 19-row allowlist against the document's 37,
so every variable the document added later (``showers``, ``snowfall``,
``cloud_cover_low``, ``uv_index_clear_sky``, …) was rejected on an explicit
request and hidden from the defaults even though the adapters were storing
it. This module parses the two markdown tables and asserts **set equality**
in both directions, so neither side can gain or lose a row alone again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from climate.weather import vocabulary

DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "weather-api.md"

_VARIABLE_ROW = re.compile(r"^\|\s*`([a-zA-Z0-9_]+)`\s*\|\s*`([a-zA-Z0-9_]+)`\s*\|")
_UNIT_ROW = re.compile(r"^\|\s*`([a-zA-Z0-9_]+)`\s*\|")


def _parse_vocabulary_and_units(text: str) -> tuple[dict[str, str], set[str]]:
    """``(variable_id -> unit_id, {unit_id, ...})`` from the two doc tables.

    Both are ordinary GitHub-flavoured markdown pipe tables; only rows whose
    first cell is a backticked id are data rows, so the header and the
    ``---`` separator are skipped without special-casing.
    """
    section4 = text.split("\n## 4. Vocabulary\n", 1)[1]
    variable_block, _, rest = section4.partition("\n### 4.1 Units\n")
    unit_block = rest.split("\n### 4.2 Kinds\n", 1)[0]

    variables: dict[str, str] = {}
    for line in variable_block.splitlines():
        match = _VARIABLE_ROW.match(line)
        if match:
            variables[match.group(1)] = match.group(2)

    units = {
        match.group(1)
        for match in (_UNIT_ROW.match(line) for line in unit_block.splitlines())
        if match
    }
    return variables, units


DOC_VARIABLES, DOC_UNITS = _parse_vocabulary_and_units(DOCS_PATH.read_text(encoding="utf-8"))


def test_the_parser_found_real_tables() -> None:
    """A canary: a heading rename must fail loudly, not empty the tables."""
    assert DOC_VARIABLES.get("temperature") == "degC"
    assert "degC" in DOC_UNITS
    assert len(DOC_VARIABLES) >= 30
    assert len(DOC_UNITS) >= 10


def test_no_variable_is_documented_but_missing_from_the_module() -> None:
    assert set(DOC_VARIABLES) - set(vocabulary.VARIABLES) == set()


def test_no_variable_is_in_the_module_but_undocumented() -> None:
    assert set(vocabulary.VARIABLES) - set(DOC_VARIABLES) == set()


def test_every_variable_carries_exactly_the_documented_unit() -> None:
    assert vocabulary.VARIABLES == DOC_VARIABLES


def test_the_unit_table_matches_exactly() -> None:
    assert set(vocabulary.UNITS) == DOC_UNITS


def test_every_variables_unit_is_itself_a_unit_row() -> None:
    assert set(vocabulary.VARIABLES.values()) <= set(vocabulary.UNITS)


def test_the_fallback_unit_is_a_unit_row() -> None:
    assert vocabulary.FALLBACK_UNIT in vocabulary.UNITS


@pytest.mark.parametrize("variable", sorted(DOC_VARIABLES))
def test_unit_for_answers_for_every_documented_variable(variable: str) -> None:
    assert vocabulary.unit_for(variable) == DOC_VARIABLES[variable]


def test_unit_for_is_none_outside_the_table() -> None:
    assert vocabulary.unit_for("x_snow_depth") is None


def test_unit_for_is_none_for_an_unknown_id() -> None:
    assert vocabulary.unit_for("not_a_variable") is None


@pytest.mark.parametrize("variable", ["x_snow_depth", "x_a", "x_TG"])
def test_extension_ids_are_recognised(variable: str) -> None:
    assert vocabulary.is_extension(variable) is True


@pytest.mark.parametrize("variable", ["x_", "temperature", "", "snow_depth", "ax_b"])
def test_non_extension_ids_are_not(variable: str) -> None:
    assert vocabulary.is_extension(variable) is False


def test_is_known_accepts_a_vocabulary_id() -> None:
    assert vocabulary.is_known("temperature") is True


def test_is_known_accepts_an_extension() -> None:
    assert vocabulary.is_known("x_snow_depth") is True


def test_is_known_rejects_an_invented_id() -> None:
    assert vocabulary.is_known("not_a_variable") is False


def test_is_known_rejects_a_bare_prefix() -> None:
    assert vocabulary.is_known("x_") is False


def test_the_vocabulary_module_imports_nothing_third_party() -> None:
    """The host CLI must stay importable with every third-party package
    blocked, so this module is stdlib-only — in fact import-free."""
    source = (
        Path(vocabulary.__file__).read_text(encoding="utf-8")
        if vocabulary.__file__
        else ""  # pragma: no cover - a namespace package would be a bug
    )
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]
    assert imports == []
