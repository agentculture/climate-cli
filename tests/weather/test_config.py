"""Tests for climate.weather.config — user configuration, locations, quota.

Uses a neutral public reference point (Greenwich Observatory, 51.48, -0.00)
for every test coordinate; never the user's real location.
"""

from __future__ import annotations

import json

import pytest

from climate.cli._errors import CliError
from climate.weather import config as weather_config

GREENWICH_LAT = 51.4800001
GREENWICH_LON = -0.0000001


def _write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _base_config(**overrides):
    data = {
        "locations": {
            "greenwich": {"latitude": GREENWICH_LAT, "longitude": GREENWICH_LON},
        },
        "providers": {
            "open-meteo": {
                "enabled": True,
                "interval_seconds": 900,
                "request_params": {"forecast_hours": 48},
                "quota": {"calls_per_day": 10000},
            }
        },
    }
    data.update(overrides)
    return data


class TestConfigPathResolution:
    def test_env_var_overrides_default_path(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "custom" / "weather.json"
        _write_config(cfg_path, _base_config())
        monkeypatch.setenv(weather_config.CONFIG_PATH_ENV_VAR, str(cfg_path))

        resolved = weather_config.resolve_config_path()

        assert resolved == cfg_path

    def test_default_path_is_outside_the_repo_under_xdg_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv(weather_config.CONFIG_PATH_ENV_VAR, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconf"))

        resolved = weather_config.default_config_path()

        assert resolved == tmp_path / "xdgconf" / "climate-cli" / "weather.json"
        # Must never resolve inside this repository checkout.
        import climate

        repo_root = weather_config.Path(climate.__file__).resolve().parents[1]
        assert not str(resolved).startswith(str(repo_root))

    def test_explicit_path_argument_wins_over_env_var(self, tmp_path, monkeypatch):
        env_path = tmp_path / "from_env.json"
        explicit_path = tmp_path / "explicit.json"
        _write_config(env_path, _base_config())
        _write_config(explicit_path, _base_config())
        monkeypatch.setenv(weather_config.CONFIG_PATH_ENV_VAR, str(env_path))

        resolved = weather_config.resolve_config_path(explicit_path)

        assert resolved == explicit_path


class TestLoadConfig:
    def test_loads_labelled_locations_and_provider_settings(self, tmp_path):
        cfg_path = _write_config(tmp_path / "weather.json", _base_config())

        config = weather_config.load_config(cfg_path)

        assert set(config.locations) == {"greenwich"}
        location = config.locations["greenwich"]
        assert location.label == "greenwich"

        provider = config.providers["open-meteo"]
        assert provider.provider_id == "open-meteo"
        assert provider.enabled is True
        assert provider.interval_seconds == 900
        assert provider.request_params == {"forecast_hours": 48}
        assert provider.quota.calls_per_day == 10000

    def test_missing_config_file_yields_empty_but_typed_config(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.json"

        config = weather_config.load_config(missing_path)

        assert config.locations == {}
        assert config.providers == {}


class TestNoLocationConfigured:
    def test_load_tracker_config_raises_env_error_with_remediation(self, tmp_path):
        cfg_path = _write_config(
            tmp_path / "weather.json",
            {"locations": {}, "providers": {}},
        )

        with pytest.raises(CliError) as exc_info:
            weather_config.load_tracker_config(cfg_path)

        error = exc_info.value
        assert error.code == 2
        assert error.remediation

    def test_missing_file_also_raises_for_the_tracker(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.json"

        with pytest.raises(CliError) as exc_info:
            weather_config.load_tracker_config(missing_path)

        assert exc_info.value.code == 2

    def test_no_default_place_exists_anywhere_in_the_package(self):
        import ast
        import pathlib

        package_root = pathlib.Path(weather_config.__file__).resolve().parent
        offenders = []
        for py_file in package_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    # A latitude/longitude-shaped float literal (has a
                    # fractional component and lies in a plausible range)
                    # would indicate a hard-coded default place.
                    value = node.value
                    if -180.0 <= value <= 180.0 and value not in (0.0,):
                        offenders.append((py_file, value))
        assert offenders == [], f"possible hard-coded coordinate literals: {offenders}"


class TestCoordinateRounding:
    def test_default_precision_is_two_decimals(self, tmp_path):
        cfg_path = _write_config(tmp_path / "weather.json", _base_config())

        config = weather_config.load_config(cfg_path)

        location = config.locations["greenwich"]
        assert location.latitude == 51.48
        assert location.longitude == -0.0

    def test_precision_is_configurable(self, tmp_path):
        data = _base_config(coordinate_precision=3)
        cfg_path = _write_config(tmp_path / "weather.json", data)

        config = weather_config.load_config(cfg_path)

        location = config.locations["greenwich"]
        assert location.latitude == round(GREENWICH_LAT, 3)

    def test_precision_is_never_more_than_four_decimals(self, tmp_path):
        data = _base_config(coordinate_precision=8)
        cfg_path = _write_config(tmp_path / "weather.json", data)

        config = weather_config.load_config(cfg_path)

        location = config.locations["greenwich"]
        assert location.latitude == round(GREENWICH_LAT, 4)

    def test_round_coordinate_clamps_precision_directly(self):
        assert weather_config.round_coordinate(GREENWICH_LON, 8) == round(GREENWICH_LON, 4)
        assert weather_config.round_coordinate(GREENWICH_LAT, 2) == 51.48

    def test_rounding_happens_before_any_other_module_sees_it(self, tmp_path):
        # load_config is the single seam other modules go through; the
        # returned Location must already carry rounded values, never the
        # raw, higher-precision input.
        cfg_path = _write_config(tmp_path / "weather.json", _base_config())

        config = weather_config.load_config(cfg_path)

        location = config.locations["greenwich"]
        assert location.latitude != GREENWICH_LAT
        assert location.longitude != GREENWICH_LON


class TestQuotaValidation:
    def test_locations_times_interval_within_quota_is_accepted(self, tmp_path):
        data = _base_config()
        data["providers"]["open-meteo"]["quota"]["calls_per_day"] = 10000
        cfg_path = _write_config(tmp_path / "weather.json", data)

        config = weather_config.load_config(cfg_path)

        assert config.providers["open-meteo"].quota.calls_per_day == 10000

    def test_locations_times_interval_exceeding_quota_is_rejected(self, tmp_path):
        data = _base_config(
            locations={
                "greenwich": {"latitude": GREENWICH_LAT, "longitude": GREENWICH_LON},
                "second": {"latitude": GREENWICH_LAT, "longitude": GREENWICH_LON},
            }
        )
        # Every-tick fetch (5 minutes) x 2 locations x a day = 576 calls/day,
        # far above a deliberately tiny quota of 10.
        data["providers"]["open-meteo"]["interval_seconds"] = 300
        data["providers"]["open-meteo"]["quota"]["calls_per_day"] = 10
        cfg_path = _write_config(tmp_path / "weather.json", data)

        with pytest.raises(CliError) as exc_info:
            weather_config.load_config(cfg_path)

        assert "open-meteo" in exc_info.value.message

    def test_quota_error_names_the_offending_provider_among_several(self, tmp_path):
        data = _base_config(
            providers={
                "open-meteo": {
                    "enabled": True,
                    "interval_seconds": 900,
                    "request_params": {},
                    "quota": {"calls_per_day": 10000},
                },
                "met-no": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "request_params": {},
                    "quota": {"calls_per_day": 5},
                },
            }
        )
        cfg_path = _write_config(tmp_path / "weather.json", data)

        with pytest.raises(CliError) as exc_info:
            weather_config.load_config(cfg_path)

        assert "met-no" in exc_info.value.message
        assert "open-meteo" not in exc_info.value.message

    def test_no_quota_configured_means_no_limit_check(self, tmp_path):
        data = _base_config()
        data["providers"]["open-meteo"]["quota"] = {}
        data["providers"]["open-meteo"]["interval_seconds"] = 1
        cfg_path = _write_config(tmp_path / "weather.json", data)

        config = weather_config.load_config(cfg_path)

        assert config.providers["open-meteo"].quota.calls_per_day is None
