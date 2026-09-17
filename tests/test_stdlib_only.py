"""The host CLI must import with every third-party package blocked.

Spec c18/h14: ``dependencies = []``. A plain ``pip install climate-cli``
carries no third-party package at all, so ``climate whoami``, ``learn``,
``explain``, ``overview``, ``doctor``, ``cli overview`` and every weather
noun must work with nothing but the standard library on the path. ``pymongo``
lives in the ``weather`` extra and is lazy-imported inside the *service*
code only (``climate.weather.mongo``), which this check deliberately does
not import.

Why a subprocess: proving this in-process would mean installing a
meta-path blocker and re-importing modules that are already in
``sys.modules``, leaving duplicate module objects behind. That bit us once
already — a re-imported ``climate.cli._errors`` gave a second ``CliError``
class, and ``except CliError`` in another test then failed by class
identity, flakily and only under ``pytest-xdist``. So the whole check runs
in a fresh interpreter (``sys.executable -c ...``) whose ``sys.modules`` is
its own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every host-side module an operator's CLI invocation can reach.
HOST_MODULES = [
    "climate",
    "climate.cli",
    "climate.cli._errors",
    "climate.cli._output",
    "climate.cli._commands",
    "climate.cli._commands.backup",
    "climate.cli._commands.cli",
    "climate.cli._commands.doctor",
    "climate.cli._commands.explain",
    "climate.cli._commands.learn",
    "climate.cli._commands.overview",
    "climate.cli._commands.providers",
    "climate.cli._commands.stack",
    "climate.cli._commands.weather",
    "climate.cli._commands.whoami",
    "climate.explain",
    "climate.explain.catalog",
    "climate.weather.config",
    "climate.weather.http",
    "climate.weather.store",
    "climate.weather.providers",
    "climate.weather.providers.base",
    "climate.weather.providers.ims",
    "climate.weather.providers.ims_forecast",
    "climate.weather.providers.met_no",
    "climate.weather.providers.metar",
    "climate.weather.providers.open_meteo",
    "climate.weather.providers.openweather",
]

# Runs in a *fresh* interpreter. Blocks anything that is neither a stdlib
# module nor part of the `climate` package, then imports every host module
# and builds the real parser.
_PROBE = r"""
import importlib, sys, sysconfig

ALLOWED_PREFIXES = ("climate",)
STDLIB = set(sys.stdlib_module_names)


class ThirdPartyBlocked(ImportError):
    pass


class Blocker:
    def find_module(self, fullname, path=None):  # legacy API, unused
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in STDLIB or root in ALLOWED_PREFIXES:
            return None
        raise ThirdPartyBlocked(
            "third-party import attempted by the host CLI: " + fullname
        )


sys.meta_path.insert(0, Blocker())

MODULES = %(modules)r
for name in MODULES:
    importlib.import_module(name)

# The parser wires every noun group together; building it is what an actual
# `climate ...` invocation does first.
import climate.cli

climate.cli._build_parser()

# Nothing third-party may have slipped in via an already-imported module.
leaked = sorted(
    n
    for n in sys.modules
    if n
    and "." not in n
    and n not in STDLIB
    and not n.startswith("_")
    and n not in ALLOWED_PREFIXES
    and getattr(sys.modules[n], "__file__", None)
    and "site-packages" in str(sys.modules[n].__file__)
)
if leaked:
    raise SystemExit("third-party modules loaded: " + ", ".join(leaked))
print("OK", sysconfig.get_python_version())
"""


def test_host_cli_imports_with_third_party_blocked() -> None:
    code = _PROBE % {"modules": HOST_MODULES}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "host CLI is not standard-library only:\n" f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert proc.stdout.startswith("OK")


def test_probe_blocker_actually_blocks() -> None:
    """Guard the guard: the blocker must fail on a genuine third-party import."""
    code = _PROBE % {"modules": HOST_MODULES + ["pytest"]}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "third-party import attempted" in proc.stderr
