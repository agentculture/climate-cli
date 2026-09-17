"""Markdown catalog for ``climate-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("climate-cli",)``, and ``("climate",)`` (the installed console-script name)
all resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads. Every path the real argparse tree accepts
must have an entry here — ``tests/test_explain_covers_cli.py`` walks the
parser and fails on any gap.
"""

from __future__ import annotations

_ROOT = """\
# climate-cli

A multi-provider weather tracker and its agent-first CLI. A docker stack
(MongoDB + a tracker + a read-only web service) samples several free weather
providers on their own refresh policies, stores every response verbatim, and
serves the collection over HTTP; this CLI runs that stack and queries it.

The first-class question is **the latest reading**, because the consumer is an
agent controlling hardware from measured conditions.

## Nouns and verbs

- `climate-cli whoami` — identity probe from `culture.yaml`.
- `climate-cli learn` — structured self-teaching prompt.
- `climate-cli explain <path>` — markdown docs for any noun/verb.
- `climate-cli overview` — descriptive snapshot of the agent.
- `climate-cli doctor` — identity + weather-tracker environment checks.
- `climate-cli cli overview` — describe the CLI surface.
- `climate-cli stack up|down|status|overview` — run the compose stack.
- `climate-cli weather latest|series|forecast|stats|overview` — query the data.
- `climate-cli providers [--limits]` — provider adapters and their metadata.
- `climate-cli backup dump|list|restore|overview` — local database backups.

## Output contract

Markdown is the default rendering — it is what both humans and agents read.
`--json` is supported on **every** command and returns the same data as a
structured payload (for `weather` verbs, the API payload unchanged). Results
go to stdout; errors and progress diagnostics go to stderr, never mixed.

## Exit-code policy

- `0` success (including "no data yet")
- `1` user-input error (bad flag, bad path, missing confirmation)
- `2` environment / setup error (docker, compose, daemon, web service down)
- `3` stale data — `weather latest --max-age` exceeded
- `4+` reserved

Exit `3` exists so an agent controlling hardware cannot silently act on a dead
tracker's last value: a well-formed `latest` response is not proof of freshness.

## See also

- `climate-cli explain stack`
- `climate-cli explain weather`
- `climate-cli explain providers`
- `climate-cli explain backup`
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

Prints a structured self-teaching prompt covering purpose, the command map,
the exit-code policy (including `3` = stale), `--json` support, and the
`explain` pointer. The intended first command for a new agent consumer.

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
    climate-cli explain weather latest
    climate-cli explain --json <path>
"""

_OVERVIEW = """\
# climate-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`),
the verb surface — including the weather-tracker nouns — and the
sibling-pattern artifacts this repo carries. Accepts an ignored `target` so a
stray path never hard-fails.

## Usage

    climate-cli overview
    climate-cli overview --json
"""

_DOCTOR = """\
# climate-cli doctor

Environment and identity checks in the rubric shape
`{healthy, checks: [{id, passed, severity, message, remediation}]}`.

## Checks

- **identity** — `prompt-file-present`, `backend-consistency`
  (`claude` → `CLAUDE.md`), skills present.
- **weather tracker** — the stack is managed from a repo checkout (a wheel
  install ships no `docker-compose.yml`), newest fetch age per provider,
  database size, provider credentials present for providers that require
  them, and backup freshness.

Runs sensibly from a wheel install with no `culture.yaml`. A stopped stack
yields failed checks with remediation, never an exception. Exits `1` when
unhealthy.

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

_STACK = """\
# climate-cli stack

Runs the `climate-weather` docker compose project defined by this repo's
`docker-compose.yml`: `weather-mongodb` (storage), `weather-tracker` (the
polling loop) and `weather-web` (the read-only HTTP API + dashboard).

## Verbs

- `stack up` — `docker compose up -d --build`.
- `stack down` — `docker compose down` (the named volume is preserved).
- `stack status` — per-service state and health.
- `stack overview` — describe this noun.

The bare `climate-cli stack` prints the overview.

## Prerequisites

- A repo checkout: a wheel install ships no `docker-compose.yml`, so the
  stack verbs exit `2` with that hint (a published image is a follow-up:
  <https://github.com/agentculture/climate-cli/issues/6>).
- `docker/weather.env` — copy `docker/weather.env.example` and fill it in.
  It is gitignored; it is the only place provider keys ever live.
- A location config at `~/.config/climate-cli/weather.json` (never in the
  repo). With none configured the tracker refuses to start.

## Exit codes

`0` success, `2` docker missing / compose plugin missing / daemon
unreachable / no compose file / missing `docker/weather.env`. Every verb
supports `--json`.

## Data safety

No verb in this CLI ever removes the `weather-mongodb-data` volume — not
`down`, not any other command. Take a copy with `climate-cli backup dump`.
"""

_STACK_UP = """\
# climate-cli stack up

Brings the whole weather stack up: `docker compose up -d --build` over the
repo's `docker-compose.yml` (project `climate-weather`).

Fails fast with exit `2` and a hint when docker, the compose plugin, the
daemon or `docker/weather.env` is missing — never a traceback.

## Usage

    climate-cli stack up
    climate-cli stack up --json

Then: `climate-cli stack status`, `climate-cli weather latest`, and the
dashboard at <http://127.0.0.1:8095>.
"""

_STACK_DOWN = """\
# climate-cli stack down

Stops the stack (`docker compose down --remove-orphans`). The named volume
`weather-mongodb-data` is **preserved** — collected data survives a `down`
and an image rebuild. `--remove-orphans` only drops containers that are no
longer in the compose file.

## Usage

    climate-cli stack down
    climate-cli stack down --json
"""

_STACK_STATUS = """\
# climate-cli stack status

Reports every compose service with its state and health, from
`docker compose ps --all --format json`. Markdown table by default; `--json`
gives `{compose_file, running, healthy, services: [...]}`.

An empty `health` means the service declares no healthcheck (or it has not
been evaluated yet) and counts as OK; a reported `unhealthy`/`starting` does
not.

## Usage

    climate-cli stack status
    climate-cli stack status --json
"""

_WEATHER = """\
# climate-cli weather

Queries the collected weather data through the read-only HTTP API
(`docs/weather-api.md`). This noun **never** connects to MongoDB: it speaks
only to the web service, so the host CLI stays dependency-free.

Base URL: `$CLIMATE_WEATHER_URL`, default <http://127.0.0.1:8095>; routes
live under `/api/v1`.

## Verbs

- `weather latest` — newest reading per (provider, location). The primary
  query.
- `weather series` — one variable over time.
- `weather forecast` — stored forecasts.
- `weather stats` — due-versus-stored fetch counts per provider.
- `weather overview` — describe this noun.

The bare `climate-cli weather` prints the overview.

## Output and exit codes

Markdown by default; `--json` prints the API payload structure unchanged.
`0` success (including "no data yet"), `1` a bad flag value, `2` the web
service is unreachable (connection failure, timeout, or a `503` from the
API), `3` `--max-age` exceeded.

## Privacy

Locations are identified by their user-chosen **label** only. No response and
no dashboard asset ever carries a latitude or longitude.
"""

_WEATHER_LATEST = """\
# climate-cli weather latest

The newest reading per (provider, location) — the first-class query, because
an agent controlling hardware acts on it. Every value carries its own age and
its provider, so a stale reading is never mistaken for a fresh one.

## Usage

    climate-cli weather latest
    climate-cli weather latest --provider open-meteo --location home
    climate-cli weather latest --max-age 10m --json

## Flags

- `--provider <id>` — repeatable; restrict to these provider ids.
- `--location <label>` — repeatable; restrict to these location labels.
- `--max-age <seconds|5m|1h>` — staleness threshold.
- `--json` — the API payload unchanged (it carries a `stale` flag).

## Staleness

With `--max-age`, a freshest reading older than the threshold exits **3**
(`EXIT_STALE`) in both markdown and `--json` mode, and the JSON payload's
`stale` is `true`. Without `--max-age` the age is still reported, but the
exit code stays `0`.
"""

_WEATHER_SERIES = """\
# climate-cli weather series

One variable over time, gridded server-side. `--variable` is required.

## Usage

    climate-cli weather series --variable temperature
    climate-cli weather series --variable temperature --step 900 --agg mean --json

## Flags

`--variable` (required), `--provider`, `--location`, `--kind`
(`observation` | `model` | `forecast`, repeatable), `--from`, `--to`,
`--step <seconds>`, `--agg` (`last` | `first` | `mean` | `min` | `max`,
default `last`), `--max-points`, `--json`.

Gaps are honest: a tick that was never collected stays empty. Nothing is
interpolated or back-filled.
"""

_WEATHER_FORECAST = """\
# climate-cli weather forecast

Forecasts stored alongside the observations they arrived with. Every provider
response is kept in full, so forecast blocks are retained even though the
latest-reading path does not read them.

## Usage

    climate-cli weather forecast
    climate-cli weather forecast --provider met-no --horizon-hours 48 --json

## Flags

`--provider`, `--location`, `--variables <a,b,c>`, `--issued-at`,
`--horizon-hours`, `--step-hours`, `--json`.
"""

_WEATHER_STATS = """\
# climate-cli weather stats

Due-versus-stored fetch counts per provider over a window, measured from the
stored fetch records themselves — never estimated. This is how you check the
"95% of due fetches stored over 24 h" success signal.

## Usage

    climate-cli weather stats
    climate-cli weather stats --window 24h --json

## Flags

`--window <90m|24h|7d>` (default 24h), `--from`, `--to`, `--provider`,
`--location`, `--bucket <seconds>`, `--json`.
"""

_PROVIDERS = """\
# climate-cli providers

Lists every registered weather provider adapter with its capabilities, auth
requirement, enabled state and reason, freshness strategy and licence
attribution. Reads the adapter registry only — no network, no MongoDB, and
never a location name or coordinate.

The bare `climate-cli providers` **is** the listing; `providers overview`
describes the noun.

## Usage

    climate-cli providers
    climate-cli providers --limits
    climate-cli providers --json

## Flags

- `--limits` — add the declared quota columns: calls/min, calls/day,
  calls/month, plus `verified` and `source`. A quota marked `verified: no`
  was taken from issue 5 or third-party pages, not from the provider's own
  documentation — treat it as a working assumption.
- `--json` — `{"providers": [...]}` with the full metadata rows.

## Freshness strategies

- `interval` — re-fetch once the provider's own interval has elapsed.
- `http_expires` — honour `Expires`/`Last-Modified` with a conditional GET
  (met-no; a `304` is still stored as a fetch record).
- `station_cadence` — follow the reporting station's own cadence.

A provider that requires a credential and has none is reported disabled,
with the missing environment variable named — it never fails the command.
"""

_BACKUP = """\
# climate-cli backup

Local, first-class backups of the collected weather database. Because
`weather-mongodb` publishes no host port, every verb runs through
`docker compose exec -T` — no `pymongo`, no open socket.

## Verbs

- `backup dump` — `mongodump --archive --gzip` to a host file.
- `backup list` — backups newest first, with size and age.
- `backup restore <archive> --yes` — restore into `weather-mongodb`.
- `backup overview` — describe this noun.

The bare `climate-cli backup` prints the overview.

## Backup directory

`--dir`, else `$CLIMATE_WEATHER_BACKUP_DIR`, else
`$XDG_DATA_HOME/climate-cli/backups` (`~/.local/share/...`). A directory
inside the repo checkout is refused (exit `1`) — a backup belongs outside
docker's volume store *and* outside git.

## Exit codes

`0` success, `1` user-input error (missing `--yes`, non-empty database
without `--force`, a directory inside the checkout, a missing archive),
`2` docker/compose/daemon problems. Every verb supports `--json`.
"""

_BACKUP_DUMP = """\
# climate-cli backup dump

Runs `mongodump --db weather --archive --gzip` inside `weather-mongodb` and
writes the archive atomically to the host as
`weather-<UTC timestamp>.archive.gz`, reporting its path, size and SHA-256.

The only copy of the collection otherwise lives in one docker named volume,
which a stray docker volume-pruning command would destroy — take dumps.
`climate-cli doctor` warns when the newest dump gets old.

## Usage

    climate-cli backup dump
    climate-cli backup dump --dir /srv/backups --json
"""

_BACKUP_LIST = """\
# climate-cli backup list

Lists `weather-*.archive.gz` files in the backup directory, newest first,
with size and age in seconds. Opens no docker connection at all.

## Usage

    climate-cli backup list
    climate-cli backup list --dir /srv/backups --json
"""

_BACKUP_RESTORE = """\
# climate-cli backup restore

Restores a dump into `weather-mongodb` via `mongorestore --archive --gzip
--nsInclude weather.*`.

## Usage

    climate-cli backup restore /srv/backups/weather-20260917T000000Z.archive.gz --yes
    climate-cli backup restore <archive> --yes --force --json

## Safety

- `--yes` is **required** — without it the verb exits `1`.
- A non-empty `weather` database is refused (exit `1`) unless `--force` is
  also given; `--force` is what adds `mongorestore --drop`.
- A restore into an empty database reproduces the source's fetch-record count
  and content hashes exactly.
"""


# An `overview` verb documents its own noun, so it maps to the same body under
# its own key (the explain catalog is path-keyed, not alias-resolving).
ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("climate-cli",): _ROOT,
    ("climate",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("stack",): _STACK,
    ("stack", "up"): _STACK_UP,
    ("stack", "down"): _STACK_DOWN,
    ("stack", "status"): _STACK_STATUS,
    ("stack", "overview"): _STACK,
    ("weather",): _WEATHER,
    ("weather", "latest"): _WEATHER_LATEST,
    ("weather", "series"): _WEATHER_SERIES,
    ("weather", "forecast"): _WEATHER_FORECAST,
    ("weather", "stats"): _WEATHER_STATS,
    ("weather", "overview"): _WEATHER,
    ("providers",): _PROVIDERS,
    ("providers", "overview"): _PROVIDERS,
    ("backup",): _BACKUP,
    ("backup", "dump"): _BACKUP_DUMP,
    ("backup", "list"): _BACKUP_LIST,
    ("backup", "restore"): _BACKUP_RESTORE,
    ("backup", "overview"): _BACKUP,
}
