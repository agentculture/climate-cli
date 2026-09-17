"""Tests for ``climate-cli stack`` (up/down/status/overview).

Per the plan brief, the integration task owns wiring ``stack`` into
``climate/cli/__init__.py`` — this file tests the noun through a *local*
argparse parser built here, exercising ``climate.cli._commands.stack``
directly. Docker is always mocked: ``stack.subprocess.run`` is monkeypatched
so no test ever spawns a real docker process.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from climate.cli._commands import stack
from climate.cli._errors import CliError
from climate.cli._output import emit_error

_PS_RUNNING = json.dumps(
    [
        {
            "Name": "weather-mongodb",
            "Service": "weather-mongodb",
            "State": "running",
            "Health": "healthy",
            "Status": "Up 2 minutes",
        },
        {
            "Name": "weather-tracker",
            "Service": "weather-tracker",
            "State": "running",
            "Health": "",
            "Status": "Up 2 minutes",
        },
    ]
)


def _fake_run_ok(cmd, capture_output=False, text=False, check=False):  # noqa: ANN001
    stdout = _PS_RUNNING if "ps" in cmd else ""
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


def _fake_run_daemon_down(cmd, capture_output=False, text=False, check=False):  # noqa: ANN001
    return subprocess.CompletedProcess(
        cmd,
        1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )


def _fake_run_no_compose_plugin(cmd, capture_output=False, text=False, check=False):  # noqa: ANN001
    return subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="docker: 'compose' is not a docker command."
    )


@pytest.fixture
def docker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack.shutil, "which", lambda _name: "/usr/bin/docker")


@pytest.fixture
def fake_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point find_compose() at a throwaway compose file under tmp_path, with
    a sibling docker/weather.env present (the happy-path env-file case).
    """
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("name: climate-weather\n", encoding="utf-8")
    env_dir = tmp_path / "docker"
    env_dir.mkdir()
    (env_dir / "weather.env").write_text("WEATHER_MONGO_URI=x\n", encoding="utf-8")
    monkeypatch.setattr(stack, "find_compose", lambda: compose)
    return compose


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="climate-cli")
    sub = parser.add_subparsers(dest="command")
    stack.register(sub)
    return parser


def _run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    try:
        rc = args.func(args)
    except CliError as err:
        emit_error(err, json_mode=json_mode)
        return err.code
    return rc if rc is not None else 0


# --- overview (no docker needed) -------------------------------------------


def test_stack_no_verb_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["stack"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# climate-cli stack" in out
    assert "climate-weather" in out


def test_stack_overview_verb_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["stack", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "climate-cli stack"
    assert isinstance(payload["sections"], list)


# --- status / up / down (docker mocked) -------------------------------------


def test_stack_status_json(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack.subprocess, "run", _fake_run_ok)
    rc = _run(["stack", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "status"
    assert payload["healthy"] is True
    assert {s["name"] for s in payload["services"]} == {"weather-mongodb", "weather-tracker"}


def test_stack_status_markdown_default(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack.subprocess, "run", _fake_run_ok)
    rc = _run(["stack", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("## stack status")
    assert "| weather-mongodb | running | healthy |" in out


def test_stack_up_requires_env_file(
    docker_present: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("name: climate-weather\n", encoding="utf-8")
    monkeypatch.setattr(stack, "find_compose", lambda: compose)  # no docker/weather.env

    def _fail_if_called(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("docker compose must not run when weather.env is missing")

    monkeypatch.setattr(stack.subprocess, "run", _fail_if_called)
    rc = _run(["stack", "up", "--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 2
    assert "weather.env" in payload["message"]
    assert "weather.env.example" in payload["remediation"]


def test_stack_up_runs_build_and_reports_status(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def _record(cmd, capture_output=False, text=False, check=False):  # noqa: ANN001
        calls.append(cmd)
        return _fake_run_ok(cmd, capture_output=capture_output, text=text, check=check)

    monkeypatch.setattr(stack.subprocess, "run", _record)
    rc = _run(["stack", "up", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["command"] == "up"
    assert "bringing up" in captured.err

    up_calls = [c for c in calls if "up" in c]
    assert up_calls, "expected a 'docker compose ... up' invocation"
    up_cmd = up_calls[0]
    assert up_cmd[:2] == ["docker", "compose"]
    assert "-d" in up_cmd
    assert "--build" in up_cmd


def test_stack_down_never_touches_volumes(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def _record(cmd, capture_output=False, text=False, check=False):  # noqa: ANN001
        calls.append(cmd)
        return _fake_run_ok(cmd, capture_output=capture_output, text=text, check=check)

    monkeypatch.setattr(stack.subprocess, "run", _record)
    rc = _run(["stack", "down", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "down"
    assert payload["running"] is False

    down_calls = [c for c in calls if "down" in c]
    assert down_calls
    for cmd in down_calls:
        assert "-v" not in cmd
        assert "--volumes" not in cmd


def test_stack_module_issues_no_volume_removing_command() -> None:
    """No argv this module builds may ever remove the named volume."""
    text = Path(stack.__file__).read_text(encoding="utf-8")
    forbidden = ("--volumes", "volume rm", "volume prune")
    for pattern in forbidden:
        assert pattern not in text, f"stack.py must never contain {pattern!r}"
    # "down -v" specifically (space-joined argv) must not appear either.
    assert "down -v" not in text


# --- environment-error paths (no-traceback contract) ------------------------


def test_docker_absent_exits_2_with_hint(
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack.shutil, "which", lambda _name: None)
    rc = _run(["stack", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_compose_file_missing_exits_2(
    docker_present: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack, "find_compose", lambda: None)
    rc = _run(["stack", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "Traceback" not in err


def test_daemon_unreachable_gives_specific_hint(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack.subprocess, "run", _fake_run_daemon_down)
    rc = _run(["stack", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "daemon" in err.lower()
    assert "Traceback" not in err


def test_compose_plugin_missing_gives_specific_hint(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stack.subprocess, "run", _fake_run_no_compose_plugin)
    rc = _run(["stack", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "compose plugin" in err.lower()
    assert "Traceback" not in err


def test_docker_vanishes_midrun_exits_2(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(*_a, **_k):  # noqa: ANN002, ANN003
        raise FileNotFoundError("docker")

    monkeypatch.setattr(stack.subprocess, "run", _raise)
    rc = _run(["stack", "status"])
    assert rc == 2
    assert "hint:" in capsys.readouterr().err


# --- _parse_ps robustness ----------------------------------------------------


def test_parse_ps_line_delimited() -> None:
    text = "\n".join(
        [
            json.dumps({"Name": "a", "Service": "a", "State": "running", "Health": "healthy"}),
            json.dumps({"Name": "b", "Service": "b", "State": "exited", "Health": ""}),
        ]
    )
    services = stack._parse_ps(text)
    assert [s["name"] for s in services] == ["a", "b"]


def test_parse_ps_empty() -> None:
    assert stack._parse_ps("") == []
    assert stack._parse_ps("   \n  ") == []


def test_compose_gets_the_env_file_for_interpolation_when_it_exists(tmp_path):
    """Compose interpolates ${CLIMATE_WEB_BIND} etc. from its project env file,
    not from a service's env_file, so the verbs must pass the same file."""
    from climate.cli._commands import stack as stack_module

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    assert stack_module._env_file_args(compose) == []
    (tmp_path / "docker").mkdir()
    env_file = tmp_path / "docker" / "weather.env"
    env_file.write_text("CLIMATE_WEB_BIND=127.0.0.1\n", encoding="utf-8")
    assert stack_module._env_file_args(compose) == ["--env-file", str(env_file)]
