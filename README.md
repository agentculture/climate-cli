# climate-cli

A multi-provider weather tracker and its agent-first CLI.

A small docker stack — MongoDB, a polling tracker and a read-only web service —
samples several free weather providers on **their own** refresh policies, stores
every response **verbatim** (body bytes, status, cache headers, content hash),
derives normalized readings from those stored bytes, and serves the collection
over HTTP with a chart-first dashboard. `climate` runs that stack and queries
what it collected.

The first-class question is **the latest reading**: the intended consumer is an
agent controlling hardware (home AC) from measured conditions, so every value
reports its own age and its provider, and there is a dedicated exit code for
"this data is too old to act on".

Design notes worth knowing up front:

- **No provider field is ever dropped.** Anything without a row in the shared
  vocabulary is still stored, as `x_<provider_field_name>`.
- **Raw before parsed.** The verbatim response is persisted before any parsing
  runs, so a parser bug can never lose data; normalized readings are
  re-derivable from the stored bytes.
- **Failures are data.** Timeouts, 4xx/5xx and 429s are stored as fetch records
  too, so a gap in the data is distinguishable from a gap in collection.
- **No backfill, ever.** A missed tick (stack down, host off, network out) stays
  an honest gap. Nothing is interpolated or invented.

## Quickstart

```bash
uv sync

# 1. Compose secrets — gitignored; the only place provider keys ever live.
cp docker/weather.env.example docker/weather.env
$EDITOR docker/weather.env

# 2. Your locations — private data, OUTSIDE the repo.
mkdir -p ~/.config/climate-cli
$EDITOR ~/.config/climate-cli/weather.json

# 3. Run it.
uv run climate stack up
uv run climate stack status
uv run climate weather latest
uv run climate doctor
```

The dashboard is then at <http://127.0.0.1:8095>. Take a backup with
`uv run climate backup dump`.

### The location config

`~/.config/climate-cli/weather.json` (or `$XDG_CONFIG_HOME/climate-cli/`,
or wherever `CLIMATE_WEATHER_CONFIG_PATH` points) holds labelled locations and
per-provider settings. **It never belongs in this repository** — not in the
package, not in tests, not in fixtures, not in the public eidetic memory store.
The values below are placeholders; put your own in:

```json
{
  "coordinate_precision": 2,
  "locations": {
    "home": {"latitude": 0.0, "longitude": 0.0}
  },
  "providers": {
    "open-meteo": {"enabled": true},
    "met-no": {"enabled": true},
    "openweather": {"enabled": true, "interval_seconds": 600},
    "metar": {"enabled": true, "request_params": {"stations": ["XXXX"]}},
    "ims": {"enabled": false},
    "ims-forecast": {"enabled": false, "request_params": {"cities": ["Your City"]}}
  }
}
```

There is no default location anywhere in the package: with nothing configured,
the tracker refuses to start (exit 2) rather than silently watching some place
it picked for you.

## CLI

| Command | What it does |
|---------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Identity invariants **plus** weather-tracker environment checks. |
| `cli overview` | Describe the CLI surface itself. |
| `stack up` / `down` / `status` / `overview` | Run the `climate-weather` compose project. |
| `weather latest` | Newest reading per (provider, location). The primary query. |
| `weather series` | One variable over time. |
| `weather forecast` | Stored forecasts. |
| `weather stats` | Due-versus-stored fetch counts per provider. |
| `weather overview` | Describe the weather noun. |
| `providers [--limits]` | Adapters: capabilities, auth, quota, freshness, attribution. |
| `backup dump` / `list` / `restore` / `overview` | Local database backups. |

Markdown is the default rendering — it is what humans and agents both read.
Every command supports `--json` for code. Bare `weather --json` returns
overview JSON describing the noun's query verbs, not API data; only the
query sub-verbs — `weather latest --json`, `weather series --json`,
`weather forecast --json`, `weather stats --json` — return the HTTP API's
payload structure unchanged. Results go to stdout, errors and progress
diagnostics to stderr, never mixed.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success, including "no data yet". |
| `1` | User-input error: a bad flag value, a bad path, a missing `--yes`. |
| `2` | Environment error: docker/compose/daemon missing, no compose file, web service unreachable. |
| `3` | **Stale data** — `weather latest --max-age` exceeded. |
| `4+` | Reserved. |

Exit `3` exists because a stopped tracker still returns a well-formed `latest`.
An agent that acts on measured conditions should ask for it:

```bash
climate weather latest --json --max-age 10m || echo "too old to act on"
```

## Providers

Six adapters ship. `climate providers --limits` prints this table live from the
adapters themselves; the notes below add the licence and freshness context.

Quota figures marked **unverified** were taken from issue 5 or third-party
pages, not from the provider's own documentation — they are configurable
metadata to re-check, not facts.

### open-meteo

- **Auth:** none (keyless).
- **Licence / attribution:** "Weather data by Open-Meteo.com", CC BY 4.0 —
  <https://open-meteo.com/en/license>. Free tier is non-commercial.
- **Freshness:** `interval`. Default interval **900 s** — the provider's
  `current` block is a 900-second model slice, so polling faster returns the
  same slice.
- **Rate limits:** 600 calls/min, 10,000 calls/day, 300,000 calls/month, with a
  variable-count call weight. **Unverified** (source: issue 5).
- **Notes:** one request returns current, `minutely_15`, hourly and daily
  blocks; the forecast horizon defaults to 48 h. The only keyless source of
  radiation *model* data here.

### met-no

- **Auth:** none, but a mandatory identifying `User-Agent` is sent.
- **Licence / attribution:** MET Norway, CC BY 4.0 / NLOD —
  <https://api.met.no/doc/License>.
- **Freshness:** `http_expires`. Default interval **1800 s**, but the interval
  is a floor, not the policy: no request is issued before the previous
  response's `Expires`, and the post-expiry request carries
  `If-Modified-Since`. A `304` is stored as a fetch record like any other.
- **Rate limits:** no published numeric quota
  (<https://api.met.no/doc/TermsOfService>). The honest obligation is to honour
  `Expires` and send conditional GETs, not to spend a budget. **Verified** as
  "no published figure" — which is not the same as unlimited.
- **Notes:** Locationforecast 2.0 complete; no shortwave-radiation field.
  Coordinates are capped at 4 decimals by MET Norway's terms — this service
  never sends more than 2 by default anyway.

### openweather

- **Auth:** **required** — `CLIMATE_OPENWEATHER_API_KEY`
  (<https://openweathermap.org/appid>). No key means the provider is reported
  disabled, with the variable named; nothing else breaks.
- **Licence / attribution:** "Weather data provided by OpenWeather".
- **Freshness:** `interval`. Default interval **600 s** (~10 min upstream
  update cadence).
- **Rate limits:** 60 calls/min, 1,000,000 calls/month. **Unverified**
  (source: issue 5).
- **Notes:** the free `/data/2.5/weather` current-conditions call only — no One
  Call 3.0 (it needs a card on file). Its payload carries no dew point, no
  solar radiation and no UV.

### ims

- **Auth:** **required** — `CLIMATE_IMS_API_TOKEN`, granted manually by email
  to `ims@ims.gov.il` with no stated turnaround.
- **Licence / attribution:** Israel Meteorological Service —
  <https://ims.gov.il/en/termOfuse>.
- **Freshness:** `station_cadence`. Default interval **600 s**.
- **Rate limits:** none published, and none discoverable without a token. The
  adapter declares a conservative 1,000 calls/day placeholder, explicitly
  **unverified** (`source: "unverified"`).
- **Notes:** the only source here of *measured* solar radiation, humidity and
  rain. Station and channel ids are discovered at runtime from `GET /stations`,
  never hard-coded — they differ per station. Observation timestamps are
  corrected from the API's documented always-UTC+2 quirk (its `+03:00` label is
  wrong in summer) to true UTC. Ships disabled; enable it when a token arrives.
  **Not verified live** — the adapter is built against the vendor PDF and
  synthesized fixtures.

### metar

- **Auth:** none (keyless).
- **Licence / attribution:** aviationweather.gov (NOAA/FAA), U.S. Government
  Work, public domain — <https://aviationweather.gov/data/api/>.
- **Freshness:** `station_cadence`. Default interval **1800 s** (reports are
  roughly hourly).
- **Rate limits:** none published for the keyless METAR endpoint. **Verified**
  as "no documented cap" from the Data API docs.
- **Notes:** genuine airport *observations*, not model output. Off unless you
  set `enabled: true` and list ICAO codes in `request_params.stations` — there
  is no default station. Station coordinates in the feed are deliberately never
  stored as reading values.

### ims-forecast

- **Auth:** none (keyless).
- **Licence / attribution:** Israel Meteorological Service —
  <https://ims.gov.il/en/termOfuse>.
- **Freshness:** `interval`. Default interval **21600 s** (6 h); the feed is
  issued a few times a day.
- **Rate limits:** none published for the keyless feed.
- **Notes:** a daily *city forecast* XML feed (ISO-8859-8), not observations.
  Off unless you set `enabled: true` and give an ordered `request_params.cities`
  candidate list — the feed lists only about fifteen cities, so name yours
  first and a larger nearby one as a fallback.

## Privacy

Your location is private data and this repository treats it as such.

- **Coordinates are rounded** before they are sent or stored — 2 decimals
  (about 1 km) by default, never more than the 4 decimals MET Norway allows.
  Weather does not vary at address precision.
- **The API speaks in labels.** Every HTTP response and every dashboard asset
  identifies a place by the label you chose (`home`, `office`), never by a
  coordinate. Stored fetch records carry the label, and request URLs in them
  carry only the rounded values.
- **Your config never enters the repo.** Locations live in
  `~/.config/climate-cli/weather.json`, bind-mounted read-only into the
  containers. A repo-wide test (`tests/test_repo_hygiene.py`) fails the build on
  any coordinate literal in `climate/`, `tests/` or the docker files.
- **Keys live in one gitignored file.** `docker/weather.env` (from
  `docker/weather.env.example`) and the environment only — never in the config
  file, never in a log line, never in a stored request URL, never in the public
  `.eidetic/memory` store. Redaction is enforced at both the transport and the
  storage layer.

## Exposing the web service

By default `weather-web` is published on **loopback only**
(`127.0.0.1:8095`) and `weather-mongodb` publishes no host port at all.

To reach the dashboard from another machine, set the bind address in
`docker/weather.env`:

```bash
CLIMATE_WEB_BIND=0.0.0.0
CLIMATE_WEB_PORT=8095
```

> **Warning:** the web service has **no authentication of any kind.** Anyone who
> can reach the port can read everything you have collected. Put it behind a
> reverse proxy with auth, or restrict it to a trusted network — do not put it
> on the public internet. Note also that re-serving third-party weather data
> beyond personal use may carry licence conditions beyond attribution; the
> provider terms above were read in summary only. Re-check before exposing the
> service outside your LAN.

The service is read-only over the collection: it has no write route at all.

## Debugging MongoDB directly

`weather-mongodb` publishes no host port by default. For local inspection
with a GUI mongo client, apply the debug override on top of the base
compose file — this only adds a port publish to the existing
`weather-mongodb` service, never a second mongod process against the same
volume:

```bash
docker compose -f docker-compose.yml -f docker-compose.debug.yml up -d weather-mongodb
```

That reaches it at `127.0.0.1:27020`. Bring it back down the same way you
brought up the rest of the stack; there is no separate teardown step.

## Development

```bash
uv sync
uv run pytest -n auto                     # the full suite: no network, no docker, no Mongo
uv run pytest -n auto --cov=climate       # coverage (gate: 60%)
uv run black --check climate tests
uv run isort --check-only climate tests
uv run flake8 climate tests
uv run bandit -c pyproject.toml -r climate
uv run teken cli doctor . --strict        # the agent-first rubric gate CI runs
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
```

The runtime package has **no third-party dependencies** (`dependencies = []`).
`pymongo` lives in the `weather` extra, which only the container image installs
(`pip install ".[weather]"`); the host CLI reaches the collection through the
web service's HTTP API and never imports it. `tests/test_stdlib_only.py` proves
this by importing every host-side module in a fresh interpreter with all
third-party imports blocked.

Every PR bumps the version and adds a `CHANGELOG.md` entry — CI's version-check
job fails otherwise. See [`CLAUDE.md`](CLAUDE.md) for the full conventions.

## Follow-ups

- **A published container image** so `climate stack up` works from a PyPI
  install rather than a repo checkout:
  [issue #6](https://github.com/agentculture/climate-cli/issues/6). Today the
  stack verbs exit `2` with that hint from a wheel install.
- **S3 archive** of the collection — deferred; the stored shape does not
  preclude it.
- **Compaction** of stored-as-is data — revisit once months of data exist.
- **`ecmwf-open` and `noaa-gfs`** direct-model adapters from issue 5.

## License

MIT — see [`LICENSE`](LICENSE).
