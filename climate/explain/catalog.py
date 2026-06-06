"""Markdown catalog for ``climate-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("climate-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# climate-cli

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `climate-cli whoami` — identity probe from `culture.yaml`.
- `climate-cli learn` — structured self-teaching prompt.
- `climate-cli explain <path>` — markdown docs for any noun/verb.
- `climate-cli overview` — descriptive snapshot of the agent.
- `climate-cli doctor` — check the agent-identity invariants.
- `climate-cli cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `climate-cli explain whoami`
- `climate-cli explain doctor`
"""

_WHOAMI = """\
# climate-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    climate-cli whoami
    climate-cli whoami --json
"""

_LEARN = """\
# climate-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    climate-cli learn
    climate-cli learn --json
"""

_EXPLAIN = """\
# climate-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    climate-cli explain climate-cli
    climate-cli explain whoami
    climate-cli explain --json <path>
"""

_OVERVIEW = """\
# climate-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    climate-cli overview
    climate-cli overview --json
"""

_DOCTOR = """\
# climate-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    climate-cli doctor
    climate-cli doctor --json
"""

_CLI = """\
# climate-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    climate-cli cli overview
    climate-cli cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("climate-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
