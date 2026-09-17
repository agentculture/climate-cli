"""Packaging invariants for the weather tracker.

The image installs ``.[weather]`` (see Dockerfile), and the web service
serves its dashboard from ``climate/weather/web/static/`` *inside the
installed package* — so those assets must ship in the wheel. Hatchling
selects files under ``packages = ["climate"]`` but honours the repo's VCS
ignore rules, so an asset that is untracked or gitignored silently
disappears from the wheel. These tests assert exactly that precondition
(plus the dependency contract) without shelling out to a build: the
built-wheel contents were verified manually with ``uv build`` (see the
task report), and a build in the unit suite would be neither fast nor
hermetic.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
STATIC = REPO_ROOT / "climate" / "weather" / "web" / "static"

#: The dashboard assets the wheel must carry (Dockerfile installs `.[weather]`).
REQUIRED_ASSETS = (
    "index.html",
    "dashboard.css",
    "tokens.css",
    "js/api.js",
    "js/app.js",
    "js/chart.js",
    "js/format.js",
    "js/panels.js",
    "js/series.js",
)


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_runtime_has_no_dependencies(pyproject: dict) -> None:
    assert pyproject["project"]["dependencies"] == []


def test_weather_extra_holds_pymongo(pyproject: dict) -> None:
    extras = pyproject["project"]["optional-dependencies"]
    assert "weather" in extras
    assert any(spec.startswith("pymongo") for spec in extras["weather"])


def test_dev_group_has_pyyaml(pyproject: dict) -> None:
    # tests/weather/test_compose.py parses docker-compose.yml with PyYAML and
    # imports it outright — it must never silently skip.
    dev = pyproject["dependency-groups"]["dev"]
    assert any(spec.lower().startswith("pyyaml") for spec in dev)


def test_wheel_packages_the_climate_tree(pyproject: dict) -> None:
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["climate"]


@pytest.mark.parametrize("relative", REQUIRED_ASSETS)
def test_static_asset_exists(relative: str) -> None:
    assert (STATIC / relative).is_file()


@pytest.mark.parametrize("relative", REQUIRED_ASSETS)
def test_static_asset_is_tracked_and_not_ignored(relative: str) -> None:
    """A gitignored or untracked asset is dropped from the wheel by hatchling."""
    path = STATIC / relative
    ignored = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "check-ignore", "-q", str(path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    assert ignored.returncode != 0, f"{relative} is gitignored — it would not ship in the wheel"

    tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "ls-files", "--error-unmatch", str(path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    assert tracked.returncode == 0, f"{relative} is untracked — it would not ship in the wheel"


def test_dist_is_gitignored() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        # Trailing slash: the `dist/` ignore pattern matches directories, and
        # `dist` may not exist on disk for git to classify.
        ["git", "check-ignore", "-q", f"{REPO_ROOT / 'dist'}/"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "dist/ must be gitignored so build artefacts never land in git"
