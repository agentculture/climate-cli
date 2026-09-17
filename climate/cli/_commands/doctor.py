"""``climate-cli doctor`` — check the agent-identity invariants, plus the
weather-tracker's runtime environment.

Two independent check groups, both in the rubric-shaped contract
``{id, passed, severity, message, remediation}``:

* **identity** — mirrors the two invariants ``steward doctor`` verifies for a
  mesh agent (``prompt-file-present``, ``backend-consistency``) plus
  ``skills-present``. Unchanged from the original doctor: read-only, no
  network, no docker.
* **weather** (ids prefixed ``weather_``) — docker/stack availability, the
  web API's reachability and the data it reports (freshness, database size,
  tracker version), provider credentials, host disk headroom and the newest
  backup's age. Every external interaction (subprocess, HTTP, environment,
  wall clock, ``PATH`` lookup) goes through an injectable seam
  (:func:`_weather_checks`'s ``run``/``fetch``/``env``/``now``/``which``/
  ``disk_usage`` keywords) so tests never touch docker, a socket or the real
  disk. Every individual check is wrapped so an unexpected exception becomes
  a failed check naming the exception class, never a traceback.

Reports the rubric-shaped contract
``{healthy, checks: [{id, passed, severity, message, remediation}]}`` so the
agent-first rubric's bundle 7 passes. The identity group alone still reports
a single ``source_checkout`` info check when no ``culture.yaml`` is found
(wheel install) — but the weather group runs regardless, since a wheel
install can still be pointed at a running stack via ``CLIMATE_WEATHER_URL``.

Exit code: ``0`` when every check passes, or every failing check has
severity ``warning``/``info``; ``1`` when any ``error``-severity check
failed. This is a deliberate change from "any failing check" to "any
*error-severity* failing check" — see docs/weather-api.md's client
obligations and the plan's severity policy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from climate import __version__ as HOST_VERSION
from climate.cli._commands import backup, stack, weather
from climate.cli._commands.whoami import find_culture_yaml, read_agent_fields
from climate.cli._output import emit_result
from climate.weather import config as weather_config
from climate.weather import providers as weather_providers

Check = dict[str, Any]

# backend → required prompt file (the backend-consistency mapping).
_PROMPT_FILE = {
    "claude": "CLAUDE.md",
    "acp": "AGENTS.md",
    "gemini": "GEMINI.md",
}

_STACK_UP_HINT = "run 'climate stack up' to bring up the weather stack"
_STACK_STATUS_HINT = "run 'climate stack status' to check the weather stack"
_DOCKER_INSTALL_HINT = (
    "install Docker and ensure 'docker compose' works (https://docs.docker.com/get-docker/)"
)

#: Where the tracker actually reads its provider credentials from: compose
#: hands this file to the ``weather-tracker`` service alone, so a credential
#: is expected to be absent from both this host shell and the web container.
_WEATHER_ENV_FILE = stack._ENV_FILE_REL

_DISK_MIN_BYTES_ENV = "CLIMATE_WEATHER_DISK_MIN_GIB"
_DEFAULT_DISK_MIN_GIB = 10
_DOCKER_DATA_ROOT = Path("/var/lib/docker")

_BACKUP_MAX_AGE_ENV = "CLIMATE_WEATHER_BACKUP_MAX_AGE_DAYS"
_DEFAULT_BACKUP_MAX_AGE_DAYS = 7
_SECONDS_PER_DAY = 86400


# --- identity checks (unchanged) --------------------------------------------


def _diagnose() -> dict[str, object]:
    cfg = find_culture_yaml()
    if cfg is None:
        check = {
            "id": "source_checkout",
            "passed": True,
            "severity": "info",
            "message": "no culture.yaml found alongside the package; identity checks skipped",
            "remediation": "",
        }
        return {"healthy": True, "checks": [check]}

    root = cfg.parent
    fields = read_agent_fields()
    backend = fields["backend"]
    checks: list[dict[str, object]] = []

    # 1. backend-consistency: the prompt file for the declared backend exists.
    expected = _PROMPT_FILE.get(backend)
    if expected is None:
        checks.append(
            {
                "id": "backend_consistency",
                "passed": False,
                "severity": "error",
                "message": f"unknown backend '{backend}' in culture.yaml",
                "remediation": f"set backend to one of: {', '.join(sorted(_PROMPT_FILE))}",
            }
        )
    else:
        present = (root / expected).is_file()
        checks.append(
            {
                "id": "prompt_file_present",
                "passed": present,
                "severity": "error",
                "message": (
                    f"backend '{backend}' requires {expected} — "
                    + ("present" if present else "missing")
                ),
                "remediation": "" if present else f"create {expected} at the repo root",
            }
        )

    # 2. skills-present: the vendored skill kit is on disk.
    skills_dir = root / ".claude" / "skills"
    has_skills = skills_dir.is_dir() and any(skills_dir.iterdir())
    checks.append(
        {
            "id": "skills_present",
            "passed": has_skills,
            "severity": "warning",
            "message": (
                ".claude/skills/ vendored" if has_skills else ".claude/skills/ missing or empty"
            ),
            "remediation": (
                "" if has_skills else "vendor the skill kit (see docs/skill-sources.md)"
            ),
        }
    )

    healthy = all(c["passed"] for c in checks)
    return {"healthy": healthy, "checks": checks}


# --- weather-tracker checks --------------------------------------------------


def _safe_check(
    check_id: str,
    severity: str,
    remediation: str,
    fn: Callable[[], tuple[bool, str, str | None]],
) -> Check:
    """Run one check body, never letting an exception escape.

    ``fn`` always returns the one shape ``(passed, message,
    override_remediation)``: ``override_remediation`` is ``None`` to fall
    back to ``remediation`` on failure (and no remediation on success), or a
    string to use verbatim regardless of ``passed``. Any exception becomes a
    failed check naming the exception's class, using ``remediation`` as the
    hint.
    """
    try:
        passed, message, override_remediation = fn()
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return {
            "id": check_id,
            "passed": False,
            "severity": severity,
            "message": f"{check_id} check raised {type(exc).__name__}: {exc}",
            "remediation": remediation,
        }
    if override_remediation is not None:
        final_remediation = override_remediation
    else:
        final_remediation = "" if passed else remediation
    return {
        "id": check_id,
        "passed": bool(passed),
        "severity": severity,
        "message": message,
        "remediation": final_remediation,
    }


def _compose_ps_services(
    compose: Path, run: Callable[..., Any]
) -> tuple[list[dict[str, object]], str]:
    """Run ``docker compose ... ps --all --format json`` via ``run``.

    Returns ``(services, failure_message)``; ``failure_message`` is empty on
    success. Reuses :mod:`stack`'s ps-row parsing and compose-failure
    classification — the same helpers the real ``stack status`` verb uses.
    """
    cmd = ["docker", "compose", "-f", str(compose), "ps", "--all", "--format", "json"]
    proc = run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = stack._classify_compose_failure(
            "ps", proc.returncode, proc.stdout or "", proc.stderr or ""
        )
        return [], err.message
    return stack._parse_ps(proc.stdout), ""


def _resolve_weather_base_url(env: Mapping[str, str]) -> str:
    return env.get(weather.BASE_URL_ENV_VAR, weather.DEFAULT_BASE_URL).rstrip("/")


def _fetch_json(
    fetch: Callable[..., Any], base_url: str, path: str
) -> tuple[dict[str, Any] | None, str]:
    """GET ``base_url + API_PREFIX + path`` via ``fetch``.

    Returns ``(payload, error_message)``; ``payload`` is ``None`` when the
    call failed (transport error, non-2xx, or a body that is not JSON).
    """
    url = f"{base_url}{weather.API_PREFIX}{path}"
    result = fetch(url, timeout=weather.DEFAULT_TIMEOUT)
    if getattr(result, "error", None) is not None or getattr(result, "status", None) is None:
        return (
            None,
            f"weather API unreachable at {url}: {getattr(result, 'error', None) or 'no response'}",
        )
    status = result.status
    if not (200 <= status < 300):
        return None, f"weather API returned HTTP {status} for {url}"
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (ValueError, AttributeError) as exc:
        return None, f"weather API returned invalid JSON for {url}: {exc}"
    if not isinstance(payload, dict):
        return None, f"weather API returned a non-object JSON payload for {url}"
    return payload, ""


def _docker_data_root() -> Path:
    """Docker's data root if it's readable, else the caller's home directory."""
    if _DOCKER_DATA_ROOT.is_dir() and os.access(_DOCKER_DATA_ROOT, os.R_OK):
        return _DOCKER_DATA_ROOT
    return Path.home()


def _load_weather_config() -> weather_config.WeatherConfig:
    """The user's weather config, falling back to defaults when it is missing/broken."""
    try:
        return weather_config.load_config()
    except Exception:  # noqa: BLE001 - a missing/broken config must not crash doctor
        return weather_config.WeatherConfig()


def _provider_availability(
    provider: Any, weather_cfg: weather_config.WeatherConfig, env: Mapping[str, str]
) -> Any:
    settings = weather_cfg.providers.get(provider.id)
    return provider.availability(settings, env)


def _check_docker_available(which: Callable[[str], str | None]) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        docker_path = which("docker")
        if docker_path:
            return True, f"docker found at {docker_path}", None
        return False, "docker is not installed or not on PATH", None

    return _safe_check("weather_docker_available", "error", _DOCKER_INSTALL_HINT, _fn)


def _check_stack_running(compose: Path, run: Callable[..., Any]) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        services, failure = _compose_ps_services(compose, run)
        if failure:
            return False, f"docker compose ps failed: {failure}", _STACK_STATUS_HINT
        if not services:
            return False, "the weather stack has no running services", _STACK_UP_HINT
        running = [s for s in services if s.get("state") == "running"]
        names = ", ".join(str(s.get("name")) for s in services)
        if len(running) != len(services):
            return False, f"not all weather-stack services are running: {names}", _STACK_UP_HINT
        return True, f"weather stack running: {names}", None

    return _safe_check("weather_stack_running", "error", _STACK_UP_HINT, _fn)


def _check_web_api_reachable(
    fetch: Callable[..., Any], base_url: str
) -> tuple[Check, dict[str, Any] | None]:
    """Returns ``(check, health_payload)``; ``health_payload`` is ``None`` on failure."""
    health_box: list[dict[str, Any] | None] = []

    def _fn() -> tuple[bool, str, str | None]:
        payload, error = _fetch_json(fetch, base_url, "/health")
        health_box.append(payload)
        if payload is None:
            return False, error, _STACK_STATUS_HINT
        message = f"weather API reachable at {base_url} (status: {payload.get('status')})"
        return True, message, None

    check = _safe_check("weather_web_api_reachable", "error", _STACK_STATUS_HINT, _fn)
    health = health_box[0] if health_box else None
    return check, health


def _check_fetch_ages(
    health: dict[str, Any] | None,
    weather_cfg: weather_config.WeatherConfig,
    env: Mapping[str, str],
) -> list[Check]:
    """``weather_fetch_age``/``weather_fetch_age_<id>`` — freshness per provider entry.

    A provider that is merely disabled (no credentials, or disabled in
    settings) is reported as a passed ``info`` check rather than a failing
    ``warning`` — it has no fetch recorded because it was never asked to
    run, which is expected, not a problem worth surfacing noisily.
    """
    if health is None:
        return [
            {
                "id": "weather_fetch_age",
                "passed": False,
                "severity": "warning",
                "message": "cannot determine provider fetch ages: weather API unreachable",
                "remediation": _STACK_STATUS_HINT,
            }
        ]

    providers_by_id = {p.id: p for p in weather_providers.iter_providers()}
    checks: list[Check] = []
    for entry in health.get("providers") or []:
        provider_id = entry.get("provider", "?")
        provider = providers_by_id.get(provider_id)
        availability = None
        if provider is not None:
            try:
                availability = _provider_availability(provider, weather_cfg, env)
            except Exception:  # noqa: BLE001 - a check must never raise
                availability = None

        # The API's own ``enabled`` comes from the tracker (the only process
        # holding the credentials), so it overrules this host shell's guess:
        # a provider that is collecting is never reported as "disabled here".
        if (
            availability is not None
            and not availability.enabled
            and entry.get("enabled") is not True
        ):
            message = f"{provider_id}: disabled ({availability.reason}); no fetch expected"
            checks.append(
                {
                    "id": f"weather_fetch_age_{provider_id}",
                    "passed": True,
                    "severity": "info",
                    "message": message,
                    "remediation": "",
                }
            )
            continue

        def _check_fetch_age(entry: dict[str, Any] = entry) -> tuple[bool, str, str | None]:
            age = entry.get("newest_fetch_age_seconds")
            stale = bool(entry.get("stale"))
            provider_id = entry.get("provider", "?")
            if age is None:
                return False, f"{provider_id}: no fetch recorded yet", None
            if stale:
                return False, f"{provider_id}: newest fetch age {age}s (stale)", None
            return True, f"{provider_id}: newest fetch age {age}s", None

        checks.append(
            _safe_check(
                f"weather_fetch_age_{provider_id}",
                "warning",
                _STACK_STATUS_HINT,
                _check_fetch_age,
            )
        )

    return checks


def _fetch_provider_rows(
    fetch: Callable[..., Any], base_url: str
) -> dict[str, dict[str, Any]] | None:
    """``GET /providers`` as ``{provider_id: row}``, or ``None`` when unavailable.

    This is the tracker's own answer about each provider (the API reports what
    the tracker published in its heartbeat), which is the only honest source
    for "is this credential present": the tracker reads it from
    ``docker/weather.env`` inside its container, never from this shell.
    """
    try:
        payload, _ = _fetch_json(fetch, base_url, "/providers")
    except Exception:  # noqa: BLE001 - a check must never raise
        return None
    rows = (payload or {}).get("providers")
    if not isinstance(rows, list):
        return None
    return {
        str(row["provider"]): row
        for row in rows
        if isinstance(row, dict) and row.get("provider") is not None
    }


def _credential_check_from_api(provider: Any, row: Mapping[str, Any]) -> Check | None:
    """The credential check for one provider, from the API's (tracker's) row."""
    env_var = provider.auth.env_var
    reason = row.get("enabled_reason") or ""
    if not row.get("enabled") and "disabled in configuration" in reason:
        return None  # explicit user choice, not a credential problem
    if not row.get("auth_required", True):
        return None  # the tracker says this provider needs no credential
    passed = bool(row.get("credential_present"))
    if passed:
        message = f"{provider.id}: {env_var} is set in the tracker"
    else:
        message = (
            f"{provider.id}: {env_var} is not set in the tracker " "(provider disabled until it is)"
        )
    return {
        "id": f"weather_provider_credentials_{provider.id}",
        "passed": passed,
        "severity": "warning",
        "message": message,
        "remediation": ("" if passed else f"add {env_var} to {_WEATHER_ENV_FILE}"),
    }


def _credential_check_from_host(
    provider: Any,
    weather_cfg: weather_config.WeatherConfig,
    env: Mapping[str, str],
    *,
    api_reachable: bool,
) -> Check | None:
    """The honest fallback when the API cannot be asked.

    The host shell is *not* where the tracker reads its key from — the key
    lives in the gitignored ``docker/weather.env``, which only the tracker
    container is given — so this reports the state as unknown from here
    rather than claiming a missing credential. Severity ``info``: an
    unanswerable question is not a failure.
    """
    env_var = provider.auth.env_var
    try:
        settings = weather_cfg.providers.get(provider.id)
    except Exception:  # noqa: BLE001 - a check must never raise
        settings = None
    if settings is not None and not getattr(settings, "enabled", True):
        return None  # explicit user choice, not a credential problem
    in_host_shell = "set" if env.get(env_var) else "not set"
    why = (
        "the weather API is unreachable"
        if api_reachable is False
        else "the weather API reported no such provider"
    )
    return {
        "id": f"weather_provider_credentials_{provider.id}",
        "passed": True,
        "severity": "info",
        "message": (
            f"{provider.id}: cannot tell whether {env_var} is set — {why}, and the "
            f"tracker reads it from {_WEATHER_ENV_FILE}, not from this shell "
            f"(where it is {in_host_shell})"
        ),
        "remediation": (
            f"start the stack and re-run doctor; set {env_var} in {_WEATHER_ENV_FILE} "
            f"to enable {provider.id}"
        ),
    }


def _check_provider_credentials(
    weather_cfg: weather_config.WeatherConfig,
    env: Mapping[str, str],
    provider_rows: Mapping[str, Mapping[str, Any]] | None,
) -> list[Check]:
    """``weather_provider_credentials_<id>`` — one check per credentialed provider.

    Never reports a value, only whether the credential is set — and asks the
    process that actually holds it: ``provider_rows`` is
    ``GET /api/v1/providers``, whose ``credential_present`` comes from the
    tracker's heartbeat. Only when that is unavailable does this fall back to
    the host environment, and it then says so instead of pretending the host
    shell is authoritative.
    """
    checks: list[Check] = []
    for provider in weather_providers.iter_providers():
        if not provider.auth.required or not provider.auth.env_var:
            continue
        row = (provider_rows or {}).get(provider.id)
        try:
            check = (
                _credential_check_from_api(provider, row)
                if row is not None
                else _credential_check_from_host(
                    provider, weather_cfg, env, api_reachable=provider_rows is not None
                )
            )
        except Exception as exc:  # noqa: BLE001 - a check must never raise
            check = {
                "id": f"weather_provider_credentials_{provider.id}",
                "passed": False,
                "severity": "warning",
                "message": (
                    f"weather_provider_credentials_{provider.id} check raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                "remediation": f"add {provider.auth.env_var} to {_WEATHER_ENV_FILE}",
            }
        if check is not None:
            checks.append(check)
    return checks


def _check_database_size(health: dict[str, Any] | None) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        if health is None:
            return False, "cannot determine database size: weather API unreachable", None
        store = health.get("store") or {}
        if not store.get("reachable"):
            return False, "database size unknown: store unreachable", None
        size_bytes = store.get("size_bytes")
        if size_bytes is None:
            return False, "database size unknown: store did not report a size", None
        mib = size_bytes / (1024 * 1024)
        return True, f"database size: {size_bytes} bytes ({mib:.1f} MiB)", None

    return _safe_check("weather_database_size", "warning", _STACK_STATUS_HINT, _fn)


def _check_disk_headroom(env: Mapping[str, str], disk_usage: Callable[[Path], Any]) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        target = _docker_data_root()
        usage = disk_usage(target)
        free_gib = usage.free / (1024**3)
        min_gib = int(env.get(_DISK_MIN_BYTES_ENV, _DEFAULT_DISK_MIN_GIB))
        if free_gib < min_gib:
            return False, f"{target}: only {free_gib:.1f} GiB free (threshold {min_gib} GiB)", None
        return True, f"{target}: {free_gib:.1f} GiB free", None

    remediation = (
        "free disk space on the docker host, or lower the threshold via " f"{_DISK_MIN_BYTES_ENV}"
    )
    return _safe_check("weather_disk_headroom", "warning", remediation, _fn)


def _check_backup_age(env: Mapping[str, str]) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        result = backup.newest_backup()
        if result is None:
            return False, "no weather backup found", None
        path, age_seconds = result
        max_days = int(env.get(_BACKUP_MAX_AGE_ENV, _DEFAULT_BACKUP_MAX_AGE_DAYS))
        age_days = age_seconds / _SECONDS_PER_DAY
        if age_days > max_days:
            message = f"newest backup ({path.name}) is {age_days:.1f} days old (> {max_days}d)"
            return False, message, None
        return True, f"newest backup ({path.name}) is {age_days:.1f} days old", None

    return _safe_check("weather_backup_age", "warning", "run 'climate-cli backup dump'", _fn)


def _check_tracker_version(health: dict[str, Any] | None) -> Check:
    def _fn() -> tuple[bool, str, str | None]:
        if health is None:
            return False, "cannot determine tracker version: weather API unreachable", None
        tracker_version = health.get("tracker_version")
        if tracker_version is None:
            return False, "tracker has not reported a heartbeat yet", None
        if tracker_version != HOST_VERSION:
            message = (
                f"tracker version {tracker_version} differs from host CLI version {HOST_VERSION}"
            )
            return False, message, None
        message = f"tracker version {tracker_version} matches host CLI version {HOST_VERSION}"
        return True, message, None

    remediation = "rebuild/redeploy the weather-tracker image to match the host CLI version"
    return _safe_check("weather_tracker_version", "warning", remediation, _fn)


def _weather_checks(
    *,
    run: Callable[..., Any] = subprocess.run,
    fetch: Callable[..., Any] | None = None,
    env: Mapping[str, str] = os.environ,
    now: datetime | None = None,
    which: Callable[[str], str | None] = shutil.which,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> list[Check]:
    """The weather-tracker environment checks, ids prefixed ``weather_``.

    A thin orchestrator: each check (or check family) lives in its own
    ``_check_*`` function above, and this function only assembles them in
    order. Every external interaction is an injectable seam so tests never
    touch docker, a socket or the real disk:

    * ``run`` — the subprocess runner for ``docker compose ... ps``.
    * ``fetch`` — the HTTP GET used against the web API (see
      :func:`climate.cli._commands.weather.fetch`); defaults to that same
      function, resolved at call time so a test can monkeypatch
      ``climate.cli._commands.weather.fetch`` instead.
    * ``env`` — the environment mapping (base URL override, backup-age
      threshold, provider credentials). Defaults to ``os.environ``.
    * ``now`` — the wall clock instant.
    * ``which`` — the ``PATH`` lookup used for "is docker installed".
    * ``disk_usage`` — ``shutil.disk_usage``-shaped callable for the host
      disk headroom check.

    Severity policy: docker missing / stack not running / web API
    unreachable are ``error`` (this function only reaches them once a
    ``docker-compose.yml`` was found, i.e. a repo checkout); everything else
    is ``warning`` (or ``info`` for a disabled provider's fetch-age check).
    From a wheel install with no compose file, this returns a single
    ``info`` check and skips the rest entirely.
    """
    if fetch is None:
        fetch = weather.fetch
    if now is None:
        now = datetime.now(UTC)

    compose = stack.find_compose()
    if compose is None:
        return [
            {
                "id": "weather_managed_from_repo_checkout",
                "passed": True,
                "severity": "info",
                "message": (
                    "no docker-compose.yml found; the weather stack is managed from a "
                    "climate-cli repo checkout, not this wheel install"
                ),
                "remediation": "",
            }
        ]

    weather_cfg = _load_weather_config()
    base_url = _resolve_weather_base_url(env)

    checks: list[Check] = [_check_docker_available(which), _check_stack_running(compose, run)]

    api_check, health = _check_web_api_reachable(fetch, base_url)
    checks.append(api_check)

    # Only worth a second request when the API answered the first one.
    provider_rows = _fetch_provider_rows(fetch, base_url) if health is not None else None

    checks.extend(_check_fetch_ages(health, weather_cfg, env))
    checks.extend(_check_provider_credentials(weather_cfg, env, provider_rows))
    checks.append(_check_database_size(health))
    checks.append(_check_disk_headroom(env, disk_usage))
    checks.append(_check_backup_age(env))
    checks.append(_check_tracker_version(health))

    return checks


# --- rendering + top-level verb ----------------------------------------------


def _render_checks(checks: list[Check]) -> list[str]:
    lines: list[str] = []
    for check in checks:
        mark = "ok" if check["passed"] else "FAIL"
        lines.append(f"[{mark}] {check['id']}: {check['message']}")
        if not check["passed"] and check["remediation"]:
            lines.append(f"  hint: {check['remediation']}")
    return lines


def cmd_doctor(args: argparse.Namespace) -> int:
    identity_report = _diagnose()
    identity_checks = identity_report["checks"]
    weather_checks = _weather_checks(
        run=subprocess.run,
        fetch=weather.fetch,
        env=os.environ,
        which=shutil.which,
    )
    all_checks = list(identity_checks) + list(weather_checks)
    healthy = not any(not c["passed"] and c["severity"] == "error" for c in all_checks)

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result({"healthy": healthy, "checks": all_checks}, json_mode=True)
    else:
        status = "healthy" if healthy else "unhealthy"
        lines = [f"climate-cli doctor: {status}", "", "## identity"]
        lines.extend(_render_checks(identity_checks))
        lines.append("")
        lines.append("## weather tracker")
        lines.extend(_render_checks(weather_checks))
        emit_result("\n".join(lines), json_mode=False)
    return 0 if healthy else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Check the agent-identity invariants and the weather-tracker's runtime environment.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_doctor)
