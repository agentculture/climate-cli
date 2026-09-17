"""Tests for docker-compose.yml (the weather-tracking compose project).

These tests parse docker-compose.yml with PyYAML and assert the acceptance
criteria from the plan (task t8):

  - no host port on weather-mongodb by default
  - every service sets json-file logging with max-size/max-file
  - the named volume weather-mongodb-data exists and is used by mongo
  - no command in the repo removes that volume (no `down -v`, `--volumes`,
    `volume rm`, `volume prune`)

No docker/compose CLI is invoked here (CI has no docker) — everything is a
plain YAML/text parse.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# PyYAML is a declared dev dependency (pyproject's `dev` group), imported
# outright rather than via importorskip: these are data-safety assertions
# about the compose file and must never skip themselves away silently.

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


def _resolve_extends(services: dict) -> dict:
    """Shallow-resolve `extends:` so per-service assertions (e.g. logging)
    see inherited fields the same way `docker compose config` does, without
    invoking the docker CLI.
    """
    resolved = {}
    for name, svc in services.items():
        extends = svc.get("extends") if isinstance(svc, dict) else None
        if isinstance(extends, dict) and extends.get("service") in services:
            base = dict(services[extends["service"]])
            merged = dict(base)
            merged.update({k: v for k, v in svc.items() if k != "extends"})
            resolved[name] = merged
        else:
            resolved[name] = svc
    return resolved


@pytest.fixture(scope="module")
def compose() -> dict:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    data["services"] = _resolve_extends(data["services"])
    return data


def test_compose_file_exists():
    assert COMPOSE_PATH.is_file(), "docker-compose.yml must exist at the repo root"


def test_compose_project_name(compose):
    assert compose.get("name") == "climate-weather"


def test_mongo_service_present_and_pinned(compose):
    services = compose["services"]
    assert "weather-mongodb" in services
    mongo = services["weather-mongodb"]
    assert mongo["image"] == "mongo:8.0"
    assert mongo.get("restart") == "unless-stopped"


def test_mongo_healthcheck_is_mongosh_ping(compose):
    mongo = compose["services"]["weather-mongodb"]
    healthcheck = mongo.get("healthcheck")
    assert healthcheck is not None, "weather-mongodb must define a healthcheck"
    test_cmd = healthcheck.get("test")
    assert test_cmd is not None
    flattened = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert "mongosh" in flattened
    assert "ping" in flattened.lower() or "adminCommand" in flattened


def test_mongo_has_no_published_port_by_default(compose):
    """weather-mongodb must not publish any host port in the default
    (no-profile) service definition. The optional debug publish lives on a
    *separate* service gated by the `debug-mongo` profile, so it does not
    affect this assertion.
    """
    mongo = compose["services"]["weather-mongodb"]
    assert "ports" not in mongo or not mongo["ports"]


def test_debug_mongo_publish_is_profile_gated_and_off_by_default(compose):
    services = compose["services"]
    debug_services = {
        name: svc
        for name, svc in services.items()
        if any("27020" in str(p) for p in svc.get("ports", []))
    }
    assert debug_services, "expected some service to publish host port 27020 for debug"
    for name, svc in debug_services.items():
        assert name != "weather-mongodb", "the debug publish must not be on the default service"
        profiles = svc.get("profiles") or []
        assert profiles, f"{name} must be gated behind a compose profile (off by default)"


def test_mongo_named_volume(compose):
    volumes = compose.get("volumes") or {}
    assert "weather-mongodb-data" in volumes

    mongo = compose["services"]["weather-mongodb"]
    mongo_volumes = mongo.get("volumes") or []
    assert any("weather-mongodb-data" in str(v) for v in mongo_volumes)


def test_tracker_and_web_services_present(compose):
    services = compose["services"]
    assert "weather-tracker" in services
    assert "weather-web" in services

    tracker = services["weather-tracker"]
    web = services["weather-web"]

    assert tracker.get("restart") == "unless-stopped"
    assert web.get("restart") == "unless-stopped"

    # Both are built from the one Dockerfile at the repo root.
    for svc in (tracker, web):
        build = svc.get("build")
        assert build is not None
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
        assert dockerfile in (None, "Dockerfile")


def test_web_service_publishes_on_configurable_bind_and_port(compose):
    web = compose["services"]["weather-web"]
    ports = web.get("ports") or []
    assert ports, "weather-web must publish a port"

    joined = " ".join(str(p) for p in ports)
    assert "CLIMATE_WEB_BIND" in joined
    assert "CLIMATE_WEB_PORT" in joined
    # Default bind must be loopback-only.
    assert "127.0.0.1" in joined
    assert "8095" in joined


def test_every_service_has_json_file_logging_with_limits(compose):
    services = compose["services"]
    for name, svc in services.items():
        logging_cfg = svc.get("logging")
        assert logging_cfg is not None, f"{name} must configure logging"
        assert logging_cfg.get("driver") == "json-file", f"{name} must use the json-file driver"
        options = logging_cfg.get("options") or {}
        assert "max-size" in options, f"{name} must set logging max-size"
        assert "max-file" in options, f"{name} must set logging max-file"


def test_secrets_come_from_gitignored_env_file(compose):
    services = compose["services"]
    for name in ("weather-tracker", "weather-web"):
        svc = services[name]
        env_file = svc.get("env_file")
        assert env_file is not None, f"{name} must load secrets via env_file"
        flattened = str(env_file)
        assert "docker/weather.env" in flattened


def test_env_example_documents_every_required_variable():
    example_path = REPO_ROOT / "docker" / "weather.env.example"
    assert example_path.is_file()
    text = example_path.read_text(encoding="utf-8")

    required_vars = [
        "CLIMATE_OPENWEATHER_API_KEY",
        "CLIMATE_IMS_API_TOKEN",
        "WEATHER_MONGO_URI",
        "CLIMATE_WEB_BIND",
        "CLIMATE_WEB_PORT",
        "CLIMATE_WEATHER_CONFIG_DIR",
    ]
    for var in required_vars:
        assert var in text, f"weather.env.example must document {var}"

    # The optional debug mongo publish must be documented too, even though
    # it's a profile flag rather than an env var.
    assert "debug-mongo" in text or "27020" in text

    # No real-looking secrets or committed values — every var line must be
    # followed by an obvious placeholder, not a real key. This is a light
    # heuristic, not a secret scanner: reject suspiciously long
    # alphanumeric values that don't contain "REPLACE" or common placeholder
    # markers.
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        _, _, value = line.partition("=")
        value = value.strip()
        if not value:
            continue
        safe_markers = ("REPLACE", "127.0.0.1", "8095", "./", "weather-mongodb")
        assert any(
            marker in value for marker in safe_markers
        ), f"suspicious non-placeholder value in weather.env.example: {line!r}"


def test_config_path_is_mounted_read_only_via_variable(compose):
    services = compose["services"]
    for name in ("weather-tracker", "weather-web"):
        svc = services[name]
        volumes = svc.get("volumes") or []
        flattened = " ".join(str(v) for v in volumes)
        assert "CLIMATE_WEATHER_CONFIG_DIR" in flattened
        assert ":ro" in flattened or "read_only" in flattened.lower()


def test_config_mount_default_is_outside_the_repo(compose):
    """The user's location config is private data and must not default to
    an in-repo/relative path (e.g. ./config) — it must default outside the
    repo, matching the config task's own default of
    $XDG_CONFIG_HOME/climate-cli (~/.config/climate-cli).
    """
    services = compose["services"]
    for name in ("weather-tracker", "weather-web"):
        svc = services[name]
        volumes = svc.get("volumes") or []
        flattened = " ".join(str(v) for v in volumes)

        # The default (fallback after `:-`) must not be a relative path.
        assert "CLIMATE_WEATHER_CONFIG_DIR:-./" not in flattened
        assert "CLIMATE_WEATHER_CONFIG_DIR:-config" not in flattened

        # It must resolve to somewhere under the user's home / XDG config,
        # not a bare relative-looking default.
        assert (
            "${HOME}/.config/climate-cli" in flattened or "$HOME/.config/climate-cli" in flattened
        ), f"{name}: config mount default must live outside the repo, got: {flattened!r}"


def test_services_set_config_path_env_var(compose):
    services = compose["services"]
    for name in ("weather-tracker", "weather-web"):
        svc = services[name]
        env = svc.get("environment") or {}
        assert (
            "CLIMATE_WEATHER_CONFIG_PATH" in env
        ), f"{name} must set CLIMATE_WEATHER_CONFIG_PATH so it reads the mounted config file"
        assert env["CLIMATE_WEATHER_CONFIG_PATH"] == "/app/config/weather.json"


def test_gitignore_covers_the_weather_env_file():
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docker/weather.env" in gitignore_text


# --- "no command in the repo removes the mongo volume" -----------------

FORBIDDEN_VOLUME_PATTERNS = ("down -v", "--volumes", "volume rm", "volume prune")

# Directories to scan for the forbidden patterns, per the brief.
SCAN_DIRS = ("climate", "scripts", "docker")
# Extra top-level files to scan directly.
SCAN_FILES = ("Dockerfile", "docker-compose.yml")

# Binary-ish / irrelevant extensions to skip.
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".lock", ".ico"}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _candidate_paths() -> list[Path]:
    tracked = set(_tracked_files())
    candidates: list[Path] = []
    for rel_dir in SCAN_DIRS:
        base = REPO_ROOT / rel_dir
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path in tracked and path.suffix not in SKIP_SUFFIXES:
                candidates.append(path)
    for rel_file in SCAN_FILES:
        path = REPO_ROOT / rel_file
        if path.is_file() and path in tracked:
            candidates.append(path)
    return candidates


@pytest.mark.skipif(sys.platform == "win32", reason="git ls-files path handling assumes posix")
def test_no_command_removes_the_mongo_volume():
    offenders = []
    for path in _candidate_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in FORBIDDEN_VOLUME_PATTERNS:
            if pattern in text:
                offenders.append((str(path.relative_to(REPO_ROOT)), pattern))
    assert not offenders, f"found volume-destroying command(s): {offenders}"


def test_web_container_listens_on_all_interfaces_inside_but_maps_to_loopback_outside() -> None:
    """Found by live validation: a loopback listener INSIDE the container is
    unreachable through docker's port forward (connection reset). Exposure is
    decided by the host side of the mapping, never by the in-container bind."""
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web = yaml.safe_load(text)["services"]["weather-web"]
    assert web["environment"]["CLIMATE_WEB_BIND"] == "0.0.0.0"  # nosec B104 - in-container only
    assert web["environment"]["CLIMATE_WEB_PORT"] == "8095"
    assert web["ports"] == ["${CLIMATE_WEB_BIND:-127.0.0.1}:${CLIMATE_WEB_PORT:-8095}:8095"]
