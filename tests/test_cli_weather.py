"""Tests for ``climate-cli weather`` — the host CLI's weather query verbs.

This noun is deliberately **not** registered in ``climate/cli/__init__.py``
(the integration task owns that registration); tests build a small local
parser with :func:`weather.register` instead, following the pattern in
``climate/cli/_commands/cli.py``. The local parser reuses the real
``_CliArgumentParser`` and ``_dispatch`` from :mod:`climate.cli` so parse
errors and ``CliError`` propagation behave exactly as they will once the
noun is wired into the real CLI.

No real network is touched anywhere here: ``tests/conftest.py`` blocks
sockets suite-wide, and every test that needs the API injects a fake
:func:`weather.fetch`.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from climate.cli import _CliArgumentParser, _dispatch
from climate.cli._commands import weather as weather_cmd
from climate.cli._errors import EXIT_ENV_ERROR, EXIT_STALE, EXIT_SUCCESS, EXIT_USER_ERROR
from tests.weather.neutral import NEUTRAL_LABEL

# --- test harness ------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(prog="climate-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    weather_cmd.register(sub)
    return parser


def _run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _dispatch(args)


def _reading(
    *,
    provider: str = "open-meteo",
    location: str = NEUTRAL_LABEL,
    age_seconds: int = 300,
    stale: bool = False,
    value: float = 21.5,
) -> dict:
    return {
        "provider": provider,
        "location": location,
        "kind": "model",
        "observed_at": "2026-09-17T08:30:00Z",
        "requested_at": "2026-09-17T08:35:00Z",
        "age_seconds": age_seconds,
        "stale": stale,
        "stale_after_seconds": 900,
        "fetch_id": "f_test",
        "provenance": {
            "provider": provider,
            "source": "v1/forecast",
            "model": None,
            "model_run_at": None,
            "station": None,
            "interval_seconds": 900,
            "fetch_id": "f_test",
            "schema_version": 1,
        },
        "values": {
            "temperature": {
                "value": value,
                "unit": "degC",
                "original_value": value,
                "original_unit": "degC",
                "observed_at": "2026-09-17T08:30:00Z",
                "requested_at": "2026-09-17T08:35:00Z",
                "age_seconds": age_seconds,
                "kind": "model",
                "stale": stale,
                "stale_after_seconds": 900,
                "quality": None,
                "provenance": {
                    "provider": provider,
                    "source": "v1/forecast",
                    "model": None,
                    "model_run_at": None,
                    "station": None,
                    "interval_seconds": 900,
                    "fetch_id": "f_test",
                    "schema_version": 1,
                },
            }
        },
    }


def _latest_payload(readings: list[dict], *, stale: bool = False, max_age_seconds=None) -> dict:
    return {
        "generated_at": "2026-09-17T08:35:12Z",
        "stale": stale,
        "max_age_seconds": max_age_seconds,
        "readings": readings,
        "missing": [],
        "warnings": [],
    }


def _fake_fetch(status: int, payload: dict | bytes):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def fetch(url: str, *, timeout: float = 10.0):
        return weather_cmd.ApiResult(status=status, body=body)

    return fetch


def _fake_fetch_error(message: str):
    def fetch(url: str, *, timeout: float = 10.0):
        return weather_cmd.ApiResult(error=message)

    return fetch


# --- overview / registration -------------------------------------------------


def test_bare_noun_defaults_to_overview(monkeypatch, capsys):
    rc = _run(["weather"])
    assert rc == EXIT_SUCCESS
    assert "# climate-cli weather" in capsys.readouterr().out


def test_overview_verb_text(capsys):
    rc = _run(["weather", "overview"])
    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "# climate-cli weather" in out
    assert "latest" in out
    assert "series" in out


def test_overview_verb_json(capsys):
    rc = _run(["weather", "overview", "--json"])
    assert rc == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "climate-cli weather"
    assert isinstance(payload["sections"], list)


def test_unknown_flag_is_structured_exit_1(capsys):
    with pytest.raises(SystemExit) as exc:
        _run(["weather", "latest", "--bogus"])
    assert exc.value.code == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- latest: fresh / stale / no-data / unreachable / bad flag ---------------


def test_latest_fresh_markdown_table(monkeypatch, capsys):
    payload = _latest_payload([_reading(age_seconds=100, stale=False)])
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest", "--location", NEUTRAL_LABEL])

    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "temperature" in out
    assert "degC" in out
    assert "100" in out
    assert "STALE" not in out


def test_latest_fresh_json_matches_payload_shape(monkeypatch, capsys):
    payload = _latest_payload([_reading(age_seconds=100, stale=False)])
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest", "--json"])

    assert rc == EXIT_SUCCESS
    out_payload = json.loads(capsys.readouterr().out)
    assert out_payload["stale"] is False
    assert out_payload["readings"][0]["provider"] == "open-meteo"
    assert "readings" in out_payload and "warnings" in out_payload


def test_latest_stale_from_api_flag_exits_3(monkeypatch, capsys):
    payload = _latest_payload([_reading(age_seconds=2000, stale=True)], stale=True)
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest"])

    assert rc == EXIT_STALE
    out = capsys.readouterr().out
    assert "STALE" in out


def test_latest_stale_enforced_locally_from_max_age(monkeypatch, capsys):
    # The API itself reports stale=false (its own default threshold hasn't
    # been exceeded), but the caller's --max-age is tighter: the CLI must
    # still enforce it locally from age_seconds and exit 3.
    payload = _latest_payload([_reading(age_seconds=120, stale=False)], stale=False)
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest", "--max-age", "60"])

    assert rc == EXIT_STALE


def test_latest_max_age_duration_suffix(monkeypatch, capsys):
    payload = _latest_payload([_reading(age_seconds=120, stale=False)], stale=False)
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest", "--max-age", "1m"])

    assert rc == EXIT_STALE


def test_latest_json_carries_stale_true_when_locally_exceeded(monkeypatch, capsys):
    payload = _latest_payload([_reading(age_seconds=120, stale=False)], stale=False)
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest", "--max-age", "60", "--json"])

    assert rc == EXIT_STALE
    out_payload = json.loads(capsys.readouterr().out)
    assert out_payload["stale"] is True


def test_latest_no_data_exits_0(monkeypatch, capsys):
    payload = _latest_payload([])
    payload["missing"] = [
        {"provider": "open-meteo", "location": NEUTRAL_LABEL, "reason": "no_data"}
    ]
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "latest"])

    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "No weather data available" in out


def test_latest_unreachable_exits_2_with_hint(monkeypatch, capsys):
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch_error("Connection refused"))

    rc = _run(["weather", "latest"])

    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert "climate stack status" in err


def test_latest_503_exits_2_distinct_from_stale(monkeypatch, capsys):
    monkeypatch.setattr(
        weather_cmd,
        "fetch",
        _fake_fetch(
            503,
            {"error": {"code": "store_unavailable", "message": "store down", "status": 503}},
        ),
    )

    rc = _run(["weather", "latest"])

    assert rc == EXIT_ENV_ERROR


def test_latest_bad_max_age_value_exits_1(monkeypatch, capsys):
    rc = _run(["weather", "latest", "--max-age", "notaduration"])

    assert rc == EXIT_USER_ERROR
    assert "error:" in capsys.readouterr().err


def test_latest_400_from_api_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(
        weather_cmd,
        "fetch",
        _fake_fetch(
            400,
            {
                "error": {
                    "code": "unknown_provider",
                    "message": "unknown provider 'bogus'",
                    "status": 400,
                }
            },
        ),
    )

    rc = _run(["weather", "latest", "--provider", "bogus"])

    assert rc == EXIT_USER_ERROR
    assert "unknown provider" in capsys.readouterr().err


def test_latest_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("CLIMATE_WEATHER_URL", "http://example-weather.internal:9000")
    seen = {}

    def fetch(url: str, *, timeout: float = 10.0):
        seen["url"] = url
        return weather_cmd.ApiResult(status=200, body=json.dumps(_latest_payload([])).encode())

    monkeypatch.setattr(weather_cmd, "fetch", fetch)
    _run(["weather", "latest"])
    assert seen["url"].startswith("http://example-weather.internal:9000/api/v1/latest")


def test_default_base_url_has_no_api_prefix():
    assert weather_cmd.DEFAULT_BASE_URL == "http://127.0.0.1:8095"
    assert not weather_cmd.DEFAULT_BASE_URL.endswith("/api/v1")


# --- series / forecast / stats: smoke coverage of the other verbs ----------


def test_series_requires_variable_flag():
    with pytest.raises(SystemExit) as exc:
        _run(["weather", "series"])
    assert exc.value.code == EXIT_USER_ERROR


def test_series_markdown_and_json(monkeypatch, capsys):
    payload = {
        "generated_at": "2026-09-17T08:35:12Z",
        "variable": "temperature",
        "unit": "degC",
        "from": "2026-09-17T07:00:00Z",
        "to": "2026-09-17T08:30:00Z",
        "step_seconds": 900,
        "agg": "last",
        "point_count": 2,
        "series": [
            {
                "provider": "open-meteo",
                "location": NEUTRAL_LABEL,
                "kind": "model",
                "unit": "degC",
                "source": "v1/forecast",
                "model": None,
                "station": None,
                "value_count": 2,
                "null_count": 0,
                "first_at": "2026-09-17T07:00:00Z",
                "last_at": "2026-09-17T08:15:00Z",
                "min": 23.9,
                "max": 25.4,
                "points": [],
            }
        ],
        "warnings": [],
    }
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "series", "--variable", "temperature"])
    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "open-meteo" in out
    assert "23.9" in out

    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))
    rc = _run(["weather", "series", "--variable", "temperature", "--json"])
    assert rc == EXIT_SUCCESS
    out_payload = json.loads(capsys.readouterr().out)
    assert out_payload["variable"] == "temperature"


def test_forecast_markdown_and_json(monkeypatch, capsys):
    payload = {
        "generated_at": "2026-09-17T08:35:12Z",
        "horizon_hours": 3,
        "step_hours": 1,
        "forecasts": [
            {
                "provider": "met-no",
                "location": NEUTRAL_LABEL,
                "kind": "forecast",
                "issued_at": "2026-09-17T08:00:00Z",
                "requested_at": "2026-09-17T08:20:03Z",
                "fetch_id": "f_test",
                "provenance": {},
                "variables": ["temperature"],
                "units": {"temperature": "degC"},
                "point_count": 3,
                "points": [],
            }
        ],
        "warnings": [],
    }
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "forecast"])
    assert rc == EXIT_SUCCESS
    assert "met-no" in capsys.readouterr().out

    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))
    rc = _run(["weather", "forecast", "--json"])
    assert rc == EXIT_SUCCESS
    out_payload = json.loads(capsys.readouterr().out)
    assert out_payload["forecasts"][0]["provider"] == "met-no"


def test_forecast_no_data(monkeypatch, capsys):
    payload = {
        "generated_at": "2026-09-17T08:35:12Z",
        "horizon_hours": 48,
        "step_hours": 1,
        "forecasts": [],
        "warnings": [],
    }
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))
    rc = _run(["weather", "forecast"])
    assert rc == EXIT_SUCCESS
    assert "No forecast data available" in capsys.readouterr().out


def test_stats_markdown_and_json(monkeypatch, capsys):
    payload = {
        "generated_at": "2026-09-17T08:35:12Z",
        "from": "2026-09-16T08:35:12Z",
        "to": "2026-09-17T08:35:12Z",
        "window_seconds": 86400,
        "bucket_seconds": None,
        "totals": {
            "due_count": 10,
            "stored_count": 9,
            "completeness": 0.9,
            "bytes_stored": 100,
        },
        "providers": [
            {
                "provider": "open-meteo",
                "location": NEUTRAL_LABEL,
                "enabled": True,
                "freshness_strategy": "interval",
                "interval_seconds": 900,
                "due_count": 10,
                "due_estimated": False,
                "stored_count": 9,
                "completeness": 0.9,
                "ok_count": 9,
                "not_modified_count": 0,
                "client_error_count": 0,
                "rate_limited_count": 0,
                "server_error_count": 0,
                "transport_error_count": 0,
                "reading_count": 9,
                "bytes_stored": 100,
                "first_fetch_at": None,
                "newest_fetch_at": None,
                "newest_fetch_age_seconds": None,
                "mean_interval_seconds": None,
                "longest_gap_seconds": None,
                "buckets": None,
            }
        ],
        "warnings": [],
    }
    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))

    rc = _run(["weather", "stats"])
    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "open-meteo" in out
    assert "0.9" in out

    monkeypatch.setattr(weather_cmd, "fetch", _fake_fetch(200, payload))
    rc = _run(["weather", "stats", "--json"])
    assert rc == EXIT_SUCCESS
    out_payload = json.loads(capsys.readouterr().out)
    assert out_payload["providers"][0]["provider"] == "open-meteo"


# --- module hygiene: standard library only -----------------------------------


def test_weather_module_imports_only_stdlib_and_climate_cli():
    """AC: ``climate/cli/_commands/weather.py`` imports only the standard
    library (plus ``climate.cli`` internals) — no third-party packages, and
    no other ``climate`` subpackage (it never talks to the store or MongoDB
    directly, only the HTTP API)."""
    spec = importlib.util.find_spec("climate.cli._commands.weather")
    assert spec is not None and spec.origin
    source = Path(spec.origin).read_text(encoding="utf-8")
    tree = ast.parse(source)
    stdlib_names = set(sys.stdlib_module_names)

    def _check(top: str, full: str) -> None:
        assert top in stdlib_names or top == "climate", f"non-stdlib import: {full}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check(alias.name.split(".")[0], alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level:  # relative import
                continue
            _check(node.module.split(".")[0], node.module)


def test_errors_module_imports_only_stdlib_with_third_party_blocked(monkeypatch):
    """``climate/cli/_errors.py`` must import only the standard library.

    Re-imports the module under a guard that raises on any import whose top
    package is neither in ``sys.stdlib_module_names`` nor ``climate``
    itself, proving the constraint by construction rather than by reading
    the source.
    """
    module_name = "climate.cli._errors"
    stdlib_names = set(sys.stdlib_module_names)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if level == 0 and top not in stdlib_names and top != "climate":
            raise ImportError(f"blocked third-party import inside {module_name}: {name}")
        return real_import(name, globals, locals, fromlist, level)

    # Keep the ORIGINAL module object and put it back afterwards: leaving a
    # fresh copy in sys.modules would give later-imported modules a different
    # CliError class than the one other tests already hold (seen as a flaky
    # ``pytest.raises(CliError)`` miss under pytest-xdist).
    original = sys.modules.pop(module_name, None)
    parent = sys.modules.get("climate.cli")
    original_attr = getattr(parent, "_errors", None) if parent else None
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    try:
        reloaded = importlib.import_module(module_name)
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        sys.modules.pop(module_name, None)
        if original is not None:
            sys.modules[module_name] = original
            if parent is not None and original_attr is not None:
                parent._errors = original_attr

    assert reloaded.EXIT_STALE == 3
    assert reloaded.EXIT_SUCCESS == 0
