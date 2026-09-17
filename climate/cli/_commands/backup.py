"""``climate-cli backup`` — dump and restore the weather MongoDB database.

Because ``weather-mongodb`` publishes no host port (see docker-compose.yml),
every verb here talks to it through ``docker compose exec -T`` rather than a
direct MongoDB connection — no ``pymongo`` import at module scope, and no
socket ever opened by this module or its tests.

    climate-cli backup dump                 # mongodump --archive --gzip -> host file
    climate-cli backup dump --json
    climate-cli backup list                 # newest first, with size + age
    climate-cli backup restore <archive> --yes
    climate-cli backup overview

Modelled on :mod:`climate.cli._commands.stack` (imported for its
``find_compose()`` / docker-availability / compose-failure-classification
helpers, not copied):

* every verb supports ``--json``; results to stdout, diagnostics to stderr.
* docker/compose/daemon problems raise :class:`CliError` with
  ``code=EXIT_ENV_ERROR`` (2) — never a Python traceback.
* user-input problems (missing ``--yes``, a non-empty database without
  ``--force``, a backup directory inside the repo checkout, a missing
  archive file) raise :class:`CliError` with ``code=EXIT_USER_ERROR`` (1).
* this module issues no volume-deleting docker command at all — the same
  invariant ``stack.py`` holds, enforced repo-wide by
  ``tests/weather/test_compose.py`` and locally by
  ``tests/test_cli_backup.py``.

Every docker invocation funnels through the single :func:`_docker_exec`
wrapper (``subprocess.run`` in *bytes* mode — the mongodump archive is
binary). Tests monkeypatch ``backup.subprocess.run`` and never touch a real
docker daemon.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from climate.cli._commands import stack
from climate.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from climate.cli._output import emit_diagnostic, emit_result

_SERVICE = "weather-mongodb"
_DB = "weather"
_ARCHIVE_GLOB = "weather-*.archive.gz"
_BACKUP_DIR_ENV = "CLIMATE_WEATHER_BACKUP_DIR"
_XDG_DATA_HOME_ENV = "XDG_DATA_HOME"

# mongosh eval that sums document counts across every collection in the
# weather database — used by ``restore`` to decide whether the target is
# already populated before overwriting it.
_COUNT_DOCUMENTS_EVAL = (
    "db.getSiblingDB('weather').getCollectionNames()"
    ".map(function(c){return db.getSiblingDB('weather').getCollection(c).countDocuments();})"
    ".reduce(function(a,b){return a+b;}, 0)"
)

_INSIDE_REPO_HINT = (
    "choose a backup directory outside the climate-cli checkout, e.g. via --dir or "
    f"{_BACKUP_DIR_ENV}"
)


def _default_backup_dir() -> Path:
    """``$XDG_DATA_HOME/climate-cli/backups``, else ``~/.local/share/...``."""
    xdg = os.environ.get(_XDG_DATA_HOME_ENV)
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "climate-cli" / "backups"


def _repo_root() -> Optional[Path]:
    compose = stack.find_compose()
    return compose.parent if compose is not None else None


def _resolve_backup_dir(cli_dir: Optional[str]) -> Path:
    """Resolve the backup directory (--dir > env > XDG default) and refuse
    one that resolves inside the repo checkout."""
    if cli_dir:
        raw = Path(cli_dir)
    else:
        env_dir = os.environ.get(_BACKUP_DIR_ENV)
        raw = Path(env_dir) if env_dir else _default_backup_dir()
    resolved = raw.expanduser().resolve()

    repo_root = _repo_root()
    if repo_root is not None:
        repo_root = repo_root.resolve()
        if resolved == repo_root or repo_root in resolved.parents:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"backup directory {resolved} is inside the repo checkout",
                remediation=_INSIDE_REPO_HINT,
            )
    return resolved


def _docker_exec(
    compose: Path,
    service: str,
    args: list[str],
    *,
    input_bytes: Optional[bytes] = None,
) -> "subprocess.CompletedProcess[bytes]":
    """Run ``docker compose -f <compose> exec -T <service> <args...>``.

    The single subprocess entry point this module uses — always bytes mode
    (never ``text=True``), since ``mongodump``'s archive is binary. Tests
    monkeypatch ``backup.subprocess.run``; no real docker call is ever made
    under test.
    """
    cmd = ["docker", "compose", "-f", str(compose), "exec", "-T", service, *args]
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            cmd,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # docker vanished between check and run
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="docker is not installed or not on PATH",
            remediation=stack._DOCKER_HINT,
        ) from exc
    if proc.returncode != 0:
        verb = args[0] if args else "exec"
        raise stack._classify_compose_failure(
            verb,
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
    return proc


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` atomically (temp file + rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".backup-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _list_backup_files(backup_dir: Path) -> list[Path]:
    """Backups in ``backup_dir``, newest first."""
    if not backup_dir.is_dir():
        return []
    return sorted(
        backup_dir.glob(_ARCHIVE_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def newest_backup(dir: Optional[Path | str] = None) -> Optional[tuple[Path, float]]:
    """Return ``(path, age_seconds)`` of the newest backup, or ``None``.

    Small importable helper for the doctor task (backup-age check). ``dir``
    is used as-is when given (no repo-inside refusal — the caller is
    expected to already know its own directory); with no ``dir`` it
    resolves the same way ``backup list`` does.
    """
    backup_dir = Path(dir).expanduser() if dir is not None else _resolve_backup_dir(None)
    files = _list_backup_files(backup_dir)
    if not files:
        return None
    newest = files[0]
    age = time.time() - newest.stat().st_mtime
    return newest, age


def _count_weather_documents(compose: Path) -> int:
    proc = _docker_exec(compose, _SERVICE, ["mongosh", "--quiet", "--eval", _COUNT_DOCUMENTS_EVAL])
    text = proc.stdout.decode("utf-8", "replace").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last = lines[-1] if lines else "0"
    try:
        return int(last)
    except ValueError:
        return 0


def cmd_backup_dump(args: argparse.Namespace) -> int:
    stack._require_docker()
    compose = stack._require_compose()
    json_mode = bool(getattr(args, "json", False))

    backup_dir = _resolve_backup_dir(getattr(args, "dir", None))
    backup_dir.mkdir(parents=True, exist_ok=True)

    emit_diagnostic(f"dumping the {_DB} database via {_SERVICE} (mongodump --archive --gzip)…")
    proc = _docker_exec(compose, _SERVICE, ["mongodump", "--db", _DB, "--archive", "--gzip"])
    archive_bytes = proc.stdout

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"weather-{timestamp}.archive.gz"
    _atomic_write_bytes(dest, archive_bytes)

    digest = hashlib.sha256(archive_bytes).hexdigest()
    payload = {
        "command": "dump",
        "path": str(dest),
        "size": dest.stat().st_size,
        "sha256": digest,
    }
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(
            "## backup dump\n\n"
            f"- path: `{payload['path']}`\n"
            f"- size: {payload['size']} bytes\n"
            f"- sha256: `{payload['sha256']}`",
            json_mode=False,
        )
    return 0


def _render_list_markdown(backup_dir: Path, items: list[dict[str, object]]) -> str:
    if not items:
        return f"## backup list\n\nno backups found in `{backup_dir}`"
    lines = [
        "## backup list",
        "",
        f"- dir: `{backup_dir}`",
        "",
        "| path | size | age |",
        "| --- | --- | --- |",
    ]
    for item in items:
        lines.append(f"| {item['path']} | {item['size']} bytes | {int(item['age_seconds'])}s |")
    return "\n".join(lines)


def cmd_backup_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    backup_dir = _resolve_backup_dir(getattr(args, "dir", None))
    files = _list_backup_files(backup_dir)
    now = time.time()
    items = [
        {
            "path": str(f),
            "size": f.stat().st_size,
            "age_seconds": now - f.stat().st_mtime,
        }
        for f in files
    ]
    payload = {"command": "list", "dir": str(backup_dir), "backups": items}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_list_markdown(backup_dir, items), json_mode=False)
    return 0


def cmd_backup_restore(args: argparse.Namespace) -> int:
    if not getattr(args, "yes", False):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="backup restore requires --yes to confirm",
            remediation="re-run with --yes once you're sure you want to overwrite the database",
        )

    archive_path = Path(args.archive)
    if not archive_path.is_file():
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"archive not found: {archive_path}",
            remediation="check the path, or run 'climate-cli backup list'",
        )

    stack._require_docker()
    compose = stack._require_compose()
    json_mode = bool(getattr(args, "json", False))
    force = bool(getattr(args, "force", False))

    emit_diagnostic(f"counting existing documents in the {_DB} database…")
    existing = _count_weather_documents(compose)
    if existing > 0 and not force:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{_DB} database is not empty ({existing} documents)",
            remediation="pass --force to overwrite (adds mongorestore --drop)",
        )

    archive_bytes = archive_path.read_bytes()
    mongorestore_args = ["mongorestore", "--archive", "--gzip", "--nsInclude", f"{_DB}.*"]
    if force:
        mongorestore_args.append("--drop")

    emit_diagnostic(f"restoring {_DB} database from {archive_path}…")
    _docker_exec(compose, _SERVICE, mongorestore_args, input_bytes=archive_bytes)

    payload = {
        "command": "restore",
        "path": str(archive_path),
        "forced": force,
        "existing_documents": existing,
    }
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(
            "## backup restore\n\n"
            f"- path: `{payload['path']}`\n"
            f"- forced: {force}\n"
            f"- existing documents before restore: {existing}",
            json_mode=False,
        )
    return 0


def _backup_overview(args: argparse.Namespace) -> int:
    """``climate-cli backup`` with no sub-verb prints the noun's overview."""
    from climate.cli._commands.overview import emit_overview

    sections = [
        {
            "title": "Verbs",
            "items": [
                "backup dump — mongodump --archive --gzip inside weather-mongodb -> host file",
                "backup list — list backups, newest first, with size + age",
                "backup restore <archive> --yes — restore into weather-mongodb",
                "backup overview — describe this noun",
            ],
        },
        {
            "title": "Backup directory",
            "items": [
                "--dir, else CLIMATE_WEATHER_BACKUP_DIR, else $XDG_DATA_HOME/climate-cli/backups",
                "refused (exit 1) if it resolves inside the repo checkout",
            ],
        },
        {
            "title": "Restore safety",
            "items": [
                "--yes is required to confirm",
                "refuses a non-empty database unless --force is also given",
                "--force adds mongorestore --drop; never used otherwise",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every verb supports --json",
                "docker/compose/daemon problems -> exit 2 with a hint:, never a traceback",
                "user-input problems (missing --yes, non-empty DB, bad dir) -> exit 1",
            ],
        },
    ]
    emit_overview("climate-cli backup", sections, json_mode=bool(getattr(args, "json", False)))
    return 0


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "backup",
        help="Dump and restore the weather MongoDB database via docker compose exec.",
    )
    _add_json_flag(p)
    p.set_defaults(func=_backup_overview, json=False)
    verb = p.add_subparsers(dest="backup_command", parser_class=type(p))

    dump = verb.add_parser(
        "dump", help="mongodump --archive --gzip inside weather-mongodb -> host file."
    )
    _add_json_flag(dump)
    dump.add_argument(
        "--dir",
        help=f"Backup directory (default: ${_BACKUP_DIR_ENV} or XDG data home).",
    )
    dump.set_defaults(func=cmd_backup_dump)

    lst = verb.add_parser("list", help="List backups, newest first, with size + age.")
    _add_json_flag(lst)
    lst.add_argument("--dir", help="Backup directory to list.")
    lst.set_defaults(func=cmd_backup_list)

    restore = verb.add_parser("restore", help="Restore weather DB from an archive.")
    _add_json_flag(restore)
    restore.add_argument("archive", help="Path to a *.archive.gz dump file.")
    restore.add_argument("--yes", action="store_true", help="Confirm the restore (required).")
    restore.add_argument(
        "--force",
        action="store_true",
        help="Allow restoring into a non-empty database (adds mongorestore --drop).",
    )
    restore.set_defaults(func=cmd_backup_restore)

    ov = verb.add_parser("overview", help="Describe the backup noun.")
    _add_json_flag(ov)
    ov.set_defaults(func=_backup_overview)
