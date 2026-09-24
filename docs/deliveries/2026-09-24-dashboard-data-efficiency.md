# Delivery evidence — dashboard data efficiency

plan: `dashboard-data-efficiency` · evidence for task `t3` · date: `2026-09-24`

This file holds the live measurements task `t3` requires. The delivery summary
(`/summarize-delivery`) is added to this file after the final PR merges.

## Method

- **Stack:** the running `climate-weather` compose project on the host, rebuilt
  with `uv run climate stack up`. The `weather-mongodb-data` volume was
  preserved; nothing was dropped or restored.
- **Fixed window:** `from=2026-09-17T07:00:00Z`, `to=2026-09-24T07:00:00Z`,
  `step=3600`, `variable=temperature`, `kind=observation&kind=model`. The
  baseline JSON was saved before the rebuild and compared against the same
  request afterwards.
- **Query plan:** `db.readings.find(…).explain("executionStats")` for the
  `open-meteo`/`model` leg of that window, which is the query `series()` issues.
- **Latency:** `curl -w %{time_total}` against `127.0.0.1:8095`, median of 5.

## Results

| Measure | Before (no index) | After the index | After deviation d1 |
|---------|-------------------|-----------------|--------------------|
| Winning plan | `SORT` ← `COLLSCAN` | `FETCH` ← `IXSCAN` | `SORT` ← `FETCH` ← `IXSCAN` |
| Documents examined / returned | 175,059 / 509 | 509 / 509 | 509 / 509 |
| Query execution time | — | 1 ms | 2 ms |
| `/series` 7 d median | 0.638 s | 0.080 s | 0.084 s |
| `/latest` median | — | 0.008 s | — |
| `/forecast` 48 h median (follow-up, not in scope) | 0.83 s | 0.61 s | — |

- **Index:** `location_1_provider_1_kind_1_observed_at_1` on `weather.readings`,
  created by the tracker at startup. The web service's store is opened with
  `ensure_indexes=False` and issues no `createIndex`.
- **Build time:** the `createIndexes` command on the live collection (about
  175,000 documents) logged 233 ms. No downtime: the web and tracker
  containers restarted in the normal `stack up` recreate.
- **Success signal (c17):** the plan shows `IXSCAN` with documents examined ≤ 2×
  returned (1×), and `/series` 7 d answers in < 100 ms. Both hold.

## Same results before and after (h1, reworded by deviation d1)

The first after-index comparison found 11 of 840 points different from the
baseline: 2 values and 9 `fetch_id`s. Every one was an `observed_at` shared by
several stored readings (a met-no model value re-issued by a later fetch, or a
METAR report repeated for hours). `series()` sorted only by `observed_at`, so
ties were resolved arbitrarily: the full scan happened to pick the earlier
fetch, and the index picked the later one.

Deviation `d1` (approved, recorded with `devague deviate`) made the tie-break
explicit: `(observed_at, requested_at)` in both stores, pinned by
`test_series_breaks_observed_at_ties_by_requested_at` in the store contract.
After the rebuild:

- two identical requests return identical bodies;
- 9 of 840 points differ from the pre-index baseline, and all 9 are ties now
  resolved to the most recently fetched reading (checked one by one against
  `requested_at` in Mongo).

## Gate

- `uv run pytest -n auto --cov=climate -q`: 1003 passed, coverage 91.6%
  (gate 60%).
- `black`, `isort`, `flake8`, `bandit -c pyproject.toml -r climate`: clean.
- `teken cli doctor . --strict`: passes.
- `climate weather latest`: unchanged output shape (markdown table, all
  enabled providers including `ims`).
