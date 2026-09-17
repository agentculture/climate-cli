# weather tracking service

> climate-cli tracks Tel-Aviv-area weather every 5 minutes from several free providers, keeps every response in full (current state, forecast, all stats) in a local weather-mongodb docker instance, and the climate CLI controls the tracking service and queries what it collected

## Audience

- Three readers on one machine: the AC-control agent (reads markdown, acts on the latest reading), code and scripts (read --json), and Ori as the human operator (reads markdown in the terminal and the dashboard in a browser)

## Before → After

- Before: climate-cli is a bare agent template with no weather capability; nothing records Tel-Aviv-area conditions, so the AC is controlled without measured humidity, radiation, wind or forecast context, and past conditions cannot be looked up
- After: One CLI stack command brings up weather-mongodb, the tracker and the web service; from then on every provider is sampled on its own refresh policy every 5 minutes, every response is kept in full, 'latest reading' answers in markdown or --json, and the dashboard charts what has been collected

## Why it matters

- The collected weather data drives the user's home AC control — every parameter counts, so no provider field is discarded

## Requirements

- Every response is stored in full: all stats the provider returns (temperature, wind, precipitation, humidity, whatever is provided), and any forecast returned in the same request is kept too, even when only the 'current state' is used today
  - honesty: Forecast blocks are stored even though nothing reads them yet; the 48 h horizon is a request parameter, not a post-hoc trim of what the provider returned
- Data lands in a local 'weather-mongodb' docker MongoDB instance, following the same convention used for the other services' mongodb containers on this machine
  - honesty: Collected data survives 'stack down' and image rebuilds: it lives in the named volume and no CLI verb ever removes that volume without an explicit destructive flag
- The climate CLI controls the tracking service (lifecycle) and provides a way to query the collected data
  - honesty: Every stack/tracker verb works from the host CLI against the containers, and exits 2 with a remediation hint when docker or the compose plugin is missing or the daemon is unreachable
- New noun groups register via climate/cli/`__init__.py` `_build_parser` (the 'Register your own noun groups here' hook) as modules under climate/cli/`_commands`/, each exposing an 'overview' verb, --json on every verb, and failures raised as CliError (exit 1 user / 2 environment)
  - honesty: uv run teken cli doctor . --strict still passes with the new noun groups registered
- Every new noun/verb path gets an entry in climate/explain/catalog.py, and README's CLI table + the root explain entry (still template text: 'A clonable template for AgentCulture mesh agents') are updated to describe the weather tracker
  - honesty: Every new verb path resolves under 'climate explain', and no template boilerplate ('clonable template') remains in README, the root explain entry or the parser description
- climate doctor gains environment checks for the tracker (weather-mongodb reachable, provider credentials present, service running) in the existing {healthy, checks:\[{id,passed,severity,message,remediation}\]} shape
  - honesty: doctor's tracker checks run and report sensibly from a wheel install with no culture.yaml, and a stopped stack yields failed checks with remediation rather than an exception
- weather-mongodb is its own dedicated instance: image mongo:8.0, `container_name` weather-mongodb, named volume weather-mongodb-data, restart unless-stopped, mongosh ping healthcheck, no auth — defined in a docker-compose.yml in this repo. It publishes NO host port: only the tracker and web-service containers reach it, over the compose network. (Host ports 27017 qq, 27018 data-refinery/eidetic, 27019 jlab stay untouched; 27020 is reserved for an optional, off-by-default debug publish)
  - honesty: Host port 27020 is a default, not a constant: if it is taken at 'stack up' time the verb fails with a clear message and the port is configurable
- Runtime core stays dependency-free (dependencies = \[\]): the host CLI talks to the stack with docker compose and stdlib urllib only. pymongo and the web framework live in a \[project.optional-dependencies\] extra that the container image installs, lazy-imported inside the service code; service code run without the extra exits 2 (environment error) with an install hint, never an ImportError traceback
  - honesty: A plain install without the extra still runs whoami, learn, explain, overview, doctor and cli overview with zero third-party imports
- Cadence and configuration are per provider: each provider has its own configurable poll interval, enabled flag, request parameters (e.g. forecast horizon, variables, stations) and quota, with sensible defaults in its adapter. The 5-minute tick is only the scheduler's base resolution — on each tick a provider is fetched when its own interval has elapsed AND its refresh policy allows it: met-no is Expires/Last-Modified driven with conditional GET and a mandatory identifying User-Agent; open-meteo's current block is a 900-second model slice; ims and METAR follow station cadence. Every fetch that is made is stored as-is, even when its content is unchanged
  - honesty: With recorded met-no headers as fixtures, no request is issued before Expires and the post-expiry request carries If-Modified-Since and the identifying User-Agent; a 304 is stored as a fetch record
- Storage keeps the verbatim raw provider response per fetch (body plus fetch metadata: provider, endpoint, `requested_at`, HTTP status, cache headers, content hash) as the system of record, with normalized 'current' readings derived from it and re-derivable; provider/source/model and `observed_at` vs `requested_at` stay distinct per issue 5
  - honesty: Normalized readings carry provider, source, model, `observed_at` and `requested_at`, reference their raw fetch record, and IMS `observed_at` is corrected to true UTC from the documented always-UTC+2 quirk
- A web service, run in docker and managed by the climate CLI, serves the collected data (latest reading first) so it can be consumed from outside this machine; it includes a beautiful human-facing web app, designed with the frontend-design plugin and verified with playwright
  - honesty: The web service is read-only over the collected data and binds to loopback by default; serving it to other machines is an explicit opt-in, because it has no authentication
- Locations are user configuration held outside the repo (never committed, including in test fixtures, docs and public eidetic memory); multiple locations are supported, bounded by each provider's request budget
  - honesty: A repository-wide search finds no location coordinates or place configuration: test fixtures are captured for a neutral public reference point unrelated to the user, and the public eidetic store holds none
- Output contract: markdown is the default rendering for agents and humans, --json for code; 'latest reading' is the first-class query because the AC agent controls from it
  - honesty: Every query verb renders markdown by default and the same data as --json, and 'latest' reports each value's age and provider so a stale reading is never mistaken for a fresh one
- The web app is a graph- and chart-oriented dashboard of the data we keep, with little to no prose; charts follow the dataviz skill, the visual design follows the AgentCulture look of ../org and ../culture-nodes via the frontend-design plugin, and it is verified in a real browser with the playwright MCP
  - instruction: Load the dataviz and frontend-design skills before writing any chart or UI code; drive the running dashboard with the playwright MCP in light and dark themes and at phone width
  - honesty: The dashboard stays truthful with sparse data: in its first hour, or with a provider disabled, charts show explicit empty or partial states rather than interpolated or invented lines, and observations are visually distinct from model output and from forecasts
- The dashboard carries the AgentCulture design system by point-in-time extraction, the way culture-nodes does: org's site-astro/src/styles/global.css tokens copied verbatim at a pinned org commit into this repo with an ADR recording the pin; ../org and ../culture-nodes are read-only sources and are never modified or imported at build or run time
  - honesty: A check script proves the extracted tokens byte-match the pinned org commit, and neither ../org nor ../culture-nodes shows any diff after this work
- The collection has a first-class local backup path before any S3 work: a CLI verb dumps the database (mongodump-style archive) to a host directory outside docker's volume store, and doctor warns when the newest dump is older than a configurable age
  - honesty: A dump taken from the running stack can be restored into an empty weather-mongodb and yields the same fetch-record count and content hashes
- The raw response is stored as the exact bytes received (plus content-type and declared charset), not as re-serialized JSON or a BSON-converted document; any parsed copy is additional. IMS XML arrives as ISO-8859-8 and JSON re-encoding would alter float text and key order
  - honesty: The stored bytes of a fixture response hash to the same digest as the fixture file
- Exactly one tracker fetches at a time: a lease held in weather-mongodb stops a second tracker (a host-run dev instance, a duplicated stack) from fetching, a tick that overruns is never overlapped by the next, each provider fetch has its own timeout, and tick start carries a small random jitter
  - honesty: Starting a second tracker against the same database makes it exit 2 (or idle) without issuing a single provider request
- 'latest' supports a caller-supplied maximum age: when the freshest reading for the requested provider/location is older than that, the verb exits non-zero with a distinct code and says so, so the AC agent cannot silently control from a dead tracker's last value
  - honesty: With the tracker stopped for longer than the max age, 'latest' with that option exits non-zero in both markdown and --json modes and the --json payload carries a stale flag
- The compose project bounds its own footprint: every service sets json-file log rotation (max-size/max-file), and doctor reports database size, newest fetch age per provider and host disk headroom
  - honesty: A container that logs continuously cannot exceed its configured log cap, and doctor's size and freshness figures come from the database, not estimates
- Coordinates sent to providers and stored are rounded to a configurable precision (default 2 decimals, about 1 km; never more than the 4 decimals MET Norway allows), and when the web service is exposed beyond loopback it identifies locations by user-chosen label only, never by coordinates
  - honesty: With exposure enabled, no API response or dashboard asset contains a latitude or longitude, and provider request URLs in fetch records carry only the rounded values
- doctor detects version skew between the host CLI and the running tracker/web images and reports the remediation (rebuild or pull and restart)
  - honesty: Running an older image against a newer CLI yields a failed doctor check naming both versions
- Issue 5's provider surface ships: a providers verb lists each provider's capabilities, auth requirement, configured quota, freshness strategy and enabled state (the issue's 'climate providers --limits'), and per-provider licence/attribution, freshness and rate-limit notes are documented
  - honesty: Every provider adapter has a row in the providers output and a documented attribution line, checked by a test that fails when an adapter lacks either
- The dashboard carries the attribution the data licences require (Open-Meteo CC BY 4.0, MET Norway, IMS, OpenWeather) as a compact footer — the one deliberate exception to 'little to no prose'
  - honesty: Each provider whose data appears on screen is credited on that screen with a link to its licence or terms

## Honesty conditions

- Failed fetches (timeout, 4xx/5xx, 429) are stored as fetch records too, so a gap in the data is distinguishable from a gap in collection
- The raw response body is persisted before any parsing or normalization runs, so a parser bug can never lose a provider field
- No coordinate is hard-coded anywhere in the package: with no location configured the tracker refuses to start (exit 2) instead of defaulting to a place
- The full test suite passes with network access and docker unavailable, and coverage stays at or above the configured 60%
- The `env_file` holding keys is gitignored and compose refuses nothing silently: a missing key disables only that provider and doctor reports it, and no key value appears in logs, fetch records or stored request URLs
- The connection guard refuses URIs pointing at host ports 27017, 27018 and 27019 and the tracker never runs docker commands against containers it did not create
- The AC-control agent can obtain the latest reading with one command and no prior knowledge beyond 'climate learn' / 'climate explain'
- Nothing else on this machine already records this data — confirmed by the scope survey finding no weather collection in any sibling repo
- A missed tick (stack down, host off, network out) is left as an honest gap: there is no backfill and no synthetic data
- The 24-hour figures are measured from the stored fetch records themselves by a CLI verb, not estimated
- Re-derivation is exercised by a test that rebuilds normalized readings from stored raw fixtures and compares them
- With the web container stopped and the tracker still running, 'latest' exits 2 naming the web service, collection continues unaffected, and no code path in the host CLI imports pymongo

## Success signals

- After 24 hours of unattended running: at least 95% of due fetches per enabled provider are stored (at most 288 per always-due provider per location), a host reboot is followed by collection resuming with no manual step, and the latest-reading query returns in under 1 second with data less than 10 minutes old for interval providers
- Any stored document can be traced back to its verbatim provider response: 100% of normalized readings reference a raw fetch record, and re-deriving a reading from that raw record gives the same values

## Scope / boundaries

- Location scope is the Tel-Aviv area, Israel; sources are the free providers from issue 5 (ims, openweather, open-meteo, met-no); Tomorrow.io is out
- CI (tests.yml) must stay green without network, docker, or API keys: tests use captured provider fixtures and a fake/mocked Mongo layer; the lint job's gates (black, isort, flake8, bandit -r climate, markdownlint, 'teken cli doctor . --strict') and the version-check job (every PR bumps the version + CHANGELOG) all apply to this work
- Provider API keys (IMS ApiToken, OpenWeather key) never enter the repo or the public .eidetic memory store; .gitignore already excludes .env/.envrc
- The tracker never reads or writes qq-mongodb, eidetic-mongo/data-refinery-mongo, or jlab-mongodb, and this work does not touch those repos or containers (including the orphaned eidetic-mongo container)

## Non-goals

- S3 archive is a later addition — not part of this delivery, but the stored shape should not preclude it

## Assumptions

- Only code running inside the tracker and web-service containers connects to MongoDB: pymongo, lazy-imported, through one choke-point function reading `WEATHER_MONGO_URI` (default the compose service, mongodb://weather-mongodb:27017/weather). The guard refuses loopback/host addresses on the sibling instances' ports 27017, 27018 and 27019; the compose-network service name is the expected target
- Secrets and connection strings come only from environment variables, one per resource (e.g. `WEATHER_MONGO_URI`, `CLIMATE_IMS_API_TOKEN`, `CLIMATE_OPENWEATHER_API_KEY`), validated eagerly at service start; non-secret tracker config (location, enabled providers, per-provider intervals) lives in a config file under XDG config
- Storage is stored-as-is with no dedup for now (roughly 6.5 GB/year for met-no's fixed 62 KB shape if fetched every tick, less when its ~30 min Expires is honoured; open-meteo shrinks well below 40 KB at a 48 h horizon). Compaction strategy is deliberately deferred until months of data exist
- ims (the only source of real measured solar radiation, humidity and rain near Tel Aviv) is token-gated by a manual email request to `ims@ims.gov.il` with unknown turnaround, so the tracker must ship and run usefully with ims disabled and pick it up when a token appears; Tel-Aviv-area station ids and channel ids are discovered at runtime from GET /stations, not hard-coded
- The tracker and the web service each run as their own container in the same compose project as weather-mongodb (restart: unless-stopped; secrets via a gitignored `env_file`), driven by CLI stack verbs modelled on data-refinery-cli's 'stack up/down/status'. This replaces the systemd --user proposal (c19)
- Chart series colours come from the extracted AgentCulture categorical palette only if it passes the dataviz skill's colour validator in both themes; where it fails, the dataviz-validated palette wins over brand fidelity
- Dashboard JS/CSS under climate/ is analysed by SonarCloud (sonar.sources=climate, quality gate blocks on new-code coverage) with no JS test runner in the repo, so static assets are excluded from coverage (not from analysis) in sonar-project.properties, and the browser behaviour is evidenced agent-side with the playwright MCP rather than in CI

## Scope exploration

- `s1` — `issue 5 (agentculture/climate-cli) body + owner comment`: Issue specifies a GLOBAL budget of one outbound request per 5 minutes rotated across providers; the user overrode this in-session (all providers every tick, each per its own refresh properties). Issue also fixes provider ids ims/openweather/open-meteo/met-no, keeps provider/source/model distinct, requires `observed_at` vs `requested_at`, fixture-only tests, and drops Tomorrow.io
  - seeds: `c4`, `c8`
- `s2` — `climate/cli/__init__.py + climate/cli/_commands/cli.py`: CLI is argparse noun/verb; `_build_parser` has an explicit registration hook for new noun groups; cli.py documents that any noun with action-verbs must expose 'overview' (rubric check `overview_cli_noun_exists`); `_dispatch` wraps all non-CliError exceptions so no traceback leaks
  - seeds: `c9`
- `s3` — `climate/explain/catalog.py + README.md`: explain catalog is keyed by command-path tuples with verbatim markdown per verb; root entry and README still carry template boilerplate, and the parser description in climate/cli/`__init__.py` says 'a clonable template'
  - seeds: `c10`
- `s4` — `pyproject.toml`: dependencies = \[\] today, requires-python >=3.12, version 0.4.0, console script 'climate'; dev group has pytest/xdist/cov, bandit, flake8, isort, black, teken; coverage `fail_under` = 60
  - seeds: `c11` (rejected)
- `s5` — `.github/workflows/tests.yml`: test job runs 'uv run pytest -n auto --cov=climate' on ubuntu-latest with no services block (no Mongo available); lint job runs bandit over climate/ (urllib/subprocess use will be scanned; B404/B603 already skipped) and the afi rubric gate; version-check fails any PR whose pyproject version equals main
  - seeds: `c12`
- `s6` — `climate/cli/_commands/doctor.py`: doctor reports a rubric-shaped checks list and currently only verifies identity invariants (prompt file, backend, skills); it short-circuits to a single info check when no culture.yaml is found (wheel install), so tracker checks must not depend on culture.yaml being present
  - seeds: `c13`
- `s7` — `.gitignore + CLAUDE.md memory convention`: .env and .envrc are gitignored; CLAUDE.md states eidetic memory in this repo is committed and public by default, so secrets and token values must never be /remember-ed
  - seeds: `c14`
- `s8` — `sibling mongo containers: data-refinery-cli/docker-compose.yml:56-71, jetson-ai-lab-cli/README.md:93-96 + jlab/mongo.py, autonomous-intelligence/qq/docker-compose.yml:4-19, docker inspect, ss -ltn`: No written cross-repo convention or port registry exists; de-facto convention is one mongo:8.0 container per product on its own host port (27017 qq, 27018 data-refinery, 27019 jlab; 27020 verified free via ss -ltn), no auth, pymongo, env-var URI. Newest precedent (data-refinery) uses compose + named volume + unless-stopped + mongosh ping healthcheck + loopback-only bind with `DR_BIND` opt-out; jlab is a bare docker run bound 0.0.0.0 with no restart policy; jlab/mongo.py refuses ports 27017/27018 to avoid borrowing a sibling's instance. data-refinery-cli exposes 'stack up/down/status/overview' verbs wrapping docker compose. Running eidetic-mongo is an orphan of a compose file deleted from eidetic-cli
  - seeds: `c15`, `c16`, `c17`
- `s9` — `sibling pyproject.toml files: data-refinery-cli:16-23, discord-bot-cli:16-24, ec2-cli, reterminal-cli, jetson-ai-lab-cli:16`: House norm in teken-scaffolded siblings is dependencies = \[\] plus optional extras lazy-imported with exit-2 install hints (data-refinery-cli 'store' extra carries pymongo>=4; discord-bot-cli 'discord' extra states the contract verbatim); jetson-ai-lab-cli is the lone in-family exception with pymongo directly in dependencies
  - seeds: `c18`
- `s10` — `culture/culture_core/cli/server.py:577-652 + culture_core/persistence.py + pidfile.py; reachy-mini-cli/reachy/service/units.py + manager.py; sibling compose files`: Two host-process service patterns exist in siblings (culture: fork+PID file plus generated systemd --user unit; reachy-mini-cli: systemd --user only) and every sibling compose file is data-plane only, so containerising the poller is new to this workspace. It was chosen anyway once the user asked for a dockerised, CLI-managed web service: one compose project gives a single lifecycle, reboot survival via restart policy, and `env_file` secrets without the systemd EnvironmentFile wrinkle. Costs: a Dockerfile/image build for this arm64 host, rebuild on code change, and CI cannot exercise the containers
  - seeds: `c19` (rejected)
- `s11` — `jetson-ai-lab-cli/jlab/mongo.py:51,167-180; reachy-mini-cli/reachy/demo_config.py:27-54; culture/culture_core/cli/shared/mesh.py:43`: No sibling loads .env files; secrets are env vars (jlab: single required `JLAB_MONGO_URI`, no config fallback) or OS keyring (culture link passwords); non-secret daemon config is a file the unit can run from alone (reachy: JSON under `XDG_CONFIG_HOME` because stdlib tomllib cannot write; culture: YAML under ~/.culture). A systemd --user unit does not inherit the login shell's exports, so env-var secrets need an EnvironmentFile or equivalent
  - seeds: `c20`
- `s12` — `live responses saved in scratchpad: om.json (39,838 B), met.json (61,934 B) + headers_met.txt; aviationweather.gov METAR LLBG`: Open-Meteo: keyless, one call returns current+`minutely_15`+hourly+daily; current.interval = 900 s and carries dew point, apparent temperature, shortwave/direct/diffuse radiation, UV, cloud cover, both pressures; no cache headers returned; `minutely_15` is interpolated from hourly outside Europe/North America per its docs. MET Norway complete: keyless with mandatory User-Agent, 89-entry timeseries whose first entry is the current UTC hour, Expires about 30 min after request, Last-Modified tracks the model run, no shortwave radiation field. METAR LLBG is a keyless genuine observation roughly hourly; LLSD returns nothing (airport closed)
  - seeds: `c21`
- `s13` — `measured payload sizes (om.json 39,838 B, met.json 61,934 B) x 288/day x 365`: Naive every-tick full-body storage is about 4.2 GB/yr (open-meteo, 7-day everything request) plus 6.5 GB/yr (met-no, fixed shape) on a local docker volume; met-no content is unchanged within its ~30 min Expires window, so most of that would be duplicate blobs
  - seeds: `c22`, `c23`
- `s14` — `IMS API PDF (ims.gov.il/sites/default/files/2023-01/API_Explanation_en.pdf, saved as scratchpad/ims_api.pdf) + keyless isr_cities.xml`: Envista API needs 'Authorization: ApiToken', granted manually by email with no stated SLA; channels include TD, RH, WS/WD/gusts, Rain, BP and measured radiation Grad/DiffR/NIP; channel ids differ per station so station metadata must be read first; PDF states observation time is always UTC+2 despite a +03:00 label; no numeric cadence or rate limit is documented; station ids for Tel Aviv Coast / Bet Dagan could not be obtained without a token. Keyless `isr_cities`.xml (30,922 B, ISO-8859-8) is a daily city FORECAST including 'Tel Aviv - Yafo', not observations
  - seeds: `c24`
- `s15` — `OpenWeather docs via search (openweathermap.org/api/one-call-3, /price) — not fetched live, no key used`: Free 2.5 current and forecast are separate endpoints; One Call 3.0 gives 1,000 free calls/day but only with a card on file; the 60/min and 1M/month limits and the ~10 min update cadence come from issue 5 and third-party pages, not re-verified on an OpenWeather page this session; response size unmeasured
  - seeds: `c25`
- `s16` — `culture-nodes/docs/adr/0001-culture-design-source.md + web/src/culture-design/README.md + org/site-astro/src/styles/global.css`: org's global.css ('First light over the mesh') is the canonical AgentCulture token contract: light/dark colour tokens verified at WCAG AA, mesh palette, Fraunces/Albert Sans type roles, motion easings and a reduced-motion kill switch. culture-nodes ADR 0001 extracts it verbatim at pinned org commit b4d939b into web/src/culture-design/ (tokens.css, mark.tsx, a 7-colour categorical palette.ts, edges.ts), never imports from the org checkout, and guards fidelity with scripts/check-culture-design.mjs. culture-nodes web is Vite + React 18 + TypeScript with @playwright/test e2e; org is Astro 7
  - seeds: `c30`, `c31`
- `s17` — `challenge pass / recovery + data-loss lens: docker system df on this host, spec c5/h4/c7`: Spec protects the volume from the CLI's own verbs (h4) but the only copy of a year of irreplaceable observations is one named docker volume on a dev box holding 96 volumes (13 active, ~130 GB reclaimable) where pruning is likely; 'docker volume prune -a' or a manual 'volume rm' while the stack is down destroys it, and S3 is deferred. Disk capacity is not a constraint (2.2 TB free, ~10 GB/yr projected)
  - seeds: `c39`
- `s18` — `challenge pass / unstated-assumptions lens: scratchpad isr_cities.xml (ISO-8859-8), om.json, spec c3/c22/h2`: 'Verbatim' was never defined: inserting parsed JSON into MongoDB changes number representation and key order and cannot hold XML; the saved IMS feed is ISO-8859-8, so byte-exact storage with charset metadata is the only reading that keeps the stored-as-is decision (q1) honest
  - seeds: `c40`
- `s19` — `challenge pass / concurrency lens: spec c4/c21/c29, issue 5 scheduler point 7 (jitter), MET Norway ToS`: Nothing in the spec prevents two trackers running at once (container plus a developer's host run), which would double request counts, breach met-no's Expires rule and duplicate records; overlapping ticks and per-fetch timeouts were unspecified; issue 5 asks for jitter where providers discourage synchronized calls and the spec had dropped it
  - seeds: `c41`
- `s20` — `challenge pass / failure-modes lens: spec c28/h19/c33, README exit-code policy (3+ reserved)`: h19 makes staleness visible in the output, but an agent or script controlling hardware needs it machine-checkable: a stopped tracker still returns a well-formed 'latest'. Exit codes 3+ are reserved in README, so a dedicated stale code is available
  - seeds: `c42`
- `s21` — `challenge pass / operations + observability lens: docker info (LoggingDriver=json-file), /etc/docker/daemon.json absent`: The host's docker daemon has no log limits configured, so a tracker logging every 5 minutes for a year, or a crash-looping container, grows its log without bound; the spec had no observability beyond doctor's reachability checks
  - seeds: `c43`
- `s22` — `challenge pass / security + privacy lens: spec c26/h17/c27/h18, MET Norway ToS 4-decimal rule`: c27 keeps the location out of the repo, but the spec did not notice that the location then leaves the machine three other ways: exact coordinates go to four third parties every 5 minutes, sit in every stored document and dump, and would be served to the network once the web service is exposed. Weather does not vary at address precision, so rounding costs nothing
  - seeds: `c44`
- `s23` — `challenge pass / adjacent-systems lens: spec c15/c16/c18/c26/c29`: Once tracker and web service moved into containers (c29), the reason for publishing mongo on host port 27020 and for a host-side pymongo extra is only the CLI's query verbs; routing them through the web API would remove both, shrinking the attack surface (no unauthenticated mongo port) at the cost of coupling the AC agent's read path to the web container. Recorded as question q2 for the user
- `s24` — `challenge pass / lifecycle + packaging lens: pyproject.toml hatch wheel target, data-refinery-cli stack.py:46-75, .github/workflows/publish.yml`: The wheel ships only the python package; data-refinery's stack verbs locate docker-compose.yml by walking up from the module and return None from a wheel install. The spec says one CLI command brings the stack up but never said from which install. publish.yml publishes to PyPI/TestPyPI only and builds no image (TestPyPI publishing is already known to 422 for config reasons). Recorded as question q3
- `s25` — `challenge pass / lifecycle lens (upgrade path): spec c29, version-bump-every-PR rule in tests.yml`: Every PR bumps the version, so host CLI and long-running containers drift apart routinely; nothing in the spec noticed or reported a stale image still collecting with old adapter code
  - seeds: `c45`
- `s26` — `challenge pass / counter-evidence lens: issue 5 acceptance criteria vs exported spec`: Checked the spec against issue 5's 16 acceptance criteria: provider capability/quota metadata, 'providers --limits', and licence/attribution documentation had no claim; Open-Meteo's free tier is CC BY 4.0 and non-commercial and MET Norway requires credit, which collides with the no-prose dashboard requirement (c30). The issue's 'current' and 'compare' verbs are covered by the latest query (c28); ecmwf/gfs are parked (v5)
  - seeds: `c46`, `c47`
- `s27` — `challenge pass / operations lens (CI): sonar-project.properties, tests.yml lint job, decision c38`: sonar.qualitygate.wait=true with sources=climate means a build-free JS dashboard lands as uncovered new code and can turn the gate red; bandit will also flag a 0.0.0.0 bind (B104) and urllib use (B310) in the web service and adapters. No CI job can run the containers or a browser
  - seeds: `c48`
- `s28` — `challenge pass / reversibility + rollback lens: spec c5/h4/o6/c17`: Clean: the feature is additive (new noun groups, new compose project, new port); removing it is 'stack down' plus deleting one named volume, no sibling instance or repo is written, and agent-first verbs are untouched. Residual: reversibility of the DATA is the backup finding (c39), not this lens
- `s29` — `challenge pass / time lens: spec h16/o12/c36, IMS PDF timestamp note, Israel DST`: Examined timestamp handling: IMS fixed-UTC+2 correction is obliged (o12) and met-no/open-meteo/METAR are UTC or carry offsets. Not previously stated and left to the plan: all stored times are UTC, the container clock is the host clock, and the dashboard alone converts to local time across DST changes
- `s30` — `challenge pass / migration lens: spec c22/c37`: Clean by construction: raw fetch records are the system of record and normalized readings are re-derivable (c37), so a normalized-schema change is a re-derive, not a data migration. Holds only if the raw-record shape itself is versioned — left to the plan as a `schema_version` field
- `s31` — `challenge pass / UNEXAMINED surfaces`: Not examined, and why: (1) the AC-control agent itself — it does not exist in any surveyed repo, so which variables, units and freshness it needs is unverified; (2) live IMS and OpenWeather responses — no token/key, so payload shape, size and rate behaviour rest on documentation; (3) provider terms-of-use full texts (IMS terms link, OpenWeather licence) were not read; (4) long-run behaviour of MongoDB 8 on this host under a year of append-only writes; (5) the frontend-design and dataviz skills' concrete guidance was not loaded during speccing

## Decisions

- No provider rotation: on every 5-minute tick ALL providers are sampled at once, each gated by its own refresh properties (a provider that only refreshes every 15 min is not re-fetched sooner). This supersedes issue 5's 'one global request slot per 5 minutes' scheduler wording
- openweather is polled with the single free 2.5 current-weather call only; its payload lacks dew point, solar radiation and UV, and One Call 3.0 (card on file) is not used
- Dashboard stack: a Python web-service container serves a build-free static dashboard (vanilla ES modules, inline SVG charts); no Node toolchain is added to the repo
- The CLI's query verbs go through the web service's HTTP API; the host CLI never connects to MongoDB
- 'climate stack' works from a repo checkout only in this delivery; a published container image is a follow-up issue

## Hard questions

- How much forecast do we request where the provider lets us choose (open-meteo): everything for 7+ days (about 40 KB/fetch, weighted as roughly 5 API calls) or a trimmed horizon such as 48 h? (resolved: Request a 48-hour forecast horizon where the provider lets us choose (open-meteo); fixed-shape providers are stored as they come)
- When a provider is polled on a tick but returns data identical to the last stored fetch (same body hash, or HTTP 304), do we store nothing, store a small 'seen unchanged at T' marker, or store the full body again? (resolved: Store every fetched response as-is, including unchanged ones — no dedup, no markers. Compaction (collating similars, dropping outliers, etc.) is decided later, once months to a year of data exist)
- Does 'control the tracking service' mean a systemd --user unit installed by the CLI (survives reboot, sibling reachy/culture pattern), a CLI-forked daemon with a PID file, or the poller as a container next to weather-mongodb (no sibling does this)? (resolved: Containers, not systemd: tracker and web service run as containers in the weather-mongodb compose project, managed by CLI stack verbs (proposed in c29 for the user to confirm))
- What must 'query' answer first: latest reading per provider, a time range for one variable, a cross-provider comparison at a moment, stored forecasts for a target time — and is the consumer a human, the AC agent via --json, or both? (resolved: Latest reading is the primary query, for the AC agent. Agents read markdown output, code reads --json, humans read markdown or a web app. The web app should be beautiful: built with the frontend design plugin and verified with playwright)
- Is 'Tel-Aviv area' one fixed coordinate (32.0853, 34.7818) plus the nearest IMS stations, or several configurable points, and should keyless METAR LLBG and the IMS city-forecast XML be added as extra sources beyond issue 5's four? (resolved: Locations are configuration, never committed to the repo; more than one location is allowed, bounded by each provider's request budget. Extra keyless sources (METAR LLBG, IMS city-forecast XML) are welcome)
- Should weather-mongodb bind loopback only (newest sibling convention, data-refinery) or all interfaces like jlab-mongodb and qq-mongodb — i.e. will the AC controller read the data from another machine? (resolved: The AC controller reads locally for now, so mongo itself need not be exposed; outside access is served by a CLI-managed web service running in docker, not by opening the database)
- For openweather: spend two free-tier calls per tick (current + forecast), current only, or put a card on file for One Call 3.0? (resolved: openweather: current weather only (the /data/2.5/weather call); no One Call 3.0, no card on file)

## Open parks

- [unknown_nonblocking] OpenWeather free-tier limits and update cadence, and Open-Meteo's daily quota and call-weight rule, were taken from issue 5 and third-party pages rather than fetched from the providers' own pages; they are configurable provider metadata to be re-verified when the adapters are written
- [unknown_nonblocking] The AC-control agent's actual input contract (which variables, units, max acceptable age, indoor vs outdoor coupling) is unknown because that agent does not exist yet; 'latest' is designed provider-complete so it can adapt, but the first real consumer may reshape the query surface
- [unknown_nonblocking] Provider terms of use were read only in summary; storing and re-serving third-party weather data beyond personal use (exposed web service) may carry licence conditions beyond attribution — re-check before exposing the service outside the LAN
- [unknown_nonblocking] Residual surprise risk after a rigorous pass: provider API deprecations (OpenWeather 2.5 lineage), silent provider-side schema changes that normalization mis-reads while raw storage stays correct, and host-level events (docker upgrades, disk failure) between backups. The pass raises discovery odds; it does not establish that no unknown unknowns remain
- [follow_up] Compaction of stored-as-is data (collating similars, dropping outliers) — revisit once months to a year of data exist
- [follow_up] S3 archive of the collection
- [follow_up] ecmwf-open and noaa-gfs direct-model adapters from issue 5

## Resolved vagueness

- [unknown_nonblocking] IMS ApiToken turnaround is unknown (manual email grant) and Tel-Aviv-area station and channel ids cannot be listed without it; the ims adapter is built against the documented API shape and fixtures and first verified live only once a token arrives — resolved: Token request emailed to IMS by the user on 2026-09-17; turnaround still unknown, so ims stays optional and is verified live when the token arrives
- [follow_up] Publish a multi-arch (arm64 + amd64) tracker/web image to ghcr so the stack runs from a PyPI install — resolved: Tracked as <https://github.com/agentculture/climate-cli/issues/6>
