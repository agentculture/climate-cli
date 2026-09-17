"""Repo-wide hygiene guards for the weather-tracking-service work.

Covers:
- proving the conftest.py socket guard actually blocks real connections
  with a clear message
- scanning climate/, tests/ (outside tests/fixtures/) and the docker files
  for hard-coded latitude/longitude literals, so the user's real location
  never lands in the package, the test suite or a container definition
- checking tests/fixtures/ carries the fixtures and README this task
  promises
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from tests.conftest import BLOCKED_CONNECT_MESSAGE, NetworkDisabledError

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()
FIXTURES_DIR = (REPO_ROOT / "tests" / "fixtures").resolve()

# A hard-coded coordinate literal: a lat/lon-ish keyword immediately assigned
# (or passed) a decimal-degree float, e.g. `lat=51.4769`, `latitude: -0.0005`,
# `LON = "34.7818"`. Deliberately keyword-anchored rather than "any float in
# range" to avoid false positives on ports, versions, ids and quotas. An exact
# zero (`lat=0.0`, `lon=0.00`) is exempt: it is a placeholder, not a place.
_COORD_RE = re.compile(
    r"\b(?:lat(?:itude)?|lon(?:gitude)?)\b\s*[:=]\s*[\"']?-?(?!0+\.0+(?!\d))\d{1,3}\.\d+",
    re.IGNORECASE,
)

# Directories under the repo root that are scanned for coordinate literals.
_SCANNED_DIRS = ("climate", "tests")

# Extra top-level files that count as "the docker files" for this scan.
_DOCKER_GLOBS = ("Dockerfile*", "docker-compose*.yml", "docker-compose*.yaml")


def _iter_scanned_files() -> list[Path]:
    files: list[Path] = []
    for dirname in _SCANNED_DIRS:
        base = REPO_ROOT / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if FIXTURES_DIR in path.resolve().parents or path.resolve() == FIXTURES_DIR:
                continue
            if path.resolve() == THIS_FILE:
                # This file's own docstring/regex mentions "lat"/"lon" near
                # example literals; it is the scanner, not scanned content.
                continue
            if path.suffix in {".pyc"} or "__pycache__" in path.parts:
                continue
            files.append(path)

    for pattern in _DOCKER_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file():
                files.append(path)
        for path in (REPO_ROOT / "docker").glob(pattern) if (REPO_ROOT / "docker").is_dir() else ():
            if path.is_file():
                files.append(path)

    return files


# --- socket guard -----------------------------------------------------------


def test_conftest_blocks_real_socket_connect() -> None:
    """A test that tries to open a real socket connection fails with a clear message."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkDisabledError) as excinfo:
            sock.connect(("example.invalid", 80))
        assert BLOCKED_CONNECT_MESSAGE in str(excinfo.value)
    finally:
        sock.close()


def test_conftest_blocks_create_connection() -> None:
    """The urllib/http.client-facing create_connection() helper is blocked too."""
    with pytest.raises(NetworkDisabledError) as excinfo:
        socket.create_connection(("example.invalid", 80))
    assert BLOCKED_CONNECT_MESSAGE in str(excinfo.value)


# --- coordinate hygiene -------------------------------------------------------


def test_no_hardcoded_coordinates_outside_fixtures() -> None:
    """climate/, tests/ (outside fixtures) and the docker files carry no lat/lon literal."""
    offenders: list[str] = []
    for path in _iter_scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _COORD_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert (
        not offenders
    ), "Hard-coded latitude/longitude literal(s) found outside tests/fixtures/:\n" + "\n".join(
        offenders
    )


def test_scan_actually_covers_climate_and_tests_dirs() -> None:
    """Guard the guard: the scan must look at real files, not silently scan nothing."""
    scanned = _iter_scanned_files()
    scanned_names = {p.name for p in scanned}
    assert "test_cli.py" in scanned_names, "the scan should see existing tests/ files"


def test_regex_detects_a_planted_coordinate_literal(tmp_path: Path) -> None:
    """Sanity-check the detector itself against a known-bad line."""
    sample = tmp_path / "sample.py"
    sample.write_text("latitude = 32.0853\nlongitude=34.7818\n", encoding="utf-8")
    text = sample.read_text(encoding="utf-8")
    matches = [line for line in text.splitlines() if _COORD_RE.search(line)]
    assert len(matches) == 2


def test_regex_ignores_exact_zero_placeholders() -> None:
    """`lat=0.0` is a placeholder, not a place; `lat=0.05` is still a place."""
    assert not _COORD_RE.search("https://example.test/p?lat=0.00&lon=0.0")
    assert not _COORD_RE.search("latitude: -0.000")
    assert _COORD_RE.search("lat=0.05")
    assert _COORD_RE.search("longitude = -0.0005")


# --- fixtures completeness ----------------------------------------------------


_EXPECTED_FIXTURES = (
    "open_meteo_forecast.json",
    "met_no_locationforecast.json",
    "met_no_locationforecast_headers.txt",
    "openweather_current.json",
    "ims_stations.json",
    "ims_latest.json",
    "metar_llbg.json",
    "ims_isr_cities.xml",
    "README.md",
)


@pytest.mark.parametrize("name", _EXPECTED_FIXTURES)
def test_fixture_present_and_non_empty(name: str) -> None:
    path = FIXTURES_DIR / name
    assert path.is_file(), f"missing fixture: {path}"
    assert path.stat().st_size > 0, f"empty fixture: {path}"


def test_ims_city_forecast_fixture_is_iso_8859_8_bytes() -> None:
    raw = (FIXTURES_DIR / "ims_isr_cities.xml").read_bytes()
    # Must be readable as ISO-8859-8 (never re-encoded to UTF-8/JSON).
    raw.decode("iso-8859-8")
    assert b'encoding="ISO-8859-8"' in raw


def test_met_no_headers_fixture_has_expires_and_last_modified() -> None:
    headers_text = (FIXTURES_DIR / "met_no_locationforecast_headers.txt").read_text(
        encoding="utf-8"
    )
    lowered = headers_text.lower()
    assert "expires:" in lowered
    assert "last-modified:" in lowered


def test_fixtures_readme_documents_synthesized_providers() -> None:
    readme = (FIXTURES_DIR / "README.md").read_text(encoding="utf-8")
    assert "openweather" in readme.lower()
    assert "synthesized" in readme.lower() or "synthesised" in readme.lower()
    assert "ims" in readme.lower()
