"""``climate-cli learn`` — the learnability affordance.

Prints a structured self-teaching prompt describing the weather tracker. Must
satisfy the agent-first rubric: >=200 chars and mention purpose, command map,
exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from climate import __version__
from climate.cli._output import emit_result

_TEXT = """\
climate-cli — a multi-provider weather tracker and its agent-first CLI.

Purpose
-------
A docker stack (MongoDB + a tracker + a read-only web service) samples several
free weather providers on their own refresh policies, stores every response
verbatim, and serves the collection over HTTP. This CLI runs that stack and
queries what it collected. The first-class question is the LATEST READING:
an agent controlling hardware acts on it, so every value reports its own age
and provider.

Commands
--------
  climate-cli whoami             Identity from culture.yaml.
  climate-cli learn              This self-teaching prompt.
  climate-cli explain <path>...  Markdown docs for any noun/verb path.
  climate-cli overview           Descriptive snapshot of the agent.
  climate-cli doctor             Identity + weather-tracker environment checks.
  climate-cli cli overview       Describe the CLI surface itself.
  climate-cli stack up|down|status|overview
                                 Run the climate-weather compose stack.
  climate-cli weather latest|series|forecast|stats|overview
                                 Query the collected data via the HTTP API.
  climate-cli providers [--limits]
                                 Provider adapters: capabilities, auth, quota,
                                 freshness strategy, licence attribution.
  climate-cli backup dump|list|restore|overview
                                 Local backups of the weather database.

Getting the current conditions
------------------------------
  climate-cli weather latest --json --max-age 10m

Markdown is the default rendering for humans and agents alike; --json is for
code. Exit 3 means the data is older than --max-age, so a dead tracker's last
value can never be mistaken for a fresh one.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success (including "no data yet")
  1 user-input error (bad flag, bad path, missing confirmation)
  2 environment / setup error (docker, compose, daemon, web service down)
  3 stale data — 'weather latest --max-age' exceeded
  4+ reserved

More detail
-----------
  climate-cli explain climate-cli
  climate-cli explain weather latest
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "climate-cli",
        "version": __version__,
        "purpose": (
            "Track weather from several free providers into a local MongoDB "
            "stack, and query the collection (latest reading first)."
        ),
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {
                "path": ["doctor"],
                "summary": "Identity + weather-tracker environment checks.",
            },
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {"path": ["stack"], "summary": "Run the climate-weather compose stack."},
            {
                "path": ["weather", "latest"],
                "summary": "Newest reading per provider/location (the primary query).",
            },
            {"path": ["weather", "series"], "summary": "One variable over time."},
            {"path": ["weather", "forecast"], "summary": "Stored forecasts."},
            {
                "path": ["weather", "stats"],
                "summary": "Due-versus-stored fetch counts per provider.",
            },
            {
                "path": ["providers"],
                "summary": "Provider adapters: capabilities, auth, quota, freshness.",
            },
            {"path": ["backup"], "summary": "Dump, list and restore the weather database."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
            "3": "stale data ('weather latest --max-age' exceeded)",
        },
        "json_support": True,
        "explain_pointer": "climate-cli explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
