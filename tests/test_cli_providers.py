"""Tests for the ``climate providers`` host CLI verb.

``climate.cli._commands.providers`` is not registered in
``climate.cli.__init__`` (the integration task owns that wiring), so every
test here builds its own tiny argparse parser and calls
``providers.register(sub)`` directly, exactly as the brief requires.

Two fake adapters are registered onto ``climate.weather.providers``'s
``__path__`` for the duration of these tests (no real adapter module exists
in this worktree yet — five are being written in parallel) plus one live
guard test that runs the same "every row has attribution" check straight
over the real registry, so it starts guarding the merged adapters the moment
they land.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from climate.cli._commands import providers as providers_cmd
from climate.weather import providers as registry

# --- fake adapters shared by these tests -----------------------------------

_GOOD_ADAPTER = textwrap.dedent('''
    """A throwaway, complete adapter used only by test_cli_providers.py."""

    from climate.weather.providers.base import (
        Attribution,
        AuthRequirement,
        Capability,
        FreshnessStrategy,
        Quota,
        RequestSpec,
        WeatherProvider,
    )


    class GoodTestProvider(WeatherProvider):
        id = "good-test"
        capabilities = frozenset({Capability.CURRENT_OBSERVATION, Capability.FORECAST})
        auth = AuthRequirement(required=True, env_var="CLIMATE_GOOD_TEST_KEY")
        default_interval_seconds = 300
        quota = Quota(
            calls_per_minute=5,
            calls_per_day=100,
            calls_per_month=3000,
            verified=True,
            source="issue 5",
        )
        freshness = FreshnessStrategy.INTERVAL
        attribution = Attribution(
            text="Good Test data",
            url="https://example.invalid/good-test/licence",
            licence="CC BY 4.0",
        )

        def build_requests(self, location, settings=None):
            return (
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url="https://example.invalid/good-test",
                ),
            )

        def normalize(self, fetch_record):
            return ()
    ''')

# A second, keyless adapter with an *undeclared* quota (unlimited=True) so the
# --limits table has a row that legitimately has no numeric quota columns.
_UNLIMITED_ADAPTER = textwrap.dedent('''
    """A throwaway, keyless adapter used only by test_cli_providers.py."""

    from climate.weather.providers.base import (
        Attribution,
        AuthRequirement,
        Capability,
        FreshnessStrategy,
        Quota,
        RequestSpec,
        WeatherProvider,
    )


    class UnlimitedTestProvider(WeatherProvider):
        id = "unlimited-test"
        capabilities = frozenset({Capability.CURRENT_MODEL})
        auth = AuthRequirement(required=False)
        default_interval_seconds = 900
        quota = Quota(unlimited=True, source="provider docs")
        freshness = FreshnessStrategy.HTTP_EXPIRES
        attribution = Attribution(
            text="Unlimited Test data",
            url="https://example.invalid/unlimited-test/licence",
        )

        def build_requests(self, location, settings=None):
            return (
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url="https://example.invalid/unlimited-test",
                ),
            )

        def normalize(self, fetch_record):
            return ()
    ''')

# Registered on the registry but *missing its attribution* — proves the
# "a test fails when any registered adapter lacks a row or an attribution
# line" acceptance criterion actually fails when it should.
_MISSING_ATTRIBUTION_ADAPTER = textwrap.dedent('''
    """A throwaway adapter with a missing attribution (negative case)."""

    from climate.weather.providers.base import (
        AuthRequirement,
        Capability,
        FreshnessStrategy,
        Quota,
        RequestSpec,
        WeatherProvider,
    )


    class NoAttributionTestProvider(WeatherProvider):
        id = "no-attribution-test"
        capabilities = frozenset({Capability.CURRENT_OBSERVATION})
        auth = AuthRequirement(required=False)
        default_interval_seconds = 600
        quota = Quota(unlimited=True, source="test")
        freshness = FreshnessStrategy.INTERVAL
        attribution = None

        def build_requests(self, location, settings=None):
            return (
                RequestSpec(
                    provider_id=self.id,
                    location_label=location.label,
                    url="https://example.invalid/no-attribution-test",
                ),
            )

        def normalize(self, fetch_record):
            return ()
    ''')


def _install(tmp_path: Path, filename: str, source: str) -> None:
    (tmp_path / filename).write_text(source, encoding="utf-8")
    registry.__path__.append(str(tmp_path))
    registry.clear_cache()


def _uninstall(tmp_path: Path, module_name: str) -> None:
    if str(tmp_path) in registry.__path__:
        registry.__path__.remove(str(tmp_path))
    sys.modules.pop(f"climate.weather.providers.{module_name}", None)
    registry.clear_cache()


@pytest.fixture
def two_fake_providers(tmp_path: Path) -> Any:
    """Register two complete fake adapters (one keyed, one unlimited/keyless)."""
    _install(tmp_path, "good_test_adapter.py", _GOOD_ADAPTER)
    _install(tmp_path, "unlimited_test_adapter.py", _UNLIMITED_ADAPTER)
    try:
        yield
    finally:
        _uninstall(tmp_path, "good_test_adapter")
        _uninstall(tmp_path, "unlimited_test_adapter")


@pytest.fixture
def broken_provider(tmp_path: Path) -> Any:
    """Register one fake adapter that is missing its attribution."""
    _install(tmp_path, "no_attribution_test_adapter.py", _MISSING_ATTRIBUTION_ADAPTER)
    try:
        yield
    finally:
        _uninstall(tmp_path, "no_attribution_test_adapter")


@pytest.fixture(autouse=True)
def _isolated_weather_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the weather config at an empty, private tmp path for every test.

    Without this, `providers.py`'s `load_config()` call would read whatever
    real config happens to sit at the user's XDG path.
    """
    monkeypatch.setenv("CLIMATE_WEATHER_CONFIG_PATH", str(tmp_path / "weather.json"))


# --- local parser harness ---------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """A throwaway parser exercising only `providers.register(sub)`.

    `providers` is deliberately not wired into `climate.cli.__init__` by this
    task (the integration task owns that), so tests drive `register()`
    directly instead of going through `climate.cli.main`.
    """
    parser = argparse.ArgumentParser(prog="climate")
    sub = parser.add_subparsers(dest="command")
    providers_cmd.register(sub)
    return parser


def _run(argv: list[str]) -> tuple[int, argparse.Namespace]:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args), args


# --- bare noun: listing -----------------------------------------------------


def test_bare_providers_lists_every_registered_adapter(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "good-test" in out
    assert "unlimited-test" in out


def test_providers_table_reports_capabilities_auth_enabled_reason_freshness(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    # good-test requires a key that is never set in this test environment.
    assert "current-observation" in out
    assert "forecast" in out
    assert "required (CLIMATE_GOOD_TEST_KEY)" in out
    assert "no" in out  # good-test's enabled column
    assert "CLIMATE_GOOD_TEST_KEY" in out  # the disabled reason names the env var
    assert "interval" in out
    assert "http-expires" in out
    # unlimited-test has no required key, so it is enabled.
    assert "none" in out


def test_providers_table_includes_an_attribution_line_per_row(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Good Test data" in out
    assert "https://example.invalid/good-test/licence" in out
    assert "Unlimited Test data" in out
    assert "https://example.invalid/unlimited-test/licence" in out


def test_providers_table_is_markdown_by_default(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].startswith("|")
    assert set(lines[1].replace("|", "").strip()) <= {"-", " "}


def test_providers_never_prints_a_location_or_coordinate(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers", "--limits", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    for banned in ("latitude", "longitude", "location", "lat=", "lon="):
        assert banned not in out.lower()


def test_render_table_reports_plainly_with_no_rows() -> None:
    # A pure unit test of the renderer itself (not the live registry, which
    # may legitimately be non-empty by the time this test runs — real
    # adapters merge in parallel).
    assert providers_cmd.render_table([]) == "no providers registered"


def test_bare_providers_reports_plainly_when_registry_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(providers_cmd, "_rows", lambda: [])
    rc, _ = _run(["providers"])
    assert rc == 0
    assert "no providers registered" in capsys.readouterr().out


# --- --limits ----------------------------------------------------------------


def test_limits_flag_adds_quota_columns(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers", "--limits"])
    assert rc == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    for column in ("calls/min", "calls/day", "calls/month", "verified", "source"):
        assert column in header
    assert "100" in out  # good-test's calls_per_day
    assert "3000" in out  # good-test's calls_per_month
    assert "issue 5" in out  # good-test's quota source
    assert "provider docs" in out  # unlimited-test's quota source


def test_without_limits_flag_quota_columns_are_absent(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers"])
    assert rc == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "calls/day" not in header


# --- --json ------------------------------------------------------------------


def test_json_flag_emits_structured_rows(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["providers"]}
    assert {"good-test", "unlimited-test"} <= ids
    good = next(row for row in payload["providers"] if row["id"] == "good-test")
    assert good["auth"]["required"] is True
    assert good["auth"]["env_var"] == "CLIMATE_GOOD_TEST_KEY"
    assert good["enabled"] is False
    assert good["freshness"] == "interval"
    assert good["attribution"]["text"] == "Good Test data"
    assert good["quota"]["calls_per_day"] == 100


def test_json_flag_combines_with_limits(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(["providers", "--json", "--limits"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    good = next(row for row in payload["providers"] if row["id"] == "good-test")
    assert good["quota"]["calls_per_minute"] == 5


# --- enabled state from config -----------------------------------------------


def test_enabled_state_reflects_user_config_disabled_flag(
    two_fake_providers: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "weather.json"
    config_path.write_text(
        json.dumps({"providers": {"unlimited-test": {"enabled": False}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIMATE_WEATHER_CONFIG_PATH", str(config_path))
    rc, _ = _run(["providers", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    unlimited = next(row for row in payload["providers"] if row["id"] == "unlimited-test")
    assert unlimited["enabled"] is False
    assert "disabled in configuration" in (unlimited["reason"] or "")


def test_missing_config_file_is_tolerated_with_adapter_defaults(
    two_fake_providers: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLIMATE_WEATHER_CONFIG_PATH", str(tmp_path / "does-not-exist.json"))
    rc, _ = _run(["providers", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    unlimited = next(row for row in payload["providers"] if row["id"] == "unlimited-test")
    assert unlimited["enabled"] is True


def test_unreadable_config_file_is_tolerated_not_fatal(
    two_fake_providers: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "weather.json"
    config_path.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setenv("CLIMATE_WEATHER_CONFIG_PATH", str(config_path))
    rc, _ = _run(["providers", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["id"] for row in payload["providers"]} >= {"good-test", "unlimited-test"}


# --- overview ----------------------------------------------------------------


def test_providers_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(["providers", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# climate-cli providers" in out
    assert "no network" in out.lower() or "registry" in out.lower()


def test_providers_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(["providers", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "climate-cli providers"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


def test_providers_overview_never_prints_a_location_or_coordinate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, _ = _run(["providers", "overview"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    for banned in ("latitude", "longitude"):
        assert banned not in out


# --- the acceptance criterion: a test fails on a missing attribution --------


def test_every_row_carries_an_attribution_line_with_fake_adapters(
    two_fake_providers: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Positive case: two complete fake adapters each get a row and an
    attribution line, and none is reported as missing."""
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MISSING ATTRIBUTION" not in out
    for provider in registry.iter_providers():
        assert provider.id in out


def test_an_adapter_missing_attribution_is_caught(
    broken_provider: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative case: a registered adapter with attribution=None must show up
    as missing, proving this check actually fails when it should."""
    rc, _ = _run(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no-attribution-test" in out
    assert "MISSING ATTRIBUTION" in out


def test_every_row_has_a_nonblank_attribution_string(
    two_fake_providers: Any,
) -> None:
    """The row-building helper itself: every describe() row either carries a
    real attribution string or the sentinel that marks it missing — never a
    blank cell that a human or agent could mistake for "nothing to check"."""
    rows = providers_cmd._rows()
    assert rows
    for row in rows:
        cell = providers_cmd._attribution_cell(row.get("attribution"))
        assert cell.strip()


# --- the same check, run over the LIVE registry ------------------------------


def test_live_registry_every_adapter_has_a_row_and_an_attribution() -> None:
    """Guards the real registry directly (no fake adapters installed).

    Deliberately never pins the registry's size: adapters (open-meteo,
    met-no, openweather, ims, metar, ims-forecast) are merging in parallel,
    so this must hold whether the registry holds zero adapters or many. It
    runs the row + attribution check over whatever `iter_providers()`
    actually returns, and separately asserts that the check looked at every
    provider present (rendered-row count == provider count) — so an empty
    registry passes honestly because there was truly nothing to check, not
    because the loop below was silently skipped.
    """
    registry.clear_cache()
    live_providers = registry.iter_providers()

    rows = providers_cmd._rows()
    assert len(rows) == len(live_providers)  # the check ran over every provider present

    rows_by_id = {row["id"]: row for row in rows}
    problems: list[str] = []
    for provider in live_providers:
        row = rows_by_id.get(provider.id)
        if row is None:
            problems.append(f"{provider.id}: no row in `providers` output")
            continue
        attribution = row.get("attribution") or {}
        if not (attribution.get("text") or "").strip():
            problems.append(f"{provider.id}: attribution text is empty")
        if not (attribution.get("url") or "").strip():
            problems.append(f"{provider.id}: attribution url is empty")
        problems.extend(f"{provider.id}: {p}" for p in registry.validate_provider(provider))
    assert problems == []
