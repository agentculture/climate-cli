# Delivery Summary — weather tracking service

plan: `weather-tracking-service` · run: `complete` · date: `2026-09-17`
baseline: `devague summary skeleton`

All 26 plan tasks were executed and merged on branch
`feat/weather-tracking-service`. "Complete" describes the run, not the claims:
two obligations are only partly met, two success-signal figures were not
observed, four lapses await adjudication, and the final PR is not yet open.
Read the Drift, Delivery Claims and Remaining Work sections before trusting
the word.

## Intent

> climate-cli tracks Tel-Aviv-area weather every 5 minutes from several free
> providers, keeps every response in full (current state, forecast, all stats)
> in a local weather-mongodb docker instance, and the climate CLI controls the
> tracking service and queries what it collected

After: one CLI stack command brings up weather-mongodb, the tracker and the
web service; every provider is sampled on its own refresh policy; every
response is kept in full; "latest reading" answers in markdown or `--json`;
and a dashboard charts what has been collected. The run executed the confirmed
plan `docs/plans/2026-09-17-weather-tracking-service.md` (26 tasks, 6 waves)
through `/assign-to-workforce`, for GitHub issue 5.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Test harness guards and captured provider fixtures
- `t2` — User configuration: locations, per-provider settings, coordinate rounding
- `t3` — Storage model, store interface and in-memory fake
- `t4` — HTTP fetch helper with timeouts and secret redaction
- `t5` — Provider interface: capabilities, quota, refresh policy, attribution
- `t6` — HTTP API contract document
- `t7` — AgentCulture design tokens extracted at a pinned org commit
- `t8` — Container image and compose project
- `t9` — MongoDB store, connection choke-point and tracker lease
- `t10` — open-meteo adapter
- `t11` — met-no adapter with Expires-driven refresh
- `t12` — openweather adapter (current weather only)
- `t13` — ims adapter (Envista station observations)
- `t14` — Keyless extras: METAR and IMS city-forecast XML adapters
- `t15` — Read-only web API service
- `t16` — Host CLI: stack noun (up, down, status, overview)
- `t17` — Host CLI: weather query verbs over the HTTP API
- `t18` — Host CLI: providers verb
- `t19` — Scheduler: per-provider due logic, isolation, no overlap, jitter
- `t20` — Re-derivation of normalized readings from raw fetch records
- `t21` — Tracker service entrypoint
- `t22` — Chart-first dashboard in the AgentCulture design
- `t23` — Host CLI: backup verb (dump and restore)
- `t24` — doctor: tracker environment checks
- `t25` — Integration: register nouns, explain catalog, packaging, docs, CI config
- `t26` — Live validation on this host

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | Socket-blocking `tests/conftest.py`, coordinate-hygiene scan, fixtures for all six providers; merge `1dcbff1`. The met-no fixture was first captured from the wrong endpoint and recaptured in `9b89ce6` |
| `t2` | delivered | `climate/weather/config.py`; merge `b0b79d1` after two reworks (exit code 2 for over-quota, narrower literal scan) |
| `t3` | delivered | `climate/weather/store.py` plus a reusable conformance suite; merge `8704b35` |
| `t4` | delivered | `climate/weather/http.py`, https-only, redaction; merge `3484162` |
| `t5` | delivered | `climate/weather/providers/base.py` and the auto-discovering registry; merge `e1874fe` after one rework (hard-coded coordinates in tests) |
| `t6` | delivered | `docs/weather-api.md`; merge `2518446`. Vocabulary extended later in `9b89ce6` |
| `t7` | delivered | `tokens.css` at org pin `b4d939b`, ADR 0001, check script; merge `4e15467` |
| `t8` | delivered | `Dockerfile`, `docker-compose.yml`, env example; merge `18e368b` after reworks (config mount outside the repo, port 8095). Bind bug fixed live in `573f842` |
| `t9` | delivered | `climate/weather/mongo.py`: guarded connection, Mongo store, lease; merge `ca7c747`. Verified against real MongoDB only in `t26` |
| `t10` | delivered | open-meteo adapter; merge `910d86c` after one rework (dropped variables, skipped blocks) |
| `t11` | delivered | met-no adapter; merge `af9e791` after one rework (complete product, header mapping) |
| `t12` | delivered | openweather adapter; merge `0c7a1b2` shipped with non-vocabulary ids and a Kelvin fixture, fixed in `38bf693`. Live: the new key returned 401 at 19:38 UTC (OpenWeather activation delay) and 200 at 19:50 on the next scheduled attempt. The first real payload exposed a city-id leak, fixed in `874d802` |
| `t13` | partial | ims adapter; merge `49646b4`. Built from documentation and a synthesized fixture only; never run against the real API (no token) |
| `t14` | delivered | metar and ims-forecast adapters; merge `f97c72c` after one rework (city names left the machine) |
| `t15` | delivered | Read-only API on `http.server`; merge `1a8c12b`. Forecast route bug fixed live in `f9c76ea` |
| `t16` | delivered | `climate stack`; merge `d4efa0f`. `--env-file` added in `573f842` |
| `t17` | delivered | `climate weather latest/series/forecast/stats`, exit 3 for stale; merge `e925b38` |
| `t18` | delivered | `climate providers [--limits]`; merge `57f0bb1` after one rework |
| `t19` | delivered | Scheduler; merge `b06f744` |
| `t20` | delivered | `climate/weather/rederive.py`; merge `ca60a83` |
| `t21` | delivered | Tracker entrypoint with heartbeat and lease; merge `4dc1c31` |
| `t22` | delivered | Build-free dashboard, four demo screenshots under `docs/design/`; merge `8c06147`. Verified with Playwright's bundled Chromium, not the Playwright MCP |
| `t23` | delivered | `climate backup dump/list/restore`; merge `9091fb3` |
| `t24` | delivered | doctor weather checks; merge `323ec96` |
| `t25` | delivered | Noun registration, explain catalog, `weather` extra, README, CLAUDE.md, version 0.5.0; merge `3b0a5d8` |
| `t26` | partial | Live run done by the main agent on this host: stack up, real fetches, CLI, lease, web-down, dump and restore, doctor, live screenshots. The 24-hour and reboot criteria were not observed |

## Mid-work Decisions

No `/deviate` record was filed: `devague deviate --list` is empty. Every item
below is a decision no record covers, captured directly.

- The user changed the tracked place mid-run to a town with a nearby-city
  fallback, stating it is configuration only and must not be recorded as a
  deviation. Handled as config outside the repo; `t14` gained an ordered
  `cities` candidate list to support the fallback.
- Three parallel-built contracts did not line up (`t2`, `t3`, `t5`). The main
  agent reconciled them in `603a47c`: an unset provider interval means the
  adapter default, a `params` alias, `is_due` reading `cache_headers`.
- `RequestSpec` would print a live API key; fixed in `dd8baa5`.
- The API vocabulary acted as a filter, so adapters dropped provider variables.
  `9b89ce6` added 18 rows and the rule that nothing is dropped (`x_<name>`).
- `conditional_headers` was changed to return a header mapping so the scheduler
  and the met-no adapter agree.
- `mongo.build_store()`, `lease_collection()` and an optional tracker heartbeat
  were added in `e749628` to close gaps reported by `t15`.
- `t14` first sent the configured city names to IMS as a query parameter; they
  now travel in the URL fragment, which is stored but never transmitted.
- A latent flaky test from `t17` (a re-imported `_errors` module left in
  `sys.modules`) was fixed in `0967256` after it reverted the `t21` merge.
- Series colours use the dataviz-validated palette because the AgentCulture
  categorical palette failed four validator gates in both themes (assumption
  c32 anticipated this).
- The Playwright MCP cannot launch on this host (no system Chrome); browser
  checks used Playwright's bundled Chromium directly.
- Live validation found and fixed two defects: the web container listened on
  loopback inside the container (`573f842`), and `/forecast` returned zero
  points for any real fetch time (`f9c76ea`).
- The OpenWeather key was injected from the user's `grant` store into the
  gitignored `docker/weather.env` without being printed.
- After the summary was first written, the first successful OpenWeather
  payload showed the adapter emitting the provider's city id; it was removed
  and the stored readings were re-derived so the id left the database too.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t1` | Captured met-no `compact` instead of `complete`; merged on green tests and caught only when `t11` reported a missing variable (lapse `l1`) | acceptable |
| `t6` | Contract changed after merge: 18 vocabulary rows, the no-drop rule and unit `other`; also an unplanned `/locations` route (deltas `b1`, `b5`) | acceptable |
| `t7` | The fidelity check script is not wired into CI, which has no org checkout (evidence `e18` fail, delta `b7`) | needs-follow-up |
| `t8` | One variable meant both the host mapping and the in-container bind, making the service unreachable; split in `573f842` (delta `b3`) | acceptable |
| `t12` | Shipped violating the API vocabulary with a Kelvin fixture (lapse `l2`), and emitted OpenWeather's city id, which resolves to a place name; both fixed, the second only after the first live payload | risky |
| `t13` | Never exercised against the real IMS API (plan risk `r1`) | needs-follow-up |
| `t14` | City candidates moved to the URL fragment for privacy (delta `b2`) | acceptable |
| `t15` | Several contract fields are always `null` or empty (`model_run_at`, `station`, `interval_seconds`, `capabilities.variables`); due counts for non-interval providers are estimates | needs-follow-up |
| `t22` | Verified with bundled Chromium rather than the Playwright MCP; the agent's attempted browser install deleted a shared Chromium bundle outside its worktree (lapse `l3`); no automated test covers the no-interpolation rule | risky |
| `t24` | doctor now fails only on error-severity checks, and shows a warning-level FAIL for the fetch age of providers that are merely disabled (delta `b6`) | acceptable |
| `t25` | Wheel contents verified manually, not by a test; `uv sync --all-extras` breaks three tests that assert pymongo is absent | needs-follow-up |
| `t26` | Run by the main agent as planned; the 24-hour 95 % figure and the reboot-resume check were not observed (plan risk `r3`) | needs-follow-up |

Obligation `o8` is a drift of the record itself: it was approved before
decision c49, could not be rejected afterwards, and is superseded by `o15`
(plan risk `r5`, evidence `e19` fail by design).

## Evidence

- tests: full suite `uv run pytest -n auto -q` — 791 passed at `f9c76ea`
  (baseline before the run: 22); 30 consecutive parallel runs with 0 failures
  after `0967256`
- tests: the 121 obligation-mapped tests listed in evidence `e1`–`e17` — pass
- coverage: 90.54 % reported by `t25` (gate 60 %) — not re-measured afterwards
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -r climate`
  — clean; `uv run teken cli doctor . --strict` — all checks PASS after `t25`
- markdown: `markdownlint-cli2` over the repo — 0 errors after `t25`
- script: `python3 scripts/check-culture-design.py` — byte match against org
  pin `b4d939b`
- live (this host, 2026-09-17): `climate stack up` healthy; first tick fetched
  met-no (92 readings), open-meteo (263), metar (1), ims-forecast (5);
  `weather latest` markdown and `--json`; `--max-age 60` exit 3 and `1h` exit
  0; second tracker exit 2; web stopped exit 2 then recovery; dump restored
  into a scratch MongoDB with equal counts (4 fetches, 361 readings) and equal
  content hashes; all stored bodies re-hash to their recorded sha256; doctor
  healthy with tracker and CLI both 0.5.0; OpenWeather 401 stored with
  `appid=REDACTED` and the key absent from container logs; live dashboard
  loaded with no console error, no overflow and no coordinate or place name
- commits: `2420037..38eb5cd` (78 commits, 113 files)
- PRs / issues: issue `#5` (the work), issue `#6` (published image follow-up);
  no PR opened yet
- devague records: evidence `e1`–`e19`, deltas `b1`–`b7`, lapses `l1`–`l4` —
  all `proposed`, none adjudicated. Delta `b7` links `e17` where `e18` was
  meant

## Delivery Claims

Evidence records are `llm`-origin and still `proposed`, and lapses `l1`–`l4`
are pending, so none of them is yet adjudicated evidence. Confidence below is
the operator's, capped where a lapse touches the claim.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Every response is stored byte-for-byte before parsing, failures included (c1, c2, c22, c40) | high | `e1`, `e2`; live re-hash of stored bodies; commit `b06f744` |
| Per-provider cadence with Expires honoured and no rotation (c4, c21) | high | `e3`, `e14`; live restart did not re-fetch |
| Only one tracker fetches at a time (c41) | high | live second tracker exit 2; `tests/weather/test_tracker.py` |
| Keys never reach the repo, logs or stored URLs (c14) | high | `e4`; live grep for the real key returned 0 |
| Location stays out of the repo and out of API responses; coordinates rounded (c27, c44) | high | `e5`, `e10`; `tests/test_repo_hygiene.py`; live payload and page text check |
| The host CLI is standard-library only; queries go through the HTTP API (c18, c49) | high | `e8`, `e13` |
| `latest` answers in markdown and `--json` with a stale exit code (c28, c42) | high | `e9`; live exits 3 and 0 |
| The stack is dedicated, keeps its data across recreation, publishes no mongo port (c5, c15, c17) | high | `e6`, `e7`; live recreate |
| Backups restore faithfully (c39) | high | live scratch restore, equal counts and hashes |
| doctor reports stack, freshness, disk, backup and version skew (c13, c43, c45) | medium | live doctor run; `tests/test_cli_doctor_weather.py`. Skew path only unit-tested |
| The web service is read-only and loopback by default (c26) | medium | `e10`. The default was unreachable until `573f842`; exposure beyond loopback was never exercised |
| The dashboard is chart-first, in the AgentCulture look, with attribution (c30, c47) | medium | `docs/design/*.png`; live screenshots; `e16`. No automated browser test; MCP not used |
| Design tokens are a verbatim pinned copy (c31) | medium | `e17` pass, `e18` fail (not in CI) |
| met-no normalizes the complete product (c3) | medium | `tests/weather/providers/test_met_no.py`; live 92 readings. Capped by lapse `l1` |
| openweather normalizes to the contract vocabulary and emits no place identifier | medium | `tests/weather/providers/test_vocabulary_conformance.py`; live HTTP 200 with readings; commit `874d802`. Capped by lapse `l2`; fixture still synthesized |
| ims works against the real Envista API | unverified | (no evidence — no token; not claimed done) |
| At least 95 % of due fetches stored over 24 unattended hours; collection resumes after a reboot (c36) | unverified | (no evidence — not observed; not claimed done) |
| CI is green for this branch, including SonarCloud | unverified | (no evidence — nothing pushed, no PR) |

Lapse ledger evidence: pending approval (not yet evidence): `l1`, `l2`, `l3`,
`l4`.

## Remaining Work / Follow-up

- Open the final PR (human gate 3) and watch CI and the SonarCloud gate on the
  new dashboard JavaScript — main agent, on the user's go-ahead.
- Adjudicate the proposed records: evidence `e1`–`e19`, deltas `b1`–`b7`,
  lapses `l1`–`l4` — user.
- `t26` — observe and file the 24-hour completeness figure
  (`climate weather stats`) and the reboot-resume check. The stack is running
  now, so the first figure is readable tomorrow.
- `t12` — replace the synthesized OpenWeather fixture with a captured one
  (place fields removed) now that a key exists.
- API provenance still names the configured METAR station and the fallback
  forecast city; decide whether to make them opaque before the web service is
  ever exposed beyond loopback — user.
- `t13` — verify ims live when the emailed token arrives; add it to
  `docker/weather.env` as `CLIMATE_IMS_API_TOKEN`.
- `t7` — decide whether the token fidelity check belongs in CI at all, given
  CI has no org checkout.
- `t15` — fill the contract's null fields (`station`, `interval_seconds`,
  `model_run_at`, `capabilities.variables`) or drop them from the contract.
- `t22` — add an automated check for the no-interpolation rule; the dashboard
  mixes providers inside one tile (METAR wind speed beside Open-Meteo gust).
- `t24` — stop reporting a FAIL for the fetch age of a disabled provider.
- `t25` — test wheel contents in CI; decide how CI treats the `weather` extra.
- Machine side effect: `~/.cache/ms-playwright/chromium-1234` was deleted by a
  task agent; restore with `npx playwright install chromium` if another
  project needs it. The Playwright MCP needs a system Chrome to work here.
- Spec and frame still quote issue 5's public city-centre coordinates inside a
  resolved question (plan risk `r4`).
- Follow-ups already tracked: published multi-arch image (issue `#6`), S3
  archive, stored-data compaction, ecmwf-open and noaa-gfs adapters.
