"""Tests for the weather-tracker checks added to ``climate-cli doctor``.

These exercise :func:`climate.cli._commands.doctor._weather_checks` directly
(the injectable-seam function: ``run``, ``fetch``, ``env``, ``now``,
``which``) plus ``cmd_doctor``'s composition of the identity + weather
groups. No check here ever opens a socket or calls docker/subprocess for
real — every seam is a fake.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from climate import __version__ as HOST_VERSION
from climate.cli import main
from climate.cli._commands import backup as backup_mod
from climate.cli._commands import doctor
from climate.cli._commands import stack as stack_mod

NOW = datetime(2026, 9, 17, 8, 35, 12, tzinfo=UTC)


def _fake_run_ok(services):
    """A fake subprocess runner for 'docker compose ... ps ...' returning services."""

    def _run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(services),
            stderr="",
        )

    return _run


def _fake_run_fail(returncode=1, stderr="Cannot connect to the Docker daemon"):
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return _run


def _health_payload(**overrides):
    payload = {
        "status": "ok",
        "generated_at": "2026-09-17T08:35:12Z",
        "version": HOST_VERSION,
        "tracker_version": HOST_VERSION,
        "store": {"reachable": True, "size_bytes": 742391808},
        "newest_fetch": {"requested_at": "2026-09-17T08:35:02Z", "age_seconds": 10},
        "providers": [
            {
                "provider": "open-meteo",
                "enabled": True,
                "newest_fetch_at": "2026-09-17T08:35:02Z",
                "newest_fetch_age_seconds": 10,
                "stale": False,
            },
        ],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def _fake_fetch_for(body: dict, status: int = 200):
    def _fetch(url, **kwargs):
        return SimpleNamespace(status=status, body=json.dumps(body).encode("utf-8"), error=None)

    return _fetch


def _fake_fetch_unreachable():
    def _fetch(url, **kwargs):
        return SimpleNamespace(status=None, body=b"", error="Connection refused")

    return _fetch


def _by_id(checks, check_id):
    for c in checks:
        if c["id"] == check_id:
            return c
    raise AssertionError(f"no check with id {check_id!r} in {[c['id'] for c in checks]}")


# --- wheel install / no compose file ---------------------------------------


def test_weather_checks_skip_when_no_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack_mod, "find_compose", lambda: None)
    checks = doctor._weather_checks(
        run=_fake_run_ok([]),
        fetch=_fake_fetch_unreachable(),
        env={},
        now=NOW,
        which=lambda name: None,
    )
    assert len(checks) == 1
    check = checks[0]
    assert check["passed"] is True
    assert check["severity"] == "info"
    assert "repo checkout" in check["message"]


# --- docker / stack ----------------------------------------------------------


def test_docker_missing_is_error_in_repo_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    checks = doctor._weather_checks(
        run=_fake_run_fail(),
        fetch=_fake_fetch_unreachable(),
        env={},
        now=NOW,
        which=lambda name: None,
    )
    docker_check = _by_id(checks, "weather_docker_available")
    assert docker_check["passed"] is False
    assert docker_check["severity"] == "error"
    assert docker_check["remediation"]


def test_stopped_stack_yields_failed_check_not_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    checks = doctor._weather_checks(
        run=_fake_run_ok([]),  # no services => stack not running
        fetch=_fake_fetch_unreachable(),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    stack_check = _by_id(checks, "weather_stack_running")
    assert stack_check["passed"] is False
    assert stack_check["severity"] == "error"
    assert "climate stack up" in stack_check["remediation"]


def test_stack_running_when_services_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [
        {"Name": "weather-mongodb", "Service": "weather-mongodb", "State": "running", "Health": ""},
        {"Name": "weather-tracker", "Service": "weather-tracker", "State": "running", "Health": ""},
    ]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    stack_check = _by_id(checks, "weather_stack_running")
    assert stack_check["passed"] is True


def test_compose_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)

    def _boom_run(cmd, **kwargs):
        raise RuntimeError("boom")

    checks = doctor._weather_checks(
        run=_boom_run,
        fetch=_fake_fetch_unreachable(),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    stack_check = _by_id(checks, "weather_stack_running")
    assert stack_check["passed"] is False
    assert "RuntimeError" in stack_check["message"]


# --- web API reachability + derived data ------------------------------------


def test_web_api_reachable_and_derived_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    api_check = _by_id(checks, "weather_web_api_reachable")
    assert api_check["passed"] is True

    fetch_age = _by_id(checks, "weather_fetch_age_open-meteo")
    assert fetch_age["passed"] is True

    size_check = _by_id(checks, "weather_database_size")
    assert size_check["passed"] is True
    assert "742391808" in size_check["message"]

    version_check = _by_id(checks, "weather_tracker_version")
    assert version_check["passed"] is True


def test_web_api_unreachable_yields_failed_checks_not_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_unreachable(),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    api_check = _by_id(checks, "weather_web_api_reachable")
    assert api_check["passed"] is False
    assert api_check["severity"] == "error"

    size_check = _by_id(checks, "weather_database_size")
    assert size_check["passed"] is False
    assert size_check["severity"] == "warning"

    version_check = _by_id(checks, "weather_tracker_version")
    assert version_check["passed"] is False


def test_tracker_version_skew_names_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload(tracker_version="0.0.1-old")),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    version_check = _by_id(checks, "weather_tracker_version")
    assert version_check["passed"] is False
    assert version_check["severity"] == "warning"
    assert "0.0.1-old" in version_check["message"]
    assert HOST_VERSION in version_check["message"]


def test_tracker_version_null_reports_no_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload(tracker_version=None)),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    version_check = _by_id(checks, "weather_tracker_version")
    assert version_check["passed"] is False
    assert version_check["severity"] == "warning"
    assert "tracker has not reported a heartbeat yet" in version_check["message"]


# --- provider credentials ----------------------------------------------------


def test_provider_credentials_warning_names_env_var_not_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    env = {}  # no credentials set anywhere
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env=env,
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    ow_check = _by_id(checks, "weather_provider_credentials_openweather")
    assert ow_check["passed"] is False
    assert ow_check["severity"] == "warning"
    assert "CLIMATE_OPENWEATHER_API_KEY" in ow_check["message"]

    ims_check = _by_id(checks, "weather_provider_credentials_ims")
    assert ims_check["passed"] is False
    assert "CLIMATE_IMS_API_TOKEN" in ims_check["message"]

    # a provider requiring no auth never gets a credential check
    assert not any(c["id"] == "weather_provider_credentials_open-meteo" for c in checks)


def test_provider_credentials_present_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    env = {
        "CLIMATE_OPENWEATHER_API_KEY": "secret-key-value",
        "CLIMATE_IMS_API_TOKEN": "secret-token-value",
    }
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env=env,
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    ow_check = _by_id(checks, "weather_provider_credentials_openweather")
    assert ow_check["passed"] is True
    # never print the secret value itself anywhere in the checks
    dumped = json.dumps(checks)
    assert "secret-key-value" not in dumped
    assert "secret-token-value" not in dumped


def test_fetch_age_for_disabled_provider_passes_as_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider disabled for lack of credentials must not fail its
    fetch-age check — no fetch is expected from a provider that never runs,
    so this is a passed ``info`` check, not a noisy failing ``warning``."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    payload = _health_payload(
        providers=[
            {
                "provider": "open-meteo",
                "enabled": True,
                "newest_fetch_at": "2026-09-17T08:35:02Z",
                "newest_fetch_age_seconds": 10,
                "stale": False,
            },
            {
                "provider": "openweather",
                "enabled": False,
                "newest_fetch_at": None,
                "newest_fetch_age_seconds": None,
                "stale": False,
            },
        ]
    )
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(payload),
        env={},  # no CLIMATE_OPENWEATHER_API_KEY set -> disabled, not a fetch problem
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    fetch_age = _by_id(checks, "weather_fetch_age_openweather")
    assert fetch_age["passed"] is True
    assert fetch_age["severity"] == "info"
    assert "disabled" in fetch_age["message"]

    # the still-enabled provider is unaffected.
    other_fetch_age = _by_id(checks, "weather_fetch_age_open-meteo")
    assert other_fetch_age["passed"] is True
    assert other_fetch_age["severity"] == "warning"

    # a distinct, deliberately noisier check keeps flagging the missing
    # credential itself — this fix only quiets the fetch-age check.
    cred_check = _by_id(checks, "weather_provider_credentials_openweather")
    assert cred_check["passed"] is False


# --- disk headroom + backup age ---------------------------------------------


def test_disk_headroom_warning_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    tiny_free = 1 * 1024**3  # 1 GiB, below the 10 GiB threshold

    def _fake_disk_usage(path):
        return SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=tiny_free)

    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
        disk_usage=_fake_disk_usage,
    )
    disk_check = _by_id(checks, "weather_disk_headroom")
    assert disk_check["passed"] is False
    assert disk_check["severity"] == "warning"


def test_backup_age_warning_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    monkeypatch.setattr(backup_mod, "newest_backup", lambda dir=None: None)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    backup_check = _by_id(checks, "weather_backup_age")
    assert backup_check["passed"] is False
    assert backup_check["severity"] == "warning"


def test_backup_age_warning_when_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    old_age = 10 * 86400  # 10 days
    monkeypatch.setattr(
        backup_mod, "newest_backup", lambda dir=None: (Path("/tmp/weather-x.archive.gz"), old_age)
    )
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    backup_check = _by_id(checks, "weather_backup_age")
    assert backup_check["passed"] is False


def test_backup_age_custom_threshold_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    age = 2 * 86400  # 2 days old
    monkeypatch.setattr(
        backup_mod, "newest_backup", lambda dir=None: (Path("/tmp/weather-x.archive.gz"), age)
    )
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={"CLIMATE_WEATHER_BACKUP_MAX_AGE_DAYS": "1"},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    backup_check = _by_id(checks, "weather_backup_age")
    assert backup_check["passed"] is False  # 2 days > the 1-day override


def test_backup_age_passes_when_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    age = 3600  # 1 hour old
    monkeypatch.setattr(
        backup_mod, "newest_backup", lambda dir=None: (Path("/tmp/weather-x.archive.gz"), age)
    )
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    backup_check = _by_id(checks, "weather_backup_age")
    assert backup_check["passed"] is True


# --- ids are namespaced, shape matches the rubric contract -------------------


def test_every_weather_check_id_prefixed_and_shaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    checks = doctor._weather_checks(
        run=_fake_run_ok(services),
        fetch=_fake_fetch_for(_health_payload()),
        env={},
        now=NOW,
        which=lambda name: "/usr/bin/docker",
    )
    assert checks
    for check in checks:
        assert check["id"].startswith("weather_")
        assert {"id", "passed", "severity", "message", "remediation"} <= set(check)
        assert check["severity"] in ("error", "warning", "info")


# --- cmd_doctor composition ---------------------------------------------------


def test_cmd_doctor_runs_weather_checks_even_with_no_culture_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "find_culture_yaml", lambda: None)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run_ok(services))
    monkeypatch.setattr(doctor.weather, "fetch", _fake_fetch_for(_health_payload()))

    rc = main(["doctor", "--json"])
    assert rc in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    ids = [c["id"] for c in payload["checks"]]
    assert "source_checkout" in ids
    assert any(i.startswith("weather_") for i in ids)


def test_cmd_doctor_text_mode_has_weather_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run_ok(services))
    monkeypatch.setattr(doctor.weather, "fetch", _fake_fetch_for(_health_payload()))

    rc = main(["doctor"])
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "weather" in out.lower()


def test_cmd_doctor_exit_1_when_stack_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run_ok([]))  # stack down
    monkeypatch.setattr(doctor.weather, "fetch", _fake_fetch_unreachable())

    rc = main(["doctor", "--json"])
    assert rc == 1
    capsys.readouterr()


def test_cmd_doctor_exit_0_when_only_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(stack_mod, "find_compose", lambda: compose)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    services = [{"Name": "n", "Service": "n", "State": "running", "Health": ""}]
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run_ok(services))
    # tracker_version mismatch -> warning only, and no credentials set -> warnings only
    monkeypatch.setattr(
        doctor.weather, "fetch", _fake_fetch_for(_health_payload(tracker_version="9.9.9"))
    )
    monkeypatch.setattr(backup_mod, "newest_backup", lambda dir=None: None)

    rc = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert all(
        c["passed"] or c["severity"] != "error" for c in payload["checks"]
    ), "an error-severity check failed unexpectedly"
    assert rc == 0
