"""Tests for ``climate-cli backup`` (dump/list/restore/overview).

Per the plan brief, the integration task owns wiring ``backup`` into
``climate/cli/__init__.py`` — this file tests the noun through a *local*
argparse parser built here, exercising ``climate.cli._commands.backup``
directly. Docker is always mocked: ``backup.subprocess.run`` is
monkeypatched so no test ever spawns a real docker process.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from climate.cli._commands import backup
from climate.cli._errors import CliError
from climate.cli._output import emit_error

_FAKE_ARCHIVE = gzip.compress(b"fake mongodump archive bytes")


def _fake_run_ok(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
    if "mongodump" in cmd:
        stdout = _FAKE_ARCHIVE
    elif "mongosh" in cmd:
        stdout = b"0\n"
    else:
        stdout = b""
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")


def _fake_run_daemon_down(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
    return subprocess.CompletedProcess(
        cmd,
        1,
        stdout=b"",
        stderr=b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )


@pytest.fixture
def docker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup.stack.shutil, "which", lambda _name: "/usr/bin/docker")


@pytest.fixture
def fake_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("name: climate-weather\n", encoding="utf-8")
    monkeypatch.setattr(backup.stack, "find_compose", lambda: compose)
    return compose


@pytest.fixture
def backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A backup dir that is not inside the (fake) repo checkout."""
    outside = tmp_path.parent / f"{tmp_path.name}-backups"
    outside.mkdir(exist_ok=True)
    monkeypatch.delenv(backup._BACKUP_DIR_ENV, raising=False)
    return outside


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="climate-cli")
    sub = parser.add_subparsers(dest="command")
    backup.register(sub)
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


def test_backup_no_verb_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["backup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# climate-cli backup" in out


def test_backup_overview_verb_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["backup", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "climate-cli backup"
    assert isinstance(payload["sections"], list)


# --- dump --------------------------------------------------------------


def test_backup_dump_writes_archive_and_reports_sha256(
    docker_present: None,
    fake_compose: Path,
    backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def _record(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
        calls.append(cmd)
        return _fake_run_ok(cmd, input=input, capture_output=capture_output, check=check)

    monkeypatch.setattr(backup.subprocess, "run", _record)
    rc = _run(["backup", "dump", "--json", "--dir", str(backup_dir)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "dump"

    dest = Path(payload["path"])
    assert dest.parent == backup_dir
    assert dest.name.startswith("weather-")
    assert dest.name.endswith(".archive.gz")
    assert dest.read_bytes() == _FAKE_ARCHIVE
    assert payload["size"] == dest.stat().st_size
    assert payload["sha256"] == hashlib.sha256(_FAKE_ARCHIVE).hexdigest()

    dump_calls = [c for c in calls if "mongodump" in c]
    assert len(dump_calls) == 1
    cmd = dump_calls[0]
    assert cmd[:6] == ["docker", "compose", "-f", str(fake_compose), "exec", "-T"]
    assert "weather-mongodb" in cmd
    assert cmd[cmd.index("weather-mongodb") + 1 :] == [
        "mongodump",
        "--db",
        "weather",
        "--archive",
        "--gzip",
    ]

    # No temp file left behind.
    leftovers = [p for p in backup_dir.iterdir() if p.name.startswith(".backup-tmp-")]
    assert not leftovers


def test_backup_dump_refuses_dir_inside_repo(
    docker_present: None,
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _fail_if_called(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("docker must not run when the backup dir is refused")

    monkeypatch.setattr(backup.subprocess, "run", _fail_if_called)
    inside = fake_compose.parent / "backups"
    rc = _run(["backup", "dump", "--json", "--dir", str(inside)])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert "inside the repo checkout" in payload["message"]


def test_backup_dump_default_dir_from_env(
    docker_present: None,
    fake_compose: Path,
    backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(backup._BACKUP_DIR_ENV, str(backup_dir))
    monkeypatch.setattr(backup.subprocess, "run", _fake_run_ok)
    rc = _run(["backup", "dump", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["path"]).parent == backup_dir


# --- list ----------------------------------------------------------------


def test_backup_list_newest_first_json(
    backup_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = backup_dir / "weather-20260101T000000Z.archive.gz"
    old.write_bytes(b"old")
    new = backup_dir / "weather-20260102T000000Z.archive.gz"
    new.write_bytes(b"newer-data")
    now = time.time()
    import os

    os.utime(old, (now - 1000, now - 1000))
    os.utime(new, (now - 10, now - 10))

    rc = _run(["backup", "list", "--json", "--dir", str(backup_dir)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "list"
    paths = [b["path"] for b in payload["backups"]]
    assert paths == [str(new), str(old)]
    assert payload["backups"][0]["size"] == len(b"newer-data")
    assert payload["backups"][0]["age_seconds"] < payload["backups"][1]["age_seconds"]


def test_backup_list_empty_markdown(backup_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["backup", "list", "--dir", str(backup_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no backups found" in out


def test_newest_backup_helper(backup_dir: Path) -> None:
    assert backup.newest_backup(dir=backup_dir) is None
    f = backup_dir / "weather-20260101T000000Z.archive.gz"
    f.write_bytes(b"x")
    result = backup.newest_backup(dir=backup_dir)
    assert result is not None
    path, age = result
    assert path == f
    assert age >= 0


# --- restore ---------------------------------------------------------------


def test_backup_restore_requires_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "weather-x.archive.gz"
    archive.write_bytes(_FAKE_ARCHIVE)
    rc = _run(["backup", "restore", str(archive), "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert "--yes" in payload["message"]


def test_backup_restore_missing_archive_file(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["backup", "restore", "/no/such/archive.gz", "--yes", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "not found" in payload["message"]


def test_backup_restore_refuses_nonempty_without_force(
    docker_present: None,
    fake_compose: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "weather-x.archive.gz"
    archive.write_bytes(_FAKE_ARCHIVE)

    def _fake_run_nonempty(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
        if "mongosh" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"42\n", stderr=b"")
        raise AssertionError("mongorestore must not run without --force on a non-empty DB")

    monkeypatch.setattr(backup.subprocess, "run", _fake_run_nonempty)
    rc = _run(["backup", "restore", str(archive), "--yes", "--json"])
    assert rc == 1
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    payload = json.loads(err_lines[-1])
    assert payload["code"] == 1
    assert "42" in payload["message"]
    assert "--force" in payload["remediation"]


def test_backup_restore_empty_db_runs_mongorestore_without_drop(
    docker_present: None,
    fake_compose: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "weather-x.archive.gz"
    archive.write_bytes(_FAKE_ARCHIVE)
    calls: list[list[str]] = []

    def _record(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
        calls.append(cmd)
        if "mongosh" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"0\n", stderr=b"")
        assert input == _FAKE_ARCHIVE
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", _record)
    rc = _run(["backup", "restore", str(archive), "--yes", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["forced"] is False
    assert payload["existing_documents"] == 0

    restore_calls = [c for c in calls if "mongorestore" in c]
    assert len(restore_calls) == 1
    cmd = restore_calls[0]
    assert "--drop" not in cmd
    assert cmd[cmd.index("mongorestore") :] == [
        "mongorestore",
        "--archive",
        "--gzip",
        "--nsInclude",
        "weather.*",
    ]


def test_backup_restore_force_adds_drop(
    docker_present: None,
    fake_compose: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = tmp_path / "weather-x.archive.gz"
    archive.write_bytes(_FAKE_ARCHIVE)
    calls: list[list[str]] = []

    def _record(cmd, input=None, capture_output=False, check=False):  # noqa: ANN001,A002
        calls.append(cmd)
        if "mongosh" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"7\n", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", _record)
    rc = _run(["backup", "restore", str(archive), "--yes", "--force", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["forced"] is True
    assert payload["existing_documents"] == 7

    restore_calls = [c for c in calls if "mongorestore" in c]
    assert len(restore_calls) == 1
    assert "--drop" in restore_calls[0]


# --- environment-error paths (no-traceback contract) ------------------------


def test_docker_absent_exits_2_with_hint(
    fake_compose: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backup_dir: Path,
) -> None:
    monkeypatch.setattr(backup.stack.shutil, "which", lambda _name: None)
    rc = _run(["backup", "dump", "--dir", str(backup_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_compose_file_missing_exits_2(
    docker_present: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(backup.stack, "find_compose", lambda: None)
    rc = _run(["backup", "dump", "--dir", str(tmp_path / "backups")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "Traceback" not in err


def test_daemon_unreachable_gives_specific_hint(
    docker_present: None,
    fake_compose: Path,
    backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(backup.subprocess, "run", _fake_run_daemon_down)
    rc = _run(["backup", "dump", "--dir", str(backup_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "daemon" in err.lower()
    assert "Traceback" not in err


# --- volume-safety invariant -------------------------------------------------


def test_backup_module_issues_no_volume_removing_command() -> None:
    text = Path(backup.__file__).read_text(encoding="utf-8")
    forbidden = ("--volumes", "volume rm", "volume prune", "down -v")
    for pattern in forbidden:
        assert pattern not in text, f"backup.py must never contain {pattern!r}"
