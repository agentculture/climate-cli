# dashboard data efficiency

> The weather dashboard reads only what it shows: chart and stats queries hit an index over the requested window instead of scanning all history, so response time stays flat as the collection grows

## Audience

- Dashboard viewers (local and, once published, climate.culture.dev visitors) and agents calling /series, /latest and /forecast
  - instruction: n/a

## Before → After

- Before: Each /series query examines all 172,882 readings (SORT over COLLSCAN): 0.56 s for a 6 h window and 0.60 s for 7 d, growing by about 25,000 readings a day
  - instruction: re-run the explain() and curl timings recorded in s1/s2
- After: Chart queries use an index scan over (location, provider, kind, `observed_at`): documents examined track documents returned, not collection size
  - instruction: `db.readings.find(<series query>).explain('executionStats')` shows IXSCAN

## Why it matters

- Query time stays flat as history grows, and a public site cannot be slowed down by cheap repeated chart requests
  - instruction: n/a — motivation

## Requirements

- The readings collection gets a compound index matching the range query (location, provider, kind, `observed_at`), so /series and /latest read only the requested window instead of scanning the whole collection; query time stops growing with total history (/forecast is a follow-up)
  - honesty: The tracker ensures the readings index idempotently at startup on the existing volume, and a test with a spec-recording FakeCollection pins the index spec
- Index creation is owned by the writer: the tracker ensures the readings index at startup, and the web service's store never issues createIndex, so it keeps working once issue #8 gives it a read-only Mongo user
  - honesty: A store opened in read-only mode never calls `create_index` (asserted with the FakeCollection in tests/weather/`test_mongo.py`)

## Honesty conditions

- Same query results before and after; only the plan and latency change
- The full suite passes with the pinned tests updated in the same PR, and no test is deleted to make it pass
- No change for the CLI: weather verbs keep working unchanged
- The numbers are from the live stack on 2026-09-24 (s1, s2, s8)
- The index covers the query in the ESR order (equality location/provider/kind, then `observed_at` range and sort)
- Index build time on the existing volume is seconds, with no downtime
- tests/weather/web/`test_dashboard_static.py`'s documented-params-only check still passes
- Measured and recorded in the delivery doc, not asserted

## Success signals

- On the live stack, the 7 d /series explain shows IXSCAN with totalDocsExamined ≤ 2× nReturned, and /series 7d answers in < 100 ms (from 0.60 s)
  - instruction: docker exec weather-mongodb mongosh explain + curl -w %{`time_total`} against 127.0.0.1:8095

## Scope / boundaries

- Efficiency changes must keep the pinned contracts green or update them in lockstep: tests/weather/web/`test_dashboard_static.py` (exact file list, documented-params-only, the loadShape/refresh source-string and isCurrent guard-count asserts), `test_api.py` `test_series_bounds_the_store_query_with_a_limit`, and `store_contract.py` since/until/limit assertions for both stores
- No HTTP API contract change: no pagination or cursors (docs/weather-api.md §9), the same routes, params and response shapes
  - instruction: diff docs/weather-api.md: only additive notes, no param or shape changes

## Non-goals

- No client-side pixel downsampling in chart.js — the server already grids /series by step, so a 7d window is ~168 buckets per series

## Assumptions

- Wire payloads are already small and range-bounded (/series 6h ≈ 12 KB, 7d ≈ 87 KB, /forecast 48h ≈ 10 KB); the waste is server-side scanning and client re-fetching, not response size
- Forecast rows dominate storage growth: open-meteo + met-no forecast readings are ~99% of the 172,882 readings after 7 days (~25k new readings/day), so the index matters more each week
- The index bounds /series by window, but /forecast reads kind=forecast points with `observed_at` ≥ now from every stored forecast issue before picking the newest one, so it stays proportional to the number of overlapping stored issues

## Scope exploration

- `s1` — `climate/weather/mongo.py:361-362 + live explain() on weather.readings`: Only indexes are fetches(provider,location,`requested_at`) and readings(`fetch_id`). A 7-day series query explains as SORT over COLLSCAN: 172,882 docs examined to return a window. Measured /series 6h = 0.56s and 7d = 0.60s — flat, because the cost is the full scan, not the window.
  - seeds: `c2`
- `s2` — `curl timings against 127.0.0.1:8095 + api.py /series (from/to/step/max_points push-down, MAX_SERIES_STORE_POINTS=100000)`: The API already pushes since/until/limit into store.series and grids the result (default 2000, ceiling 10000 points). The dashboard sends from and step per window (app.js:31-35), so responses are bounded; only the Mongo side reads everything.
  - seeds: `c3`
- `s3` — `climate/weather/web/static/js/app.js:5-8,28,280-319,499-513 + docs/weather-api.md:1458`: Every 60 s poll, window change and variable-tab change re-runs the full refresh(): 7 requests (health, providers, locations, latest, series, forecast, stats) re-downloading the whole window. There is no cache ('never a merge with something remembered'). The contract rules out pagination and cursors (§9), but from/to are documented, so chunking by time needs no API change.
  - seeds: `c4` (rejected)
- `s4` — `climate/weather/web/static/js/app.js:199-204 (variable tabs) + 220-278 (loadShape/loadView)`: A variable-tab click calls the same full refresh() as the poll, so 6 of the 7 requests fetch data that didn't change.
  - seeds: `c5` (rejected)
- `s5` — `climate/weather/web/static/ (grep IntersectionObserver = 0 hits) + app.js loadView`: All three bands (Now, Trend, Collection) are fetched unconditionally on every refresh; nothing is lazy today.
  - seeds: `c6` (rejected)
- `s6` — `climate/weather/web/api.py:126,1654-1686 + mongo.py _fetch_from_document`: `bytes_stored` = sum(len(record.body)) over `iter_fetches`(limit=20000); the Mongo query has no projection, so each /stats call pulls raw bodies (24 MB in the fetches collection after 7 days). Measured /stats 24h = 0.25s.
  - seeds: `c7` (rejected)
- `s7` — `climate/weather/web/static/js/chart.js:301-332 + app.js WINDOWS steps`: drawSeries emits every point as SVG, but points arrive pre-gridded (6h/900s = 24, 24h/1800s = 48, 7d/3600s = 168 buckets), so there's no rendering win without longer windows.
  - seeds: `c8`
- `s8` — `live aggregation over weather.readings grouped by provider/kind`: open-meteo forecast 147,699 + met-no forecast 23,320 out of 172,882; observation/model rows are about 1,700. Without an index, every chart query scans all the forecast rows too.
  - seeds: `c9`
- `s9` — `tests/weather/web/test_dashboard_static.py + tests/weather/web/test_api.py:655 + tests/weather/store_contract.py:384-408`: Several tests pin refresh()'s literal structure and the store limit kwarg; restructuring the refresh for chunks and lazy loading will need those tests rewritten, and a new JS module must be added to `EXPECTED_FILES`.
  - seeds: `c10`
- `s10` — `user decision on the split (2026-09-24)`: The user split the climate.culture.dev scope into two specs; this frame carries the data-efficiency half. Answering the chunk-size question, the user pointed at the measured Mongo COLLSCAN and chose the index as the fix.
  - seeds: `c11`, `c2`
- `s11` — `challenge pass / adjacent-systems lens: climate/weather/mongo.py:352-362 + web/__main__.py:53 + gh issue 8`: MongoWeatherStore.`__init__` calls `create_index` on both collections, and web/`__main__` builds the same store via mongo.`build_store`(). Issue #8 plans a read-only 'web' user; createIndex needs a write-level privilege, so putting the new index in `__init__` (as h2 proposes) would stop the web container starting once #8 lands.
  - seeds: `c18`
- `s12` — `challenge pass / unstated-assumptions lens: climate/weather/web/api.py _select_forecast_issue (store.series kind='forecast', since=now) + /forecast 48h = 0.83 s`: Every open-meteo (15 min) and met-no (30 min) fetch stores its own future horizon, so 'future points' spans many issues. The spec's success signal only measures /series, so /forecast might not improve and nobody would notice.
  - seeds: `c19`
- `s13` — `challenge pass / concurrency lens: tracker + web both constructing MongoWeatherStore at startup`: Clean for today's unauthenticated Mongo: identical concurrent createIndex calls are idempotent on the server. The residual concern is only the read-only-user case (G1).
- `s14` — `challenge pass / reversibility + migration lens: mongo index build on the live 120 MB readings collection`: Reversible (dropIndex), no data rewrite. Backup/restore round-trips indexes via mongodump metadata, and the startup ensure-index re-creates it anyway. Not probed: build time on this volume (the assumption says 'seconds').
- `s15` — `challenge pass / test-seam lens: tests/weather/test_mongo.py:137-144 FakeCollection.create_index`: The fake accepts any spec and returns a name, so 'a test pins the index spec' requires the fake to record the specs it was given — a small test-helper change, not a production one.

## Decisions

- Index first: the MongoDB full-collection scan is the problem, so add the compound readings index (user, answering the chunk-size question: 'The issue you said was mongo - let's add index')

## Hard questions

- contradiction with c18? (resolved: User: resolve by the index-ownership change (c18) — the tracker ensures the index and the web store never calls `create_index`; h2's `__init__` placement is superseded)
- Should the success signal also require /forecast 48h < 200 ms, or is reading only the newest issue (e.g. by `fetch_id` / `model_run_at`) a follow-up? (resolved: User: /forecast is a follow-up)

## Open parks

- [unknown_nonblocking] No retention policy: readings grow ~25k/day (mostly forecast rows superseded by later issues) and the new index grows with them; pruning superseded forecast issues is unexamined
- [follow_up] Follow-up spec after the index lands: client-side time-chunked loading with a cache and live-chunk-only polling (was c4), variable switch fetches only /series (c5), lazy /stats via IntersectionObserver (c6), /stats `bytes_stored` without loading raw bodies (c7) — findings s3–s6
- [follow_up] Follow-up: make /forecast read only the newest stored issue (e.g. by `fetch_id` or `model_run_at`) instead of every overlapping issue's future points — measured 0.83 s for 48 h; see c19 and s12
