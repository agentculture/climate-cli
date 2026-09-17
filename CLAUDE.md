# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this repo is

**climate-cli** is a multi-provider weather tracker and its agent-first CLI.
A docker compose project (`climate-weather`) runs MongoDB, a polling tracker
and a read-only web service; the `climate` CLI runs that stack and queries the
collection. The intended consumer is an agent controlling hardware from
measured conditions, so `weather latest` is the first-class verb and staleness
is a first-class outcome (exit `3`).

Read [`README.md`](README.md) for the user-facing story, the provider table and
the privacy rules; [`docs/weather-api.md`](docs/weather-api.md) for the HTTP API
contract; and `docs/specs/` + `docs/plans/` for how this was specced and built.

## Architecture map

### `climate/cli/` — the host CLI (standard library only)

Argparse noun/verb. `_build_parser()` in `climate/cli/__init__.py` is the one
registration point: each noun group is a module under `_commands/` exposing
`register(sub)`.

| Module | Role |
|--------|------|
| `__init__.py` | Parser assembly, `_dispatch` (exceptions → exit codes), `main()`. |
| `_errors.py` | `CliError`, `EXIT_USER_ERROR=1`, `EXIT_ENV_ERROR=2`, `EXIT_STALE=3`. |
| `_output.py` | `emit_result` / `emit_error` / `emit_diagnostic` — stdout vs stderr. |
| `_commands/whoami,learn,explain,overview,doctor,cli` | The agent-first global verbs. |
| `_commands/stack.py` | `docker compose` wrapper for the `climate-weather` project. |
| `_commands/weather.py` | Queries the HTTP API. Single `fetch()` seam; never touches Mongo. |
| `_commands/providers.py` | Reads the adapter registry. No network, no Mongo. |
| `_commands/backup.py` | `mongodump`/`mongorestore` via `docker compose exec -T`. |
| `climate/explain/catalog.py` | Path-keyed markdown for `climate explain`. |

**The host CLI must stay importable with every third-party package blocked.**
`pymongo` is in the `weather` extra, lazy-imported inside `climate.weather.mongo`
only. `tests/test_stdlib_only.py` enforces this in a subprocess.

### `climate/weather/` — the service

| Module | Role |
|--------|------|
| `config.py` | User config (labelled locations, per-provider settings), coordinate rounding. |
| `providers/base.py` | The `WeatherProvider` ABC: metadata, `build_requests`, `normalize`. |
| `providers/__init__.py` | Auto-discovering registry — a new adapter module needs no edit here. |
| `providers/{open_meteo,met_no,openweather,ims,metar,ims_forecast}.py` | The six adapters. |
| `http.py` | `fetch()` with an injectable opener; `redact` / `redact_headers`. |
| `store.py` | `FetchRecord`, `Reading`, `Measurement`, `WeatherStore`, `InMemoryWeatherStore`, `redact_url`. |
| `mongo.py` | `build_store()`, the connection guard, and the tracker lease. |
| `scheduler.py` | The tick loop: due-ness, per-fetch timeouts, jitter, error kinds. |
| `rederive.py` | Rebuild normalized readings from stored raw bytes. |
| `tracker.py` | `python -m climate.weather.tracker` — thin wiring only. |
| `web/api.py` + `web/__main__.py` | Pure route handlers + a thin `http.server` wrapper. |
| `web/static/` | Build-free dashboard: vanilla ES modules, inline SVG charts. |

## The stack workflow

```bash
cp docker/weather.env.example docker/weather.env   # gitignored; keys live here only
$EDITOR ~/.config/climate-cli/weather.json          # locations; NEVER in the repo
uv run climate stack up            # docker compose up -d --build
uv run climate stack status        # per-service state + health
uv run climate weather latest      # the primary query (dashboard: :8095)
uv run climate backup dump         # local archive, outside the repo and the volume
uv run climate doctor              # identity + tracker environment checks
uv run climate stack down          # stops; the named volume is PRESERVED
```

`stack` works from a **repo checkout** only — a wheel install ships no
`docker-compose.yml` and the verbs exit `2` saying so. A published image is
[issue #6](https://github.com/agentculture/climate-cli/issues/6).

**Never** issue a command that drops the `weather-mongodb-data` volume — the
compose teardown flag that removes volumes, and the ad-hoc volume-removal and
volume-pruning subcommands, are all banned. `tests/weather/test_compose.py`
scans `climate/`, `scripts/`, `docker/`, the `Dockerfile` and
`docker-compose.yml` for those literal command forms and fails on a hit, so do
not quote them verbatim in code or comments there either. Backups are the
answer: `climate backup dump`.

## Tests and lint

```bash
uv run pytest -n auto -q                  # full suite; seconds, no network/docker/Mongo
uv run pytest -n auto --cov=climate -q    # coverage gate: 60%
uv run black --check climate tests
uv run isort --check-only climate tests
uv run flake8 climate tests               # line length 100
uv run bandit -c pyproject.toml -r climate
uv run teken cli doctor . --strict        # the agent-first rubric gate CI runs
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
```

Do **not** run `uv sync --all-extras`: installing `pymongo` into the dev
environment fails `tests/weather/test_mongo.py`, which asserts the driver is
absent (that is how the lazy-import contract is proven). Plain `uv sync` is
correct.

## Conventions that bit this build

Read these before writing code here; each one cost time to learn.

- **Location privacy is enforced, not advised.** Never write a real place name
  or a coordinate literal next to `lat`/`lon`/`latitude`/`longitude` anywhere in
  `climate/`, `tests/` (outside `tests/fixtures/`) or the docker files.
  `tests/test_repo_hygiene.py` scans for it and fails the build. In tests use
  `from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT` and build
  URLs with f-strings from those; a literal `0.0` placeholder is allowed. The
  same rule covers the public `.eidetic/memory` store — never `/remember` a
  location or a key.
- **No sockets in tests.** `tests/conftest.py` blocks `socket.connect`
  suite-wide. Inject the seam instead: a fake opener into `climate.weather.http`,
  a fake `fetch` into `climate/cli/_commands/weather.py`, a fake
  `subprocess.run` into `stack.py` / `backup.py`, and
  `InMemoryWeatherStore` for storage. Nothing needs docker or MongoDB to run.
- **The contracts are in three files.** `climate/weather/store.py` (record and
  reading shapes), `climate/weather/providers/base.py` (the adapter contract)
  and `docs/weather-api.md` (the HTTP surface and the shared vocabulary). Build
  against them; if one genuinely cannot express what you need, say so rather
  than working around it silently.
- **The vocabulary is shared, not a filter — no provider value is dropped.**
  `docs/weather-api.md` §4: a field with no vocabulary row is still emitted, as
  `x_<provider_field_name>`, with its original value and unit preserved. Only
  coordinates are deliberately excluded.
- **Markdown is the default output; `--json` is for code.** Every verb supports
  both. Results to stdout, errors and diagnostics to stderr, never mixed.
  Failures raise `CliError` — a traceback reaching a user is a bug.
- **Every noun with action verbs needs an `overview`**, every verb needs
  `--json`, and every noun/verb path needs an entry in
  `climate/explain/catalog.py`. `tests/test_explain_covers_cli.py` walks the
  real argparse tree, and `teken cli doctor . --strict` checks the rubric in CI.
- **Every PR bumps the version.** Run the `version-bump` skill (minor for a
  feature, patch for a fix) before opening the PR: it updates `pyproject.toml`
  and prepends a Keep-a-Changelog entry. CI's version-check job fails any PR
  whose version equals `main`. `climate/__init__.py` reads `__version__` from
  package metadata, so it needs no manual edit.
- **CI has no network, no docker, no API keys, and no browser.** Anything that
  needs one of those is evidenced agent-side (playwright MCP for the dashboard)
  and kept out of the suite. Dashboard static assets are analysed by SonarCloud
  but excluded from *coverage* (`sonar-project.properties`).

## Conventions and workflow

**Memory discipline — recall before, remember after.** This repo keeps its
eidetic memory **in-repo and public**: records resolve to
`<repo-root>/.eidetic/memory` — committed, and shared with the team and mesh
peers (the `claude` and `colleague` backends both read the same
`climate-cli` scope), so memory travels with the repo, not a private
home-dir store. Make it a per-task habit:

- **`/recall` before you start.** Search the store for the area you're about
  to touch — prior decisions, gotchas, "have we done this before?" — so you
  build on what's already known instead of re-deriving it. Do this before
  non-trivial tasks, not just when asked.
- **`/remember` when something worth keeping surfaces.** A non-obvious
  decision and its rationale, a constraint, a fix and *why* it was needed, a
  gotcha that cost time, a fact the next session would otherwise re-learn.
  Capture it as it happens, not at the end when it's faded.

A plain `/remember` lands the note in `./.eidetic/memory` in this repo — no
flag needed (the wrappers here default to `--visibility public`; in-repo
routing needs `eidetic >= 0.10.0`, older CLIs keep records in `$HOME`). Keep
something out of the committed store only by passing `--visibility private`
(routes to `$HOME/.eidetic/memory`, never committed); `/recall` reads both
stores and merges. Don't store what the repo already records (code structure,
git history, what's already in this file or `CHANGELOG.md`) — store what you'd
have to re-derive. These are the `recall`/`remember` skills (`.claude/skills/`),
backed by the `eidetic` store.
