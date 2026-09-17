"""``climate-cli stack`` — manage the climate-weather compose project.

Wraps ``docker compose`` over this repo's ``docker-compose.yml`` (project
name ``climate-weather``; services ``weather-mongodb``, ``weather-tracker``,
``weather-web``) so an operator or agent never has to hand-roll compose
invocations:

    climate-cli stack up        # docker compose up -d --build
    climate-cli stack down      # docker compose down
    climate-cli stack status    # docker compose ps --format json (+ health)
    climate-cli stack status --json
    climate-cli stack overview

Modelled on data-refinery-cli's ``data_refinery/cli/_commands/stack.py``
(read for reference, not imported).

Contract (agent-first):

* every verb supports ``--json``; results to stdout, diagnostics to stderr.
* docker absent, the compose plugin missing, the daemon unreachable, or no
  compose file found (e.g. a wheel install, which ships no
  ``docker-compose.yml``) each raise :class:`CliError` with ``code=2`` and a
  hint specific to that failure — never a Python traceback.
* ``up`` additionally requires ``docker/weather.env`` to exist (the compose
  file's services load secrets from it via ``env_file:``); if it is missing,
  ``up`` fails fast with a hint to copy ``docker/weather.env.example``
  instead of letting compose fail with a confusing error later.
* ``down`` never passes a compose flag that would drop the named volume
  ``weather-mongodb-data`` — this module issues no volume-deleting command
  at all (enforced repo-wide by ``tests/weather/test_compose.py``).

Docker is never invoked directly in tests: every docker call funnels through
this module's ``subprocess.run``, which tests monkeypatch to a fake — no
socket, no daemon, no container ever touched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
from pathlib import Path
from typing import Any

from climate.cli._errors import EXIT_ENV_ERROR, CliError
from climate.cli._output import emit_diagnostic, emit_result

_COMPOSE_FILENAME = "docker-compose.yml"
_ENV_FILE_REL = "docker/weather.env"
_ENV_EXAMPLE_REL = "docker/weather.env.example"

_DOCKER_HINT = (
    "install Docker and ensure 'docker compose' works (https://docs.docker.com/get-docker/)"
)
_COMPOSE_MISSING_HINT = (
    "run from a climate-cli checkout that ships docker-compose.yml (a wheel install does not "
    "include it); clone the repo to manage the weather stack"
)
_COMPOSE_PLUGIN_HINT = (
    "install the Docker Compose plugin (https://docs.docker.com/compose/install/) — the "
    "'docker' binary is present but its 'compose' subcommand is not"
)
_DAEMON_HINT = (
    "start the Docker daemon (e.g. 'sudo systemctl start docker', or open Docker Desktop) "
    "and confirm 'docker info' succeeds"
)
_ENV_FILE_HINT = f"copy {_ENV_EXAMPLE_REL} to {_ENV_FILE_REL} and fill in real values"


def find_compose() -> Path | None:
    """Locate this repo's ``docker-compose.yml`` by walking up from this module.

    In a wheel install the compose file does not ship, so this returns
    ``None`` and the caller raises a structured environment error.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _COMPOSE_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="docker is not installed or not on PATH",
            remediation=_DOCKER_HINT,
        )


def _require_compose() -> Path:
    compose = find_compose()
    if compose is None:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"could not locate {_COMPOSE_FILENAME} for the weather stack",
            remediation=_COMPOSE_MISSING_HINT,
        )
    return compose


def _require_env_file(compose: Path) -> None:
    """Fail fast (before invoking compose) when docker/weather.env is missing.

    docker-compose.yml's weather-tracker and weather-web services load
    secrets via ``env_file: docker/weather.env`` (gitignored). Compose itself
    would refuse to start with a much less actionable error, so ``up``
    checks this up front and points at the example file.
    """
    env_file = compose.parent / _ENV_FILE_REL
    if not env_file.is_file():
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"{_ENV_FILE_REL} is missing — required by docker-compose.yml's env_file:",
            remediation=_ENV_FILE_HINT,
        )


def _classify_compose_failure(verb: str, returncode: int, stdout: str, stderr: str) -> CliError:
    """Turn a failed ``docker compose`` invocation into a specific CliError.

    Distinguishes "compose plugin missing" from "daemon unreachable" from a
    generic compose failure by sniffing the CLI's own stderr wording — these
    are the actual messages the docker CLI emits for each condition.
    """
    detail = (stderr or stdout or "").strip()
    lower = detail.lower()
    first_line = detail.splitlines()[0] if detail else f"exit code {returncode}"

    if "compose" in lower and ("is not a docker command" in lower or "unknown command" in lower):
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"docker compose plugin is not available: {first_line}",
            remediation=_COMPOSE_PLUGIN_HINT,
        )
    if "cannot connect to the docker daemon" in lower or "daemon" in lower:
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"docker daemon is unreachable: {first_line}",
            remediation=_DAEMON_HINT,
        )
    return CliError(
        code=EXIT_ENV_ERROR,
        message=f"docker compose {verb} failed: {first_line}",
        remediation="run 'docker compose version' and confirm the docker daemon is running",
    )


def _env_file_args(compose: Path) -> list[str]:
    """``--env-file docker/weather.env`` when that file exists.

    Compose interpolates ``${CLIMATE_WEB_BIND}``, ``${CLIMATE_WEB_PORT}`` and
    ``${CLIMATE_WEATHER_CONFIG_DIR}`` from its *project* env file, not from a
    service's ``env_file:``. Passing the same gitignored file here makes the
    one documented file govern both the containers and the host-side mapping.
    """
    env_file = compose.parent / "docker" / "weather.env"
    return ["--env-file", str(env_file)] if env_file.is_file() else []


def _compose(compose: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose -f <compose> <args...>`` capturing output.

    Never uses a shell; the argv is a fixed list. A missing docker binary
    (vanished between the earlier check and now) or a non-zero compose exit
    is translated into a :class:`CliError` (code 2) so no traceback leaks.
    """
    cmd = ["docker", "compose", "-f", str(compose), *_env_file_args(compose), *args]
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # docker vanished between check and run
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="docker is not installed or not on PATH",
            remediation=_DOCKER_HINT,
        ) from exc
    if proc.returncode != 0:
        verb = args[0] if args else ""
        raise _classify_compose_failure(verb, proc.returncode, proc.stdout, proc.stderr)
    return proc


def _load_ps_rows(text: str) -> list[Any]:
    """Decode ``compose ps`` output: a top-level JSON array, else NDJSON."""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass
    rows: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _parse_ps(stdout: str) -> list[dict[str, object]]:
    """Parse ``docker compose ps --format json`` (array OR one-object-per-line)."""
    text = stdout.strip()
    if not text:
        return []
    services: list[dict[str, object]] = []
    for row in _load_ps_rows(text):
        if not isinstance(row, dict):
            continue
        services.append(
            {
                "name": row.get("Name") or row.get("Service") or "?",
                "service": row.get("Service") or "?",
                "state": row.get("State") or "?",
                "health": row.get("Health") or "",
                "status": row.get("Status") or "",
            }
        )
    return services


def _status_payload(compose: Path) -> dict[str, object]:
    proc = _compose(compose, "ps", "--all", "--format", "json")
    services = _parse_ps(proc.stdout)
    running = [s for s in services if s["state"] == "running"]
    # Empty health means "no healthcheck defined" (or not yet evaluated) —
    # treated as OK; a reported unhealthy/starting is not.
    healthy = bool(services) and all(
        s["state"] == "running" and s["health"] not in ("unhealthy", "starting") for s in services
    )
    return {
        "compose_file": str(compose),
        "running": len(running) == len(services) and bool(services),
        "healthy": healthy,
        "services": services,
    }


def cmd_stack_up(args: argparse.Namespace) -> int:
    _require_docker()
    compose = _require_compose()
    _require_env_file(compose)
    json_mode = bool(getattr(args, "json", False))
    emit_diagnostic("bringing up the climate-weather stack (mongo + tracker + web)…")
    _compose(compose, "up", "-d", "--build")
    payload = _status_payload(compose)
    payload["command"] = "up"
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        names = ", ".join(str(s["name"]) for s in payload["services"]) or "(none)"
        emit_result(f"## stack up\n\n- services: {names}", json_mode=False)
    return 0


def cmd_stack_down(args: argparse.Namespace) -> int:
    _require_docker()
    compose = _require_compose()
    json_mode = bool(getattr(args, "json", False))
    emit_diagnostic("stopping the climate-weather stack…")
    # --remove-orphans only drops containers no longer in the compose file;
    # it never touches the named volume weather-mongodb-data.
    _compose(compose, "down", "--remove-orphans")
    payload = {"command": "down", "compose_file": str(compose), "running": False}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result("## stack down\n\n- stopped (volume preserved)", json_mode=False)
    return 0


def _render_status_markdown(payload: dict[str, object]) -> str:
    """Markdown-first status render (climate-cli convention: markdown for
    agents/humans, ``--json`` for code)."""
    services = payload["services"]
    if not services:
        return "## stack status\n\nno services found — try `climate-cli stack up`"
    lines = [
        "## stack status",
        "",
        f"- compose file: `{payload['compose_file']}`",
        f"- healthy: **{payload['healthy']}**",
        "",
        "| service | state | health |",
        "| --- | --- | --- |",
    ]
    for s in services:  # type: ignore[attr-defined]
        lines.append(f"| {s['name']} | {s['state']} | {s['health'] or '-'} |")
    return "\n".join(lines)


def cmd_stack_status(args: argparse.Namespace) -> int:
    _require_docker()
    compose = _require_compose()
    json_mode = bool(getattr(args, "json", False))
    payload = _status_payload(compose)
    payload["command"] = "status"
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_status_markdown(payload), json_mode=False)
    return 0


def _stack_overview(args: argparse.Namespace) -> int:
    """``climate-cli stack`` with no sub-verb prints the noun's overview."""
    from climate.cli._commands.overview import emit_overview

    sections = [
        {
            "title": "Verbs",
            "items": [
                "stack up — bring up mongo + tracker + web via docker compose up -d --build",
                "stack down — stop the stack (volume preserved)",
                "stack status — report per-service state + health",
                "stack overview — describe this noun",
            ],
        },
        {
            "title": "Project",
            "items": [
                "climate-weather: weather-mongodb, weather-tracker, weather-web",
                "defined in docker-compose.yml at the repo root",
                f"'up' requires {_ENV_FILE_REL} (copy from {_ENV_EXAMPLE_REL})",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every verb supports --json",
                "docker/compose/daemon problems → exit 2 with a hint:, never a traceback",
            ],
        },
    ]
    emit_overview("climate-cli stack", sections, json_mode=bool(getattr(args, "json", False)))
    return 0


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stack",
        help="Manage the climate-weather compose project via docker compose.",
    )
    _add_json_flag(p)
    p.set_defaults(func=_stack_overview, json=False)
    # Propagate the structured-error parser_class to nested verbs, matching
    # the host CLI's convention (see climate/cli/_commands/cli.py).
    verb = p.add_subparsers(dest="stack_command", parser_class=type(p))

    up = verb.add_parser("up", help="Bring the stack up (docker compose up -d --build).")
    _add_json_flag(up)
    up.set_defaults(func=cmd_stack_up)

    down = verb.add_parser("down", help="Stop the stack (docker compose down).")
    _add_json_flag(down)
    down.set_defaults(func=cmd_stack_down)

    status = verb.add_parser("status", help="Report per-service state + health.")
    _add_json_flag(status)
    status.set_defaults(func=cmd_stack_status)

    ov = verb.add_parser("overview", help="Describe the stack noun.")
    _add_json_flag(ov)
    ov.set_defaults(func=_stack_overview)
