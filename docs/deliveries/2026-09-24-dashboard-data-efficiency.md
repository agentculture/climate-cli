# Delivery Summary — dashboard data efficiency

plan: `dashboard-data-efficiency` · run: `complete` · date: `2026-09-24`
baseline: `devague summary skeleton`

All three plan tasks merged on branch `feat/climate-culture-dev` and are in
PR #10, which is open and not yet merged. One approved mid-run deviation (`d1`)
changed what "same results" means. The confirmed claim `c2` overstated what the
index fixes (lapse `l1`, approved); `/forecast` remains a follow-up.

## Intent

> The weather dashboard reads only what it shows: chart and stats queries hit
> an index over the requested window instead of scanning all history, so
> response time stays flat as the collection grows

After: chart queries use an index scan over (location, provider, kind,
`observed_at`), so documents examined track documents returned, not collection
size. The run executed `docs/plans/2026-09-24-dashboard-data-efficiency.md`
(3 tasks, 3 waves) through `/assign-to-workforce`, with the split approved as
proposed (`docs/plans/2026-09-24-dashboard-data-efficiency-split.md`).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Mongo store: readings compound index, created only when the store is opened for writing
- `t2` — Wire index ownership: the tracker opens the store with indexes ensured, the web service opens it without
- `t3` — Verify on the live stack and record the delivery evidence (agent-side: docker + Mongo)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `MongoWeatherStore(..., ensure_indexes=True)` and `build_store(ensure_indexes=...)`; new readings index `[("location",1),("provider",1),("kind",1),("observed_at",1)]`; the two existing indexes unchanged; 4 tests in `tests/weather/test_mongo.py` (commit `efde32a`, sonnet subagent, merged `bb8455d`) |
| `t2` | delivered | tracker calls `build_store(ensure_indexes=True)`, web calls `build_store(ensure_indexes=False)`; tests in `tests/weather/test_tracker.py` and new `tests/weather/web/test_main.py` (commit `d0c4fa6`, sonnet subagent, merged `a825d99`) |
| `t3` | delivered | baseline taken, stack rebuilt with the volume kept, before/after measured; found the tie-break gap, which led to `d1` (fix `1cb1e5c`, merged `9c38fe0`); re-measured; evidence below (commit `7f7f7da`) |

## Mid-work Decisions

- `d1` — Add a deterministic tie-break to `series()`: order by
  (`observed_at`, `requested_at`) in both the Mongo and in-memory stores, with
  a store-contract test, so `agg=last` always picks the most recently fetched
  reading (the rule `/latest` already uses); reword h1 to "same values before
  and after, except where several readings share an `observed_at` — previously
  unspecified, now deterministically the most recently fetched". Reason: t3's
  live before/after check found 2 of 840 `/series` 7 d values and 9
  `fetch_id`s changed. Readings sharing an `observed_at` were tie-broken
  arbitrarily: the full-scan SORT picked the earlier fetch, and the index
  picked the later one. So h1 "same results" could not hold as written.
- The tie-break adds an in-memory `SORT` stage over the window's matching
  documents, instead of extending the index with `requested_at`. That keeps
  the pinned index spec from `t1` and the one index already built. The sort
  covers 509 documents (2 ms), not the collection. No deviation record covers
  this implementation choice; it's captured here.
- The main agent wrote the `d1` fix itself in its own worktree
  (`agent/dde-d1`), not through a subagent, because it was a two-line change
  plus one contract test.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t3` (`d1`) | t3's live before/after check found 2 of 840 /series 7d values and 9 `fetch_ids` changed: readings sharing an `observed_at` were tie-broken arbitrarily (the full-scan SORT picked the earlier fetch, the index picks the later one), so h1 'same results' could not hold as written | `acceptable` |

`t1` and `t2` delivered their contracts with no drift.

## Evidence

Live method: the running `climate-weather` stack, rebuilt with
`uv run climate stack up` (the `weather-mongodb-data` volume was preserved).
Fixed window `from=2026-09-17T07:00:00Z`, `to=2026-09-24T07:00:00Z`,
`step=3600`, `variable=temperature`, `kind=observation&kind=model`. Baseline
JSON was saved before the rebuild. `explain("executionStats")` ran on the
`open-meteo`/`model` leg of that window. Latency is the median of 5
`curl -w %{time_total}` runs against `127.0.0.1:8095`.

| Measure | Before (no index) | After the index | After `d1` |
|---------|-------------------|-----------------|------------|
| Winning plan | `SORT` ← `COLLSCAN` | `FETCH` ← `IXSCAN` | `SORT` ← `FETCH` ← `IXSCAN` |
| Documents examined / returned | 175,059 / 509 | 509 / 509 | 509 / 509 |
| Query execution time | — | 1 ms | 2 ms |
| `/series` 7 d median | 0.638 s | 0.080 s | 0.084 s |
| `/latest` median | — | 0.008 s | — |
| `/forecast` 48 h median (out of scope) | 0.83 s | 0.61 s | — |

- index build: the `createIndexes` command logged 233 ms on about 175,000
  documents; containers restarted in the normal `stack up` recreate.
- h1 as reworded by `d1`: two identical requests return identical bodies. 9 of
  840 points differ from the pre-index baseline, and all 9 are `observed_at`
  ties resolved to the newest `requested_at`, each checked in Mongo.
- tests (at `475b8e3`):
  - `tests/weather/test_mongo.py::test_readings_compound_index_pins_the_exact_esr_spec` — pass
  - `tests/weather/test_mongo.py::test_ensure_indexes_false_issues_no_create_index_calls` — pass
  - `tests/weather/web/test_main.py` — pass
  - `tests/weather/test_tracker.py::test_default_store_factory_asks_mongo_to_ensure_indexes` — pass
  - `tests/weather/store_contract.py::test_series_breaks_observed_at_ties_by_requested_at` (InMemory + Mongo) — pass
  - `tests/weather/web/test_dashboard_static.py::test_the_dashboard_only_uses_documented_query_parameters` — pass
  - full suite `uv run pytest -n auto --cov=climate -q` — 1003 passed, coverage 91.6%
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -c pyproject.toml -r climate` — clean; `teken cli doctor . --strict` — pass
- commits: `main..475b8e3` on `feat/climate-culture-dev`
- PRs / issues: PR #10 (open; CI green, Qodo review pending); related #8
- validation records (`/validate-delivery`, all `proposed`): obligations
  `o1`–`o5`, evidence `e1`–`e7`, delta `b2` (`b1` superseded; it cited the
  wrong evidence)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `weather.readings` has the compound `(location, provider, kind, observed_at)` index and `/series` uses it | high | test `tests/weather/test_mongo.py::test_readings_compound_index_pins_the_exact_esr_spec` · live explain `IXSCAN` 509/509 · commit `efde32a` |
| `/series` 7 d answers in < 100 ms (0.084 s, from 0.638 s) | high | live measurement above · evidence `e4` |
| the web service never issues `createIndex`; the tracker owns index creation | high | tests `tests/weather/web/test_main.py`, `tests/weather/test_tracker.py::test_default_store_factory_asks_mongo_to_ensure_indexes` · commit `d0c4fa6` |
| `series()` resolves `observed_at` ties to the most recently fetched reading, deterministically | high | test `tests/weather/store_contract.py::test_series_breaks_observed_at_ties_by_requested_at` · commit `1cb1e5c` · deviation `d1` |
| no HTTP API contract change | high | test `tests/weather/web/test_dashboard_static.py::test_the_dashboard_only_uses_documented_query_parameters` passes unmodified |
| `/latest` reads only the window via the index | medium | 0.008 s observed; no explain was run for the `/latest` query shape |
| the index makes `/forecast` read only the requested window (claim `c2` as originally written) | low | lapse `l1` (approved): `/forecast` was never traced; it still reads every stored forecast issue (0.61 s) |
| query time stays flat as history grows | unverified | measured at one collection size only; flatness over time is not yet observed |

Lapse ledger evidence:

| Lapse | Code | What |
|-------|------|------|
| `l1` | `assumption-for-measurement` | Claim c2 (confirmed) states the index makes /series, /latest and /forecast read only the requested window; /forecast was never traced — its query is kind=forecast, since=now across every stored issue, so the index does not bound it the way c2 says |

## Remaining Work / Follow-up

- Merge PR #10 once the Qodo review is triaged — owner: the user (gate 3).
- `/forecast`: read only the newest stored issue instead of every
  overlapping issue's future points (parked `v3`, measured 0.61 s) — follow-up
  spec.
- Client-side chunked loading, variable-switch refetch, lazy `/stats`, and
  `/stats` without raw bodies (parked `v1`, findings `s3`–`s6`) — follow-up
  spec.
- Retention: readings grow about 25,000 a day and the index grows with them
  (parked `v2`).
- Adjudicate the `/validate-delivery` records (`o1`–`o5`, `e1`–`e7`, `b2`) —
  owner: the user.
- Re-measure `/series` 7 d after a few weeks of growth to settle the
  "stays flat" claim.
