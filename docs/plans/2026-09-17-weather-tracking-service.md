# Build Plan — weather tracking service

slug: `weather-tracking-service` · status: `exported` · from frame: `weather-tracking-service`

> climate-cli tracks Tel-Aviv-area weather every 5 minutes from several free providers, keeps every response in full (current state, forecast, all stats) in a local weather-mongodb docker instance, and the climate CLI controls the tracking service and queries what it collected

## Tasks

### t1 — Test harness guards and captured provider fixtures

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns tests/conftest.py, tests/fixtures/\*\*, tests/`test_repo_hygiene.py`. Keyless providers (open-meteo, met-no, aviationweather METAR, IMS XML) may be captured live once with curl for a neutral point such as the Greenwich Observatory; met-no needs an identifying User-Agent. OpenWeather and the IMS Envista API have no credentials here, so build those fixtures from the documented response shapes and say so in the fixtures README. Request open-meteo with a 48 h horizon.
- covers: c12, h9, h18
- acceptance:
  - tests/conftest.py blocks real socket connections for the whole suite; a test that tries to connect fails with a clear message
  - tests/fixtures/ holds one verbatim response file per provider (open-meteo, met-no with its response headers, openweather, ims stations + latest, METAR LLBG, IMS city-forecast XML as ISO-8859-8 bytes) plus a fixtures README stating how each was obtained
  - fixtures for coordinate-based providers use a neutral public reference point unrelated to the user; a test scans climate/, tests/ and the docker files and fails on any latitude/longitude literal outside tests/fixtures/

### t2 — User configuration: locations, per-provider settings, coordinate rounding

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/`__init__.py`, climate/weather/config.py, tests/weather/`test_config.py`. Use JSON for the config file (stdlib can read and write it). Secrets are NOT in this file - they come from environment variables only.
- covers: c27, c8, h6, c44
- acceptance:
  - climate/weather/config.py loads configuration from an XDG config path outside the repo (overridable by an env var) and returns typed settings: labelled locations, per-provider enabled flag, interval, request parameters and quota
  - with no location configured, loading for the tracker raises an environment error (exit 2) with a remediation hint; no default place exists anywhere in the package
  - coordinates are rounded to a configurable precision, default 2 decimals and never more than 4, before any other module sees them
  - a configuration whose locations multiplied by a provider's interval exceed that provider's quota is rejected with a message naming the provider

### t3 — Storage model, store interface and in-memory fake

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/store.py, tests/weather/`test_store.py`. The interface must make it impossible to save a reading before its fetch record exists (`save_fetch` returns the id that `save_readings` requires).
- covers: c2, h2, c40, h28, c22, h1
- acceptance:
  - climate/weather/store.py defines a fetch record (exact response bytes, content-type, charset, provider, endpoint with secrets redacted, location label, `requested_at` UTC, HTTP status, cache headers, sha256 of the bytes, error detail, `schema_version`) and a normalized reading (values with units and original units, provider/source/model, `observed_at`, `requested_at`, kind of observation|model|forecast, reference to its fetch record)
  - the stored bytes of a fixture response hash to the same digest as the fixture file, including the ISO-8859-8 XML
  - a failed fetch (timeout, 4xx/5xx, 429, 304) is representable and stored as a fetch record with no body loss and no reading
  - an in-memory store implements the same interface as the Mongo store and is what every other test uses

### t4 — HTTP fetch helper with timeouts and secret redaction

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/http.py, tests/weather/`test_http.py`. urllib only. Tests inject a fake opener; no sockets. Restrict schemes to https to keep bandit B310 quiet.
- covers: c14, h11
- acceptance:
  - climate/weather/http.py performs a GET with a per-call timeout, custom headers and optional If-Modified-Since, returning status, headers and the exact body bytes; network errors become a result object, never an uncaught exception
  - a redaction function removes API keys and tokens from URLs (query string), headers and exception text; a test proves an OpenWeather-style appid value never appears in the redacted URL, the result object repr, or a log line

### t5 — Provider interface: capabilities, quota, refresh policy, attribution

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/providers/`__init__.py`, climate/weather/providers/base.py, tests/weather/providers/`test_base.py`. Adapters are added by other tasks as separate modules; the registry discovers them by an explicit list that those tasks do not need to edit - use pkgutil discovery over the providers package.
- covers: c46, c21
- acceptance:
  - climate/weather/providers/base.py defines the provider contract: id, capabilities, auth requirement, default interval, quota metadata, freshness strategy (interval | http-expires | station-cadence), licence/attribution text and URL, `build_requests`(location, settings), `is_due`(now, `last_fetch`), normalize(`fetch_record`)
  - a registry lists providers without importing any third-party module, so the host CLI can read provider metadata
  - a test fails if a registered provider lacks attribution text, quota metadata or a freshness strategy

### t6 — HTTP API contract document

- instruction: Owns docs/weather-api.md only. This is the contract the web API task, the dashboard task and the CLI query task all build against in parallel, so be exact about field names and types. Must pass markdownlint.
- covers: c26, c28
- acceptance:
  - docs/weather-api.md specifies every read-only route with request parameters and example JSON: latest (per provider and location, each value with unit, `observed_at`, `age_seconds`, provenance, stale flag), series (explicit nulls for gaps, kind tag), forecasts, providers, stats (due vs stored fetch counts per provider over a window), health (tracker version, newest fetch age)
  - the document states that there are no write routes, that locations are identified by label only, and that coordinates never appear in any response

### t7 — AgentCulture design tokens extracted at a pinned org commit

- instruction: Owns climate/weather/web/static/tokens.css, docs/adr/0001-culture-design-source.md, scripts/check-culture-design.py. Read ../org and ../culture-nodes only; never write there. Use git -C ../org show PIN:path to read the pinned file. Copy the categorical palette values culture-nodes uses into the ADR for the dashboard task to evaluate.
- covers: c31, h21
- acceptance:
  - climate/weather/web/static/tokens.css is a verbatim copy of org's site-astro/src/styles/global.css below a header comment naming the pinned commit
  - docs/adr/0001-culture-design-source.md records the pin, what was extracted and the re-pin procedure, following culture-nodes ADR 0001
  - a check script verifies byte-equality against the org checkout at the pinned commit when that checkout is present and skips cleanly when it is not; git status in ../org and ../culture-nodes is unchanged

### t8 — Container image and compose project

- instruction: Owns Dockerfile, docker-compose.yml, docker/weather.env.example, .dockerignore, tests/weather/`test_compose.py`. Base image python:3.12-slim (multi-arch; this host is aarch64). The image installs the package with its extra. Service entrypoints are 'python -m climate.weather.tracker' and 'python -m climate.weather.web' - other tasks create those modules. Compose project name: climate-weather.
- covers: c5, h4, c15, h12, c43
- acceptance:
  - docker-compose.yml defines weather-mongodb (mongo:8.0, named volume weather-mongodb-data, restart unless-stopped, mongosh ping healthcheck, NO published port by default), a tracker service and a web service built from one Dockerfile, each with restart unless-stopped and json-file log rotation (max-size and max-file)
  - the web service publishes on 127.0.0.1 by default with the bind address and port taken from variables; an optional mongo debug publish on host port 27020 is off by default and configurable
  - secrets and the config path come from a gitignored env file; docker/weather.env.example documents every variable with placeholder values only
  - a test parses the compose file and asserts: no host port on mongo by default, log limits on every service, the named volume, and that no command in the repo removes that volume

### t9 — MongoDB store, connection choke-point and tracker lease

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/mongo.py, tests/weather/`test_mongo.py`. Model the guard on jetson-ai-lab-cli/jlab/mongo.py (read it, do not import it). Store response bodies as BSON binary, never as parsed documents. Tests use a hand-written fake collection, not a live server and not mongomock.
- depends on: t3
- covers: c17, h13, c41
- acceptance:
  - climate/weather/mongo.py exposes one connection function reading `WEATHER_MONGO_URI` (default mongodb://weather-mongodb:27017/weather); it refuses loopback or host addresses on ports 27017, 27018 and 27019 and mongodb+srv URIs, with exit-2 errors
  - pymongo is imported only inside functions; importing the module without pymongo installed succeeds, and calling it raises an environment error with the exact install command, no ImportError traceback
  - the Mongo store passes the same interface test-suite as the in-memory store, run against a fake client; indexes exist for (provider, location, `requested_at` desc)
  - `acquire_lease`/renew/release implement a single-holder lease document with expiry; a second holder cannot acquire a live lease

### t10 — open-meteo adapter

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Use the verbatim fixture from tests/fixtures/, the HTTP helper and the provider contract; the adapter never touches the network in tests. normalize() must keep provider, source and model distinct and set `observed_at` from the provider's own timestamp, never from `requested_at`. Owns climate/weather/providers/`open_meteo.py`, tests/weather/providers/`test_open_meteo.py`.
- depends on: t1, t3, t4, t5
- covers: c3, h3
- acceptance:
  - builds one request per location asking for current, `minutely_15`, hourly and daily blocks with a 48-hour horizon and the full AC-relevant variable list; horizon and variables are overridable from provider settings
  - normalize() yields a current reading tagged kind=model with interval 900 s provenance, plus forecast readings; the forecast block is stored and normalized even though nothing consumes it yet
  - quota metadata reflects the call-weight rule (variables x models / 10, minimum 1) so the budget check counts a wide request as several calls

### t11 — met-no adapter with Expires-driven refresh

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Use the verbatim fixture from tests/fixtures/, the HTTP helper and the provider contract; the adapter never touches the network in tests. normalize() must keep provider, source and model distinct and set `observed_at` from the provider's own timestamp, never from `requested_at`. Owns climate/weather/providers/`met_no.py`, tests/weather/providers/`test_met_no.py`. The fixture includes the recorded response headers; drive `is_due` from them.
- depends on: t1, t3, t4, t5
- covers: h15
- acceptance:
  - `is_due`() returns false before the stored Expires of the last successful fetch and true after it; the post-expiry request carries If-Modified-Since and an identifying User-Agent built from the package version and repo URL
  - a 304 response is returned as a fetch record with no readings; coordinates are sent with at most 4 decimals
  - normalize() treats the first timeseries entry as the current model value (kind=model, `observed_at` = that entry's UTC time) and the rest as forecast

### t12 — openweather adapter (current weather only)

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Use the verbatim fixture from tests/fixtures/, the HTTP helper and the provider contract; the adapter never touches the network in tests. normalize() must keep provider, source and model distinct and set `observed_at` from the provider's own timestamp, never from `requested_at`. Owns climate/weather/providers/openweather.py, tests/weather/providers/`test_openweather.py`.
- depends on: t1, t3, t4, t5
- covers: h11
- acceptance:
  - issues exactly one call per location to the 2.5 current-weather endpoint; there is no forecast or One Call code path
  - the API key is read from `CLIMATE_OPENWEATHER_API_KEY`; without it the provider reports itself disabled with a reason instead of failing the tick
  - the endpoint stored in the fetch record and every log line carry the key redacted

### t13 — ims adapter (Envista station observations)

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Use the verbatim fixture from tests/fixtures/, the HTTP helper and the provider contract; the adapter never touches the network in tests. normalize() must keep provider, source and model distinct and set `observed_at` from the provider's own timestamp, never from `requested_at`. Owns climate/weather/providers/ims.py, tests/weather/providers/`test_ims.py`. Built against the documented API shape only - no token exists yet, so live verification is a recorded plan risk, not part of this task.
- depends on: t1, t3, t4, t5
- covers: h16
- acceptance:
  - station and channel ids are discovered from the stations endpoint and cached, never hard-coded; stations are chosen per location from provider settings (explicit ids) or nearest-by-distance
  - `observed_at` is converted from fixed UTC+2 to UTC regardless of the +03:00 label in the payload, proven by a test with a summer-date fixture
  - the token is read from `CLIMATE_IMS_API_TOKEN`; without it the provider reports itself disabled with a reason; readings are kind=observation and keep measured radiation channels (Grad, DiffR, NIP)

### t14 — Keyless extras: METAR and IMS city-forecast XML adapters

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Use the verbatim fixture from tests/fixtures/, the HTTP helper and the provider contract; the adapter never touches the network in tests. normalize() must keep provider, source and model distinct and set `observed_at` from the provider's own timestamp, never from `requested_at`. Owns climate/weather/providers/metar.py, climate/weather/providers/`ims_forecast.py` and their tests.
- depends on: t1, t3, t4, t5
- covers: c8
- acceptance:
  - a metar adapter fetches configured ICAO stations from aviationweather.gov as kind=observation with roughly hourly cadence
  - an ims-forecast adapter stores the ISO-8859-8 city-forecast XML byte-exact and normalizes the configured city's daily forecast as kind=forecast
  - both are disabled unless enabled in provider settings

### t15 — Read-only web API service

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/web/`__init__.py`, climate/weather/web/`__main__.py`, climate/weather/web/api.py, tests/weather/web/`test_api.py`. Use the standard library http.server (ThreadingHTTPServer) - a read-only JSON API plus static files needs no framework, which keeps the image's only third-party dependency pymongo. Tests call the handler functions with the in-memory store.
- depends on: t3, t6
- covers: c26, h17, h19, h20, h32, c36, h25
- acceptance:
  - climate/weather/web/ serves exactly the routes in docs/weather-api.md plus the static dashboard directory; any non-GET method returns 405
  - latest reports unit, `observed_at`, `age_seconds`, provenance and a stale flag per value; series returns explicit nulls for missed ticks and a kind tag; stats computes due-versus-stored fetch counts from fetch records
  - no response body contains a latitude or longitude - locations appear by label only - proven by a test over every route
  - the bind address defaults to loopback and changes only through an explicit setting

### t16 — Host CLI: stack noun (up, down, status, overview)

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/cli/`_commands`/stack.py, tests/`test_cli_stack.py`. Model on data-refinery-cli's `data_refinery`/cli/`_commands`/stack.py (read it, do not import it): find the compose file by walking up from the module; inject the subprocess runner so tests never call docker. Test through a local argparse parser built in the test, since the integration task does the real registration.
- depends on: t8
- covers: c6, h5, h4
- acceptance:
  - climate/cli/`_commands`/stack.py exposes register(sub) with up, down, status and overview verbs, each with --json, wrapping docker compose for the climate-weather project only
  - down never passes a volume-removing flag; no verb in the module can remove weather-mongodb-data
  - missing docker, missing compose plugin, unreachable daemon or no compose file found (wheel install) each raise CliError exit 2 with a specific remediation hint

### t17 — Host CLI: weather query verbs over the HTTP API

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/cli/`_commands`/weather.py, tests/`test_cli_weather.py`. Calls the API with urllib and a timeout; base URL from `CLIMATE_WEATHER_URL`, default <http://127.0.0.1> plus the default web port. Inject the fetch function in tests.
- depends on: t6
- covers: c28, h19, c42, h30, c33, h22
- acceptance:
  - climate/cli/`_commands`/weather.py exposes register(sub) with overview, latest, series, forecast and stats verbs; markdown by default, identical structure under --json
  - latest accepts --provider, --location and --max-age; when the freshest reading is older than --max-age it exits with a dedicated stale exit code (3) in both modes and the JSON carries stale: true
  - an unreachable web service exits 2 with a hint to run 'climate stack status', distinct from the stale code; with no data the verb says so and exits 0
  - the module imports only the standard library

### t18 — Host CLI: providers verb

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/cli/`_commands`/providers.py, tests/`test_cli_providers.py`. Reads the provider registry only - no network, no MongoDB, standard library only.
- depends on: t5
- covers: c46, h34
- acceptance:
  - climate/cli/`_commands`/providers.py exposes register(sub); 'providers' lists each provider's capabilities, auth requirement, enabled state and reason, freshness strategy and attribution; --limits adds the quota columns; --json supported
  - a test fails when any registered adapter lacks a row or an attribution line

### t19 — Scheduler: per-provider due logic, isolation, no overlap, jitter

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/scheduler.py, tests/weather/`test_scheduler.py`. Inject clock, sleep, random, store and providers so tests run instantly with fakes. All providers are sampled on every due tick - there is no rotation. Every fetch made is stored as-is even when its bytes are unchanged - no dedup.
- depends on: t2, t3, t5
- covers: c21, c41, h29, h1, h24
- acceptance:
  - on each base tick every enabled provider is fetched only when its own configured interval has elapsed AND its `is_due`() allows it; one provider's settings never alter another's schedule
  - one provider raising, timing out or returning 429 still leaves every other provider fetched and stored in the same tick, and the failure is stored as a fetch record
  - a tick that overruns is never overlapped by the next; tick start carries a small random jitter; missed ticks are not backfilled
  - without the lease the scheduler issues zero provider requests

### t20 — Re-derivation of normalized readings from raw fetch records

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/rederive.py, tests/weather/`test_rederive.py`. Works against the store interface only.
- depends on: t10, t11, t12, t13, t14
- covers: c37, h26, c22
- acceptance:
  - climate/weather/rederive.py rebuilds normalized readings for a range of stored fetch records using each provider's normalize(), and a test proves the rebuilt readings equal the originals for every provider fixture
  - 100% of readings produced by any adapter reference an existing fetch record; a test fails otherwise

### t21 — Tracker service entrypoint

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/weather/tracker.py, tests/weather/`test_tracker.py`. Thin wiring only - logic belongs to config, scheduler, mongo and the adapters. A provider missing its key is disabled with a logged reason; it does not stop the tracker.
- depends on: t9, t19, t10, t11, t12, t13, t14
- covers: c35, h6, c45
- acceptance:
  - 'python -m climate.weather.tracker' loads configuration, validates secrets eagerly, acquires the lease and runs the scheduler in the foreground, logging one line per fetch with secrets redacted
  - with no location configured, or when another tracker holds the lease, it exits 2 with a remediation hint and issues no provider request
  - it writes a heartbeat document carrying the package version on start and on every tick, and shuts down cleanly on SIGTERM releasing the lease

### t22 — Chart-first dashboard in the AgentCulture design

- instruction: Owns climate/weather/web/static/\*\* except tokens.css, and docs/design/. Load the frontend-design skill and the dataviz skill BEFORE writing any UI or chart code and follow them. Visual identity comes from tokens.css (org's 'First light over the mesh') and culture-nodes' web app as reference - read ../org and ../culture-nodes, never write there. Chart series colours: use the AgentCulture categorical palette only where it passes the dataviz colour validator in both themes, otherwise the dataviz-validated palette. Local time is a display concern only; the API is UTC. No Node toolchain, no CDN dependency that the page cannot work without.
- depends on: t7, t15
- covers: c30, h20, c47, h35
- acceptance:
  - climate/weather/web/static/ holds a build-free dashboard (index.html, ES modules, inline SVG charts, tokens.css) that reads only the documented API routes; it is charts and stat tiles with little to no prose
  - observations, model values and forecasts are visually distinct; gaps in a series render as gaps, never as interpolated lines; the first-hour and provider-disabled states are explicit empty or partial states
  - a compact footer credits every provider whose data is on screen, linking its licence or terms
  - verified with the playwright MCP against the API serving fixture-derived data: light theme, dark theme and phone width, no console errors, screenshots saved under docs/design/

### t23 — Host CLI: backup verb (dump and restore)

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. Owns climate/cli/`_commands`/backup.py, tests/`test_cli_backup.py`. Inject the subprocess runner; tests never call docker. Because mongo publishes no host port, everything goes through 'docker compose exec'.
- depends on: t16
- covers: c39, h27
- acceptance:
  - climate/cli/`_commands`/backup.py exposes register(sub) with 'dump' (runs mongodump --archive --gzip inside the weather-mongodb container via docker compose exec and writes the archive to a host directory outside docker's volume store), 'list' and 'restore' (requires an explicit confirmation flag and refuses a non-empty database unless forced)
  - the dump directory is configurable, defaults under the XDG data home, and is never inside the repo; all verbs support --json and raise CliError exit 2 on docker problems

### t24 — doctor: tracker environment checks

- instruction: Layout rule for the whole plan: service code lives under climate/weather/, host CLI verbs under climate/cli/`_commands`/. Do NOT edit climate/cli/`__init__.py`, climate/explain/catalog.py, pyproject.toml, README.md or CHANGELOG.md - the integration task owns them. Standard library only unless this task says otherwise; pymongo is imported lazily inside functions. Write the tests first. This task is the one allowed to edit climate/cli/`_commands`/doctor.py; also owns tests/`test_cli_doctor_weather.py`. Size, freshness and version figures come from the web API health and stats routes, not estimates. Standard library only.
- depends on: t16, t17, t21
- covers: c13, h10, c45, h33, c43, h31
- acceptance:
  - climate/cli/`_commands`/doctor.py gains checks in the existing {id, passed, severity, message, remediation} shape: docker available, stack running, web API reachable, newest fetch age per provider, provider credentials present, database size and host disk headroom, newest backup age, host-CLI versus tracker image version skew naming both versions
  - the checks run from a wheel install with no culture.yaml, and a stopped stack yields failed checks with remediation rather than an exception; existing identity checks and their tests are unchanged

### t25 — Integration: register nouns, explain catalog, packaging, docs, CI config

- instruction: The only task that edits climate/cli/`__init__.py`, climate/explain/catalog.py, climate/cli/`_commands`/overview.py, climate/cli/`_commands`/learn.py, pyproject.toml, uv.lock, README.md, CLAUDE.md, CHANGELOG.md, sonar-project.properties. Use the version-bump skill (minor). Bandit will flag any 0.0.0.0 bind (B104) and urllib (B310): fix by design (loopback default, https-only), and annotate only what is genuinely intended.
- depends on: t16, t17, t18, t22, t23, t21, t24
- covers: c9, h7, c10, h8, c18, h14
- acceptance:
  - climate/cli/`__init__.py` registers the stack, weather, providers and backup nouns; every new verb path resolves under 'climate explain'; 'uv run teken cli doctor . --strict' passes
  - pyproject.toml keeps dependencies = \[\] and adds a \[project.optional-dependencies\] extra holding pymongo; a test imports every host-side command module with third-party imports blocked
  - README.md, the root explain entry and the parser description describe the weather tracker with no template boilerplate left; per-provider licence, freshness and rate-limit notes are documented; CLAUDE.md documents the stack workflow
  - sonar-project.properties excludes climate/weather/web/static/\*\* from coverage only; the version is bumped with a CHANGELOG entry; black, isort, flake8, bandit and markdownlint pass and coverage stays at or above 60%

### t26 — Live validation on this host

- instruction: Agent-side validation, not CI: run on this aarch64 host with a throwaway location in a scratch config outside the repo. Needs the user's go-ahead before 'stack up', because it starts real containers and makes real provider requests. File results with devague evidence; do not claim the 24-hour or reboot criteria unless actually observed.
- depends on: t24, t21, t20, t25, t22
- covers: c1, c5, c36
- acceptance:
  - 'climate stack up' from the checkout brings up all three containers healthy; within two ticks 'climate weather latest' returns readings from every keyless provider in markdown and --json
  - a dump taken from the running stack restores into an empty scratch mongo container with the same fetch-record count and content hashes
  - stopping the web container makes 'latest' exit 2 while 'stats' later shows collection continued; starting a second tracker issues no provider request
  - 'climate weather stats' reports due-versus-stored counts from the database; the 24-hour 95% figure and the reboot-resume check are recorded as evidence when observed, or left explicitly open

## Deferred targets

- `c34` (before_state): climate-cli is a bare agent template with no weather capability; nothing records Tel-Aviv-area conditions, so the AC is controlled without measured humidity, radiation, wind or forecast context, and past conditions cannot be looked up — deferred: Before-state is a fact established by the scope survey (s8-s11: no sibling repo or container records weather); there is nothing to build. Deferred by the user on the agent's recommendation rather than faked with a task
- `h23` (honesty): Nothing else on this machine already records this data — confirmed by the scope survey finding no weather collection in any sibling repo — deferred: Honesty condition of the deferred before-state c34; already evidenced by the scope survey, nothing to build

## Risks

- [unknown_nonblocking] The ims adapter is built from documentation only: no token exists yet (requested by email 2026-09-17), so payload shape, station ids near the configured location, cadence and rate limits are unverified until it arrives (task t13)
- [unknown_nonblocking] The openweather fixture is synthesized from documented shapes because no API key was used during planning; the first live fetch may reveal normalization gaps (raw storage is unaffected) (task t12)
- [unknown_nonblocking] The 24-hour 95% success figure and the reboot-resume check (c36) cannot be observed inside a build wave; t26 records them as evidence only when actually observed, otherwise they stay open (task t26)
- [unknown_nonblocking] The spec and frame quote issue 5's public city-centre coordinates inside resolved question q5, so a literal repository-wide coordinate search (h18) would match docs/specs and .devague; t1's hygiene test therefore scans code, tests and docker files only. The user's own location is never in the repo (task t1)
- [unknown_nonblocking] Approved obligation o8 is drifted and superseded by o15 (query verbs need no extra after decision c49); devague cannot reject an approved obligation, so tasks build to o15 and validate-delivery should file evidence against o15, not o8 (task t25)
- [unknown_nonblocking] Wave 1 is eleven tasks wide and file-disjoint by design, but five adapters plus the scheduler all lean on the t3 store and t5 provider contracts; a contract change discovered mid-wave is a /deviate, not a silent edit
- [follow_up] Published multi-arch image so the stack runs from a PyPI install - tracked in issue 6
- [follow_up] S3 archive, stored-data compaction, and the ecmwf-open / noaa-gfs adapters
