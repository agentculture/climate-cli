# Weather HTTP API — contract v1

This document is the **contract** between three independently built parts of the
weather tracking service:

- the read-only web API service (`climate/weather/web/`, standard-library
  `http.server`),
- the chart-first dashboard (`climate/weather/web/static/`, vanilla ES modules
  and inline SVG),
- the host CLI query verbs (`climate weather latest|series|forecast|stats`,
  `climate providers`), which talk to this API with `urllib` and never touch
  MongoDB.

Anything not written here is not part of the contract. If an implementation
needs a field, a parameter or a status code that this document does not define,
that is a plan deviation — record it with `/deviate`, do not invent it locally.

## 1. Scope and hard rules

1. **The API is read-only.** There are **no write routes**. `POST`, `PUT`,
   `PATCH`, `DELETE` and every other method return `405` (see
   [Methods](#24-methods)). Nothing exposed here can create, mutate or delete a
   fetch record, a reading, a provider, a location or a configuration value.
2. **Locations are identified by user-chosen label only.** A location appears in
   requests and responses as an opaque string such as `"home"` or `"office"`.
3. **Coordinates never appear in any response.** No latitude, no longitude, no
   bounding box, no geohash, no place name, no station coordinates, no provider
   request URL that carries a coordinate — in any field, any error message, any
   log line served over HTTP, and any static dashboard asset. A provider's
   request URL is never echoed; only its endpoint *name* is (see
   [`provenance.source`](#31-provenance)).
4. **All times are UTC ISO-8601** (see [Time](#25-time)). The API never emits a
   local time or a non-UTC offset. Converting to local time is the dashboard's
   job and happens in the browser only.
5. **Every value carries its own unit, `observed_at`, `age_seconds`,
   `provenance` and `stale` flag.** A bare number never appears without them.
6. **Missing data is explicit.** Gaps are `null`, never interpolated, never
   omitted, never back-filled. Empty results are `200` with empty arrays, never
   `404`.

## 2. Transport

### 2.1 Base URL and versioning

| Thing | Value |
| --- | --- |
| Default bind | `127.0.0.1` (loopback; changing it is an explicit opt-in setting) |
| Default port | `8095` |
| Default base URL | `http://127.0.0.1:8095` |
| API prefix | `/api/v1` |
| Dashboard | `/` (static files served from `climate/weather/web/static/`) |
| CLI override | `CLIMATE_WEATHER_URL` (base URL, no trailing slash, no `/api/v1`) |

Every API route in this document is written relative to the prefix: the full
path of `GET /health` is `GET /api/v1/health`.

The prefix is the only version marker. A breaking change to any shape defined
here ships as `/api/v2`; `/api/v1` keeps its shape. Adding a new **optional**
query parameter, a new route, or a new key to an object is *not* breaking, so
clients must ignore keys they do not recognise.

`GET /` serves `index.html`; any other path under `/` that is not `/api/…`
resolves inside the static directory and returns `404` if absent. Path traversal
(`..`, absolute paths, symlinks leaving the directory) returns `404`.

### 2.2 Content type and encoding

- Request bodies are not read. Query strings only.
- Every API response is `Content-Type: application/json; charset=utf-8`, UTF-8
  encoded, including every error response.
- JSON is emitted with ASCII-safe escaping; no byte-order mark; a single
  trailing newline is permitted.
- Numbers are JSON numbers, not strings. Counts and `*_seconds` fields are
  integers. Measured values are floats (a whole measurement may still serialize
  as `21` rather than `21.0`; clients must accept both).
- Booleans are real booleans. `"true"` as a string is never returned.

### 2.3 Response headers

| Header | Value | Notes |
| --- | --- | --- |
| `Content-Type` | `application/json; charset=utf-8` | API routes |
| `Cache-Control` | `no-store` | all API routes; data changes every tick |
| `X-Climate-Api-Version` | `v1` | every API response, including errors |
| `Allow` | `GET, HEAD` | only on a `405` |

No CORS headers are sent. The dashboard is same-origin with the API, so it needs
none, and the service has no authentication — advertising cross-origin access
would be a gratuitous exposure.

### 2.4 Methods

`GET` and `HEAD` are the only supported methods. `HEAD` returns the headers of
the corresponding `GET` with an empty body. Every other method returns `405`
with the `Allow: GET, HEAD` header and the standard error envelope.

### 2.5 Time

- Format: `YYYY-MM-DDTHH:MM:SSZ` — UTC, second precision, literal trailing `Z`.
  Example: `2026-09-17T08:35:00Z`.
- No fractional seconds are emitted. Clients must nonetheless parse an optional
  `.sss` without failing.
- Timestamp **parameters** accept the same format and additionally accept a
  `+00:00` offset spelling. Any other offset is rejected with
  `invalid_parameter`.
- `observed_at` is the provider's own timestamp for the measurement or model
  step. `requested_at` is when the tracker issued the fetch. They are always
  distinct fields and never substituted for one another.
- `age_seconds` is an integer, `generated_at - observed_at`, computed at
  response time. It may be negative for forecast values, whose `observed_at` (a
  valid time) lies in the future; for forecasts use `lead_seconds` instead.

### 2.6 Nullability

**Every key documented here is always present in the response.** A key is never
dropped to signal absence. Absence is `null` (or an empty array / empty object
for collection-valued keys). `null` means *not known or not provided by this
provider*; it never means zero.

### 2.7 Limits

The API paginates nothing. It bounds result size instead, and says when it did:

- `series` returns at most `max_points` points per series (default `2000`, hard
  ceiling `10000`). The grid is **sized arithmetically before it is built**, so
  a window naming millions of buckets never allocates more than the ceiling.
- Every windowed route — `series` and `stats` — accepts a span (`to - from`,
  however it was spelled, `stats`'s `window` included) of at most
  **`31622400` seconds (366 days)**. A longer span is rejected with
  `invalid_parameter`, whose `detail` carries `span_seconds` and
  `max_span_seconds`. The cap exists because neither route's response size
  bounds its store query: `series`'s `step` has no maximum, and `stats`
  without a `bucket` has no grid at all.
- `series` truncation keeps the **earliest** buckets. The response's `to` then
  reports the end of the window actually returned, not the one requested, and
  the store is queried only over that narrowed window.
- One `series` entry materializes at most `100000` stored points; a series that
  hits the cap keeps its oldest points and adds a `truncated` warning.
- One `stats` row materializes at most `20000` fetch records — fewer than the
  `series` cap because a fetch record carries the provider's raw response
  body. `stored_count` and `completeness` come from a *count* query and stay
  exact however many records the window holds; when the cap applies, the
  newest `20000` records are read, the status, `bytes_stored`, cadence and
  `buckets` figures describe those, and a `truncated` warning names the row.
- `forecast` returns at most `168` hours of horizon, **measured from
  `generated_at`**, not from the issue time.
- When a bound truncated the result, the response's `warnings` array carries a
  `truncated` entry and the relevant `*_count` fields describe what was
  returned, not what exists.

## 3. Common objects

### 3.1 `provenance`

Attached to **every** value, and repeated at reading level where it is uniform.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `provider` | string | no | Registry provider id: `open-meteo`, `met-no`, `openweather`, `ims`, `metar`, `ims-forecast` |
| `source` | string | no | The provider endpoint or feed this value came from, by name, never a URL with parameters. Examples: `locationforecast/2.0/complete`, `v1/forecast`, `data/2.5/weather`, `envista/v1/stations/{id}/data/latest`, `metar/{station}` |
| `model` | string | yes | The numeric model or product identifier when the provider names one (`MEPS`, `ECMWF-IFS`, `GFS`); `null` for direct observations |
| `model_run_at` | string (time) | yes | The **provider's** model run / issue time in UTC; `null` when not a model value or when the provider stated none. Never the time the tracker fetched the response — that is `requested_at` |
| `station` | string | yes | Station or site identifier when the provider is station-based (`ims`, `metar`); `null` otherwise. Station ids are provider identifiers, not coordinates |
| `interval_seconds` | integer | yes | The provider's own declared validity slice for this value (e.g. Open-Meteo's `current.interval` of `900`); `null` when the provider declares none |
| `fetch_id` | string | no | Opaque id of the raw fetch record this value was derived from. Stable, comparable for equality, and carries no location information |
| `schema_version` | integer | no | Storage schema version of that fetch record |

`provider`, `source` and `model` stay distinct and are never collapsed into one
string. Per-value provenance is authoritative because one reading can mix
sources: an `ims` reading carries channels from more than one station, and each
value names its own.

### 3.2 `value`

The atom of the API. Returned by `latest` (inside `values`) and, in reduced
form, inside `series` and `forecast` points.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `value` | number \| string | yes | The measurement. `null` when the provider did not supply this variable in this fetch. String only for `code`-typed variables |
| `unit` | string | no | Canonical unit id from the [unit vocabulary](#41-units). Present even when `value` is `null` |
| `original_value` | number \| string | yes | The value exactly as the provider expressed it, before unit conversion. Equals `value` when no conversion happened |
| `original_unit` | string | yes | The provider's own unit id; `null` when the provider declared none |
| `observed_at` | string (time) | yes | Provider timestamp for this value |
| `requested_at` | string (time) | no | When the tracker fetched it |
| `age_seconds` | integer | yes | `generated_at - observed_at`; `null` when `observed_at` is `null` |
| `kind` | string | no | One of `observation`, `model`, `forecast` — see [kinds](#42-kinds) |
| `stale` | boolean | no | See [staleness](#43-staleness) |
| `stale_after_seconds` | integer | no | The age threshold this value's `stale` was decided against |
| `quality` | string | yes | Provider-supplied quality or confidence marker, verbatim; `null` when none |
| `provenance` | object | no | [`provenance`](#31-provenance) |

### 3.3 `reading`

A reading is one normalized snapshot: one provider, one location, one
`observed_at`, one or more values.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `provider` | string | no | Provider id |
| `location` | string | no | User-chosen label |
| `kind` | string | no | The kind shared by the reading's values |
| `observed_at` | string (time) | yes | The reading's own observation time |
| `requested_at` | string (time) | no | Fetch time |
| `age_seconds` | integer | yes | Age of `observed_at` at `generated_at` |
| `stale` | boolean | no | `true` when the reading's `age_seconds` exceeds `stale_after_seconds` |
| `stale_after_seconds` | integer | no | Threshold used |
| `fetch_id` | string | no | Raw fetch record id |
| `provenance` | object | no | The [`provenance`](#31-provenance) common to the reading; per-value provenance still wins |
| `values` | object | no | Map of variable id → [`value`](#32-value). May be `{}` when a fetch produced no usable value |

`values` is keyed by variable id (see [variables](#4-vocabulary)), so a client
reads `readings[0].values.temperature.value` without scanning an array.

### 3.4 Error envelope

Every non-2xx response, on every route, has exactly this body:

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "step must be a positive integer number of seconds",
    "status": 400,
    "detail": {
      "parameter": "step",
      "value": "-300"
    }
  }
}
```

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `error.code` | string | no | Stable machine-readable code from the [code table](#7-error-codes) |
| `error.message` | string | no | One sentence for a human. Never contains a coordinate, a secret, an API key or a stack trace |
| `error.status` | integer | no | Repeats the HTTP status code |
| `error.detail` | object | no | Structured context. `{}` when there is none. Keys vary by code and are advisory, not contractual |

An empty result is **not** an error: no data for the requested filters is `200`
with empty arrays and a `warnings` entry.

### 3.5 `warnings`

Every data route carries a top-level `warnings` array (`[]` when there is
nothing to say). Each entry:

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `code` | string | no | `no_data`, `provider_disabled`, `truncated`, `partial_window`, `store_degraded`, `due_estimated` |
| `message` | string | no | One human sentence |
| `provider` | string | yes | Provider the warning is about, when it is about one |
| `location` | string | yes | Location label the warning is about, when it is about one |

Warnings are how the dashboard renders honest empty and partial states and how
the CLI says "no data yet" while still exiting `0`.

## 4. Vocabulary

Variable ids are lower-case `snake_case` and stable. A provider that does not
supply a variable simply has no key for it in `values` — the key list is not
padded with nulls.

| Variable id | Unit id | Value type | Notes |
| --- | --- | --- | --- |
| `temperature` | `degC` | number | Air temperature |
| `apparent_temperature` | `degC` | number | Feels-like |
| `dew_point` | `degC` | number | |
| `relative_humidity` | `percent` | number | 0–100 |
| `pressure_msl` | `hPa` | number | Mean sea level |
| `pressure_surface` | `hPa` | number | Station level |
| `wind_speed` | `m_s` | number | 10 m unless the provider says otherwise |
| `wind_gust` | `m_s` | number | |
| `wind_direction` | `deg` | number | 0–360, meteorological (direction wind comes from) |
| `precipitation` | `mm` | number | Accumulation over the value's `interval_seconds` |
| `rain` | `mm` | number | |
| `precipitation_probability` | `percent` | number | Forecast values only |
| `cloud_cover` | `percent` | number | |
| `visibility` | `m` | number | |
| `shortwave_radiation` | `w_m2` | number | Global horizontal |
| `direct_radiation` | `w_m2` | number | |
| `diffuse_radiation` | `w_m2` | number | |
| `uv_index` | `index` | number | |
| `weather_code` | `code` | string | Provider's own code, as a string |
| `showers` | `mm` | number | Convective precipitation, where the provider splits it out |
| `snowfall` | `mm` | number | Snowfall depth over the interval, converted to millimetres |
| `cloud_cover_low` | `percent` | number | |
| `cloud_cover_medium` | `percent` | number | |
| `cloud_cover_high` | `percent` | number | |
| `fog` | `percent` | number | Fog area fraction |
| `uv_index_clear_sky` | `index` | number | UV index assuming no cloud |
| `direct_normal_radiation` | `w_m2` | number | Direct beam on a plane facing the sun (IMS `NIP`) |
| `wind_gust_direction` | `deg` | number | Direction of the gust (IMS `WDmax`) |
| `wind_speed_max_1min` | `m_s` | number | Highest 1-minute mean wind (IMS `WS1mm`) |
| `wind_speed_max_10min` | `m_s` | number | Highest 10-minute mean wind (IMS `Ws10mm`) |
| `wind_direction_std` | `deg` | number | Standard deviation of wind direction (IMS `STDwd`) |
| `temperature_max` | `degC` | number | Maximum over the value's interval |
| `temperature_min` | `degC` | number | Minimum over the value's interval |
| `temperature_grass_min` | `degC` | number | Minimum near the ground (IMS `TG`) |
| `relative_humidity_max` | `percent` | number | Forecast products that give a range |
| `relative_humidity_min` | `percent` | number | Forecast products that give a range |
| `is_day` | `index` | number | 1 during daylight, 0 otherwise |

**No provider value is dropped.** The table above is the shared vocabulary, not
a filter. When a provider supplies a numeric or coded variable that has no row
here, the adapter still emits it under the id `x_<provider-variable>` (the
provider's own name in lower-case `snake_case`, e.g. `x_snow_depth`), with
`original_value` / `original_unit` verbatim and `unit` set to the matching unit
id when one applies, otherwise `other`. Clients must tolerate variable ids they
do not know; the dashboard may ignore `x_` variables, the CLI lists them.

### 4.1 Units

Unit ids are ASCII, lower-case, and never rendered with symbols by the API. The
dashboard maps them to display strings.

| Unit id | Display | Meaning |
| --- | --- | --- |
| `degC` | °C | degrees Celsius |
| `percent` | % | percent |
| `hPa` | hPa | hectopascal |
| `m_s` | m/s | metres per second |
| `deg` | ° | angular degrees |
| `mm` | mm | millimetres |
| `w_m2` | W/m² | watts per square metre |
| `m` | m | metres |
| `index` | — | dimensionless index |
| `code` | — | opaque provider code |
| `other` | — | a unit outside this table; read `original_unit` |

The API normalizes to these units and preserves what the provider sent in
`original_value` / `original_unit`. `original_unit` may carry a unit id outside
this table (for example `kt` or `degF`); such a value is still converted, and the
converted `unit` is always from this table.

### 4.2 Kinds

`kind` is a required tag on every value, every series and every forecast point.

| Kind | Meaning | Examples |
| --- | --- | --- |
| `observation` | A real measurement taken by an instrument | `ims` station channels, `metar` |
| `model` | A numerical model's value for the current or nearest past time step — not measured | Open-Meteo `current` (900 s slice), MET Norway's first timeseries entry |
| `forecast` | A value for a time after its issue time | Open-Meteo hourly/daily, MET Norway later entries, `ims-forecast` |

The dashboard must render the three kinds visually distinct. A client must never
present a `model` value as a measurement.

### 4.3 Staleness

`stale` is computed per value and per reading, server-side, at response time:

```text
threshold = max_age (query parameter, when supplied)
            else reading.provenance.interval_seconds or provider default interval
                 multiplied by stale_factor (service setting, default 2)
stale     = observed_at is null  OR  age_seconds > threshold
```

The chosen threshold is always reported as `stale_after_seconds`, so a client
can explain its own verdict without knowing the provider's configuration.

A stale reading is **still returned with `200`**. Staleness is data, not an
error: the API's job is to report it truthfully. Turning staleness into a
non-zero exit is the host CLI's job (`climate weather latest --max-age` exits
`3`), not the API's.

## 5. Routes

Seven routes, all `GET`, all under `/api/v1`.

| Route | Purpose |
| --- | --- |
| [`GET /health`](#51-get-health) | Liveness, package version, newest fetch age |
| [`GET /providers`](#52-get-providers) | Capabilities, enabled state, freshness strategy, attribution |
| [`GET /locations`](#53-get-locations) | The configured location labels |
| [`GET /latest`](#54-get-latest) | The first-class query: newest reading per provider and location |
| [`GET /series`](#55-get-series) | One variable over time, with explicit nulls for gaps |
| [`GET /forecast`](#56-get-forecast) | Stored forecasts by issue and valid time |
| [`GET /stats`](#57-get-stats) | Due-versus-stored fetch counts per provider over a window |

### 5.1 GET /health

Liveness and freshness of the whole stack, for `climate doctor`, the CLI's
"is the service up" check, and the dashboard's header tile.

#### 5.1.1 Request

No parameters.

#### 5.1.2 Response

Always `200` while the process is alive, including when MongoDB is unreachable —
the `status` field, not the HTTP status, carries the verdict. A client that must
distinguish "service down" from "store down" would otherwise have to parse a
`503` body it could equally get from a reverse proxy.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `status` | string | no | `ok` (store reachable, newest fetch inside threshold), `degraded` (store reachable, nothing fresh, or a provider is failing), `down` (store unreachable) |
| `generated_at` | string (time) | no | |
| `api_version` | string | no | `v1` |
| `version` | string | no | Package version of the process serving this response (the web image's `climate` version) |
| `tracker_version` | string | yes | `climate` version recorded on the newest fetch record; `null` when no fetch exists. Differs from `version` when the images have drifted |
| `schema_version` | integer | no | Storage schema version this service reads |
| `started_at` | string (time) | no | Process start |
| `uptime_seconds` | integer | no | |
| `store.reachable` | boolean | no | |
| `store.backend` | string | no | `mongo` or `memory` |
| `store.latency_ms` | integer | yes | Round trip of the reachability probe; `null` when unreachable |
| `store.fetch_count` | integer | yes | Total stored fetch records; `null` when unreachable |
| `store.size_bytes` | integer | yes | Database size on disk as the store reports it; `null` when unreachable |
| `newest_fetch.requested_at` | string (time) | yes | Newest fetch record across all providers |
| `newest_fetch.age_seconds` | integer | yes | |
| `newest_fetch.provider` | string | yes | |
| `providers` | array | no | One entry per registered provider (see below), `[]` only if the registry is empty |
| `providers[].provider` | string | no | |
| `providers[].enabled` | boolean | no | |
| `providers[].newest_fetch_at` | string (time) | yes | |
| `providers[].newest_fetch_age_seconds` | integer | yes | |
| `providers[].newest_success_at` | string (time) | yes | Newest fetch with a 2xx status |
| `providers[].stale` | boolean | no | `newest_fetch_age_seconds` beyond that provider's threshold |
| `warnings` | array | no | [`warnings`](#35-warnings) |

#### 5.1.3 Example

```http
GET /api/v1/health
```

```json
{
  "status": "ok",
  "generated_at": "2026-09-17T08:35:12Z",
  "api_version": "v1",
  "version": "0.5.0",
  "tracker_version": "0.5.0",
  "schema_version": 1,
  "started_at": "2026-09-16T19:02:44Z",
  "uptime_seconds": 48748,
  "store": {
    "reachable": true,
    "backend": "mongo",
    "latency_ms": 3,
    "fetch_count": 18422,
    "size_bytes": 742391808
  },
  "newest_fetch": {
    "requested_at": "2026-09-17T08:35:02Z",
    "age_seconds": 10,
    "provider": "open-meteo"
  },
  "providers": [
    {
      "provider": "open-meteo",
      "enabled": true,
      "newest_fetch_at": "2026-09-17T08:35:02Z",
      "newest_fetch_age_seconds": 10,
      "newest_success_at": "2026-09-17T08:35:02Z",
      "stale": false
    },
    {
      "provider": "met-no",
      "enabled": true,
      "newest_fetch_at": "2026-09-17T08:20:03Z",
      "newest_fetch_age_seconds": 909,
      "newest_success_at": "2026-09-17T08:20:03Z",
      "stale": false
    },
    {
      "provider": "ims",
      "enabled": false,
      "newest_fetch_at": null,
      "newest_fetch_age_seconds": null,
      "newest_success_at": null,
      "stale": false
    }
  ],
  "warnings": [
    {
      "code": "provider_disabled",
      "message": "ims is disabled: CLIMATE_IMS_API_TOKEN is not set.",
      "provider": "ims",
      "location": null
    }
  ]
}
```

### 5.2 GET /providers

Provider registry metadata plus live state. The dashboard footer's attribution
comes from here, so `attribution.text` and `attribution.url` are required for
every provider.

#### 5.2.1 Request

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | string, repeatable | all | Restrict to these provider ids |
| `enabled` | boolean (`true`/`false`) | unset | Restrict to enabled or disabled providers |

#### 5.2.2 Response

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | |
| `providers` | array | no | |
| `providers[].provider` | string | no | Registry id |
| `providers[].title` | string | no | Short human name, for chart legends |
| `providers[].kind` | string | no | The kind this provider mainly yields: `observation`, `model` or `forecast` |
| `providers[].enabled` | boolean | no | Effective enabled state |
| `providers[].enabled_reason` | string | yes | Why it is disabled; `null` when enabled |
| `providers[].auth_required` | boolean | no | |
| `providers[].credential_present` | boolean | no | Whether the credential is set. Never the credential itself |
| `providers[].capabilities.variables` | array of string | no | Variable ids this provider can supply |
| `providers[].capabilities.kinds` | array of string | no | Kinds it produces |
| `providers[].capabilities.forecast_horizon_hours` | integer | yes | `null` when it has no forecast |
| `providers[].capabilities.locations` | array of string | no | Location labels it is configured for |
| `providers[].freshness.strategy` | string | no | `interval`, `http-expires` or `station-cadence` |
| `providers[].freshness.interval_seconds` | integer | yes | Configured poll interval; `null` for pure `http-expires` |
| `providers[].freshness.expected_update_seconds` | integer | yes | How often the upstream data itself actually changes |
| `providers[].freshness.notes` | string | yes | One sentence, e.g. the mandatory identifying User-Agent |
| `providers[].quota.calls_per_day` | integer | yes | |
| `providers[].quota.calls_per_minute` | integer | yes | |
| `providers[].quota.weight_per_call` | number | yes | Provider's own call weighting, when it has one |
| `providers[].quota.source` | string | no | `provider-docs`, `issue-5` or `third-party` — how the figure was obtained |
| `providers[].attribution.text` | string | no | Exact credit line the dashboard footer must render |
| `providers[].attribution.url` | string | no | Licence or terms URL |
| `providers[].attribution.licence` | string | yes | Licence identifier, e.g. `CC-BY-4.0` |
| `providers[].state.newest_fetch_at` | string (time) | yes | |
| `providers[].state.newest_success_at` | string (time) | yes | |
| `providers[].state.last_status` | integer | yes | HTTP status of the newest fetch; `null` when there has been none |
| `warnings` | array | no | |

#### 5.2.3 Example

```http
GET /api/v1/providers?enabled=true
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "providers": [
    {
      "provider": "open-meteo",
      "title": "Open-Meteo",
      "kind": "model",
      "enabled": true,
      "enabled_reason": null,
      "auth_required": false,
      "credential_present": true,
      "capabilities": {
        "variables": [
          "temperature",
          "apparent_temperature",
          "dew_point",
          "relative_humidity",
          "pressure_msl",
          "wind_speed",
          "wind_gust",
          "wind_direction",
          "precipitation",
          "cloud_cover",
          "shortwave_radiation",
          "direct_radiation",
          "diffuse_radiation",
          "uv_index",
          "weather_code"
        ],
        "kinds": ["model", "forecast"],
        "forecast_horizon_hours": 48,
        "locations": ["home", "office"]
      },
      "freshness": {
        "strategy": "interval",
        "interval_seconds": 900,
        "expected_update_seconds": 900,
        "notes": "The current block is a 900-second model slice; polling faster returns the same values."
      },
      "quota": {
        "calls_per_day": 10000,
        "calls_per_minute": null,
        "weight_per_call": 5.0,
        "source": "third-party"
      },
      "attribution": {
        "text": "Weather data by Open-Meteo.com (CC BY 4.0)",
        "url": "https://open-meteo.com/en/license",
        "licence": "CC-BY-4.0"
      },
      "state": {
        "newest_fetch_at": "2026-09-17T08:35:02Z",
        "newest_success_at": "2026-09-17T08:35:02Z",
        "last_status": 200
      }
    }
  ],
  "warnings": []
}
```

### 5.3 GET /locations

The label list, so the dashboard can build its selector and the CLI can validate
`--location` without guessing.

#### 5.3.1 Request

No parameters.

#### 5.3.2 Response

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | |
| `locations` | array | no | |
| `locations[].location` | string | no | The user-chosen label. The only identifier a location ever has here |
| `locations[].providers` | array of string | no | Provider ids configured for this label |
| `locations[].newest_observed_at` | string (time) | yes | Newest `observed_at` across its readings |
| `locations[].reading_count` | integer | no | Stored normalized readings for this label |
| `warnings` | array | no | |

No coordinate, no precision, no place name, no timezone is returned — a
timezone would narrow the location down.

#### 5.3.3 Example

```http
GET /api/v1/locations
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "locations": [
    {
      "location": "home",
      "providers": ["open-meteo", "met-no", "metar"],
      "newest_observed_at": "2026-09-17T08:30:00Z",
      "reading_count": 9211
    },
    {
      "location": "office",
      "providers": ["open-meteo", "met-no"],
      "newest_observed_at": "2026-09-17T08:30:00Z",
      "reading_count": 6104
    }
  ],
  "warnings": []
}
```

### 5.4 GET /latest

**The first-class query.** One newest reading per (provider, location) pair,
with every value fully annotated. This is what the AC-control agent reads.

#### 5.4.1 Request

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `location` | string, repeatable | all configured labels | Restrict to these labels |
| `provider` | string, repeatable | all enabled providers | Restrict to these provider ids |
| `variables` | comma-separated variable ids | all | Restrict the `values` map. A vocabulary id or an `x_<provider-field>` extension id; anything else is rejected with `unknown_variable` |
| `kind` | string, repeatable (`observation`, `model`) | `observation,model` | Which kinds count as "latest". `forecast` is rejected here — use `/forecast` |
| `max_age` | integer seconds, `> 0` | unset | Overrides the staleness threshold for every returned value and reading |

Repeatable parameters may also be given comma-separated:
`?provider=met-no,open-meteo` equals `?provider=met-no&provider=open-meteo`.

A requested (provider, location) pair with no stored reading is **not** an
error: it appears in `missing` and the response is still `200`.

#### 5.4.2 Response

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | The instant every `age_seconds` was computed against |
| `stale` | boolean | no | `true` when **any** returned reading is stale. The CLI's exit-3 signal |
| `max_age_seconds` | integer | yes | Echo of `max_age`; `null` when not supplied |
| `readings` | array of [`reading`](#33-reading) | no | Newest per (provider, location), sorted by `provider` then `location` |
| `missing` | array | no | Requested pairs with nothing to return |
| `missing[].provider` | string | no | |
| `missing[].location` | string | no | |
| `missing[].reason` | string | no | `no_data`, `provider_disabled`, `no_fresh_reading` |
| `warnings` | array | no | |

#### 5.4.3 Example

```http
GET /api/v1/latest?location=home&provider=open-meteo&provider=metar&max_age=1200
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "stale": false,
  "max_age_seconds": 1200,
  "readings": [
    {
      "provider": "metar",
      "location": "home",
      "kind": "observation",
      "observed_at": "2026-09-17T08:20:00Z",
      "requested_at": "2026-09-17T08:30:04Z",
      "age_seconds": 912,
      "stale": false,
      "stale_after_seconds": 1200,
      "fetch_id": "f_01J9Q2W7K3YB4E",
      "provenance": {
        "provider": "metar",
        "source": "metar/nearest-station",
        "model": null,
        "model_run_at": null,
        "station": "STN-1",
        "interval_seconds": 3600,
        "fetch_id": "f_01J9Q2W7K3YB4E",
        "schema_version": 1
      },
      "values": {
        "temperature": {
          "value": 26.0,
          "unit": "degC",
          "original_value": 26,
          "original_unit": "degC",
          "observed_at": "2026-09-17T08:20:00Z",
          "requested_at": "2026-09-17T08:30:04Z",
          "age_seconds": 912,
          "kind": "observation",
          "stale": false,
          "stale_after_seconds": 1200,
          "quality": null,
          "provenance": {
            "provider": "metar",
            "source": "metar/nearest-station",
            "model": null,
            "model_run_at": null,
            "station": "STN-1",
            "interval_seconds": 3600,
            "fetch_id": "f_01J9Q2W7K3YB4E",
            "schema_version": 1
          }
        },
        "wind_speed": {
          "value": 4.63,
          "unit": "m_s",
          "original_value": 9,
          "original_unit": "kt",
          "observed_at": "2026-09-17T08:20:00Z",
          "requested_at": "2026-09-17T08:30:04Z",
          "age_seconds": 912,
          "kind": "observation",
          "stale": false,
          "stale_after_seconds": 1200,
          "quality": null,
          "provenance": {
            "provider": "metar",
            "source": "metar/nearest-station",
            "model": null,
            "model_run_at": null,
            "station": "STN-1",
            "interval_seconds": 3600,
            "fetch_id": "f_01J9Q2W7K3YB4E",
            "schema_version": 1
          }
        }
      }
    },
    {
      "provider": "open-meteo",
      "location": "home",
      "kind": "model",
      "observed_at": "2026-09-17T08:30:00Z",
      "requested_at": "2026-09-17T08:35:02Z",
      "age_seconds": 312,
      "stale": false,
      "stale_after_seconds": 1200,
      "fetch_id": "f_01J9Q2X1M8ZC7A",
      "provenance": {
        "provider": "open-meteo",
        "source": "v1/forecast",
        "model": "best_match",
        "model_run_at": "2026-09-17T06:00:00Z",
        "station": null,
        "interval_seconds": 900,
        "fetch_id": "f_01J9Q2X1M8ZC7A",
        "schema_version": 1
      },
      "values": {
        "temperature": {
          "value": 25.8,
          "unit": "degC",
          "original_value": 25.8,
          "original_unit": "degC",
          "observed_at": "2026-09-17T08:30:00Z",
          "requested_at": "2026-09-17T08:35:02Z",
          "age_seconds": 312,
          "kind": "model",
          "stale": false,
          "stale_after_seconds": 1200,
          "quality": null,
          "provenance": {
            "provider": "open-meteo",
            "source": "v1/forecast",
            "model": "best_match",
            "model_run_at": "2026-09-17T06:00:00Z",
            "station": null,
            "interval_seconds": 900,
            "fetch_id": "f_01J9Q2X1M8ZC7A",
            "schema_version": 1
          }
        },
        "relative_humidity": {
          "value": 63.0,
          "unit": "percent",
          "original_value": 63,
          "original_unit": "percent",
          "observed_at": "2026-09-17T08:30:00Z",
          "requested_at": "2026-09-17T08:35:02Z",
          "age_seconds": 312,
          "kind": "model",
          "stale": false,
          "stale_after_seconds": 1200,
          "quality": null,
          "provenance": {
            "provider": "open-meteo",
            "source": "v1/forecast",
            "model": "best_match",
            "model_run_at": "2026-09-17T06:00:00Z",
            "station": null,
            "interval_seconds": 900,
            "fetch_id": "f_01J9Q2X1M8ZC7A",
            "schema_version": 1
          }
        },
        "shortwave_radiation": {
          "value": 612.0,
          "unit": "w_m2",
          "original_value": 612,
          "original_unit": "w_m2",
          "observed_at": "2026-09-17T08:30:00Z",
          "requested_at": "2026-09-17T08:35:02Z",
          "age_seconds": 312,
          "kind": "model",
          "stale": false,
          "stale_after_seconds": 1200,
          "quality": null,
          "provenance": {
            "provider": "open-meteo",
            "source": "v1/forecast",
            "model": "best_match",
            "model_run_at": "2026-09-17T06:00:00Z",
            "station": null,
            "interval_seconds": 900,
            "fetch_id": "f_01J9Q2X1M8ZC7A",
            "schema_version": 1
          }
        }
      }
    }
  ],
  "missing": [],
  "warnings": []
}
```

### 5.5 GET /series

One variable over a time window, on a fixed grid, with **explicit nulls for
missed ticks**. This is the dashboard's line-chart feed.

#### 5.5.1 Request

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `variable` | string | — | **Required.** Exactly one variable id, or one `x_<provider-field>` extension id |
| `location` | string, repeatable | all | |
| `provider` | string, repeatable | all enabled | |
| `from` | timestamp | `to` minus 24 h | Inclusive lower bound |
| `to` | timestamp | `generated_at` | Exclusive upper bound |
| `kind` | string, repeatable | `observation,model` | Which kinds to include. `forecast` is allowed here and yields the forecast values as they were stored, tagged `forecast` |
| `step` | integer seconds | `300` | Grid resolution. Must be `>= 60` and a divisor-friendly value; the grid is aligned to Unix-epoch multiples of `step` |
| `agg` | string | `last` | How several values inside one bucket collapse: `last`, `first`, `mean`, `min`, `max` |
| `max_points` | integer | `2000` | Per series; hard ceiling `10000` |

`from` must be earlier than `to`; otherwise `invalid_parameter`. `to - from`
must not exceed the span cap in [section 2.7](#27-limits); otherwise
`invalid_parameter`.

When the window names more than `max_points` buckets, the **earliest**
`max_points` are returned, the response's `to` reports the narrowed end, and a
`truncated` warning names the requested bucket count and the limit that applied.

#### 5.5.2 Response

One entry in `series` per (provider, location, kind) combination that has data.
Every entry's `points` array is the **same length** and covers the **same grid**
as every other entry's, so a chart can share one x-axis.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | |
| `variable` | string | no | Echo |
| `unit` | string | no | Canonical unit for the whole response. For an `x_` extension the table assigns none, so the stored unit is reported (or `other` when the stored units disagree or nothing matched) |
| `from` | string (time) | no | First grid instant |
| `to` | string (time) | no | Exclusive end **of what was returned** — earlier than the requested `to` when the result was truncated |
| `step_seconds` | integer | no | |
| `agg` | string | no | Echo |
| `point_count` | integer | no | Length of every `points` array |
| `series` | array | no | `[]` when nothing matched |
| `series[].provider` | string | no | |
| `series[].location` | string | no | |
| `series[].kind` | string | no | `observation`, `model` or `forecast` |
| `series[].unit` | string | no | Repeats the top-level unit |
| `series[].source` | string | yes | Provenance `source` when uniform across the series, else `null` |
| `series[].model` | string | yes | Provenance `model` when uniform, else `null` |
| `series[].station` | string | yes | Provenance `station` when uniform, else `null` |
| `series[].value_count` | integer | no | Points with a non-null value |
| `series[].null_count` | integer | no | Points with `value: null` |
| `series[].first_at` | string (time) | yes | First non-null point's `t`; `null` when all null |
| `series[].last_at` | string (time) | yes | Last non-null point's `t`; `null` when all null |
| `series[].min` | number | yes | Over non-null values; `null` when all null |
| `series[].max` | number | yes | |
| `series[].points` | array | no | Grid points, ascending by `t` |
| `series[].points[].t` | string (time) | no | Grid instant (bucket start) — always present, even for a gap |
| `series[].points[].value` | number \| string | yes | `null` for a missed tick. **Never interpolated** |
| `series[].points[].observed_at` | string (time) | yes | The contributing value's own `observed_at`; `null` for a gap |
| `series[].points[].fetch_id` | string | yes | `null` for a gap, and for aggregated buckets where `agg` is not `first`/`last` |
| `warnings` | array | no | |

A `null` point means *no fetch was stored for that bucket* — a missed tick, a
stopped stack, a provider outage. Clients must break the line at nulls; drawing
through them would invent data. A provider whose own interval is longer than
`step` legitimately produces mostly-null series: choose `step` to match the
provider, or accept the sparse grid and render points rather than a line.

#### 5.5.3 Example

```http
GET /api/v1/series?variable=temperature&location=home&provider=open-meteo&step=900&from=2026-09-17T07:00:00Z&to=2026-09-17T08:30:00Z
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "variable": "temperature",
  "unit": "degC",
  "from": "2026-09-17T07:00:00Z",
  "to": "2026-09-17T08:30:00Z",
  "step_seconds": 900,
  "agg": "last",
  "point_count": 6,
  "series": [
    {
      "provider": "open-meteo",
      "location": "home",
      "kind": "model",
      "unit": "degC",
      "source": "v1/forecast",
      "model": "best_match",
      "station": null,
      "value_count": 5,
      "null_count": 1,
      "first_at": "2026-09-17T07:00:00Z",
      "last_at": "2026-09-17T08:15:00Z",
      "min": 23.9,
      "max": 25.4,
      "points": [
        {
          "t": "2026-09-17T07:00:00Z",
          "value": 23.9,
          "observed_at": "2026-09-17T07:00:00Z",
          "fetch_id": "f_01J9Q1A0AA0001"
        },
        {
          "t": "2026-09-17T07:15:00Z",
          "value": 24.2,
          "observed_at": "2026-09-17T07:15:00Z",
          "fetch_id": "f_01J9Q1A0AA0002"
        },
        {
          "t": "2026-09-17T07:30:00Z",
          "value": null,
          "observed_at": null,
          "fetch_id": null
        },
        {
          "t": "2026-09-17T07:45:00Z",
          "value": 24.8,
          "observed_at": "2026-09-17T07:45:00Z",
          "fetch_id": "f_01J9Q1A0AA0004"
        },
        {
          "t": "2026-09-17T08:00:00Z",
          "value": 25.1,
          "observed_at": "2026-09-17T08:00:00Z",
          "fetch_id": "f_01J9Q1A0AA0005"
        },
        {
          "t": "2026-09-17T08:15:00Z",
          "value": 25.4,
          "observed_at": "2026-09-17T08:15:00Z",
          "fetch_id": "f_01J9Q1A0AA0006"
        }
      ]
    }
  ],
  "warnings": [
    {
      "code": "no_data",
      "message": "No stored fetch covers 2026-09-17T07:30:00Z for open-meteo at home.",
      "provider": "open-meteo",
      "location": "home"
    }
  ]
}
```

### 5.6 GET /forecast

Stored forecasts, addressed by issue time and valid time. Forecast values are
kept even though nothing consumed them at first delivery, so this route exists
from day one and may legitimately return empty arrays for providers that issue
none.

#### 5.6.1 Request

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `location` | string, repeatable | all | |
| `provider` | string, repeatable | all enabled with a forecast | |
| `variables` | comma-separated variable ids | all vocabulary ids | An `x_` extension has no enumerable id, so it is returned when named explicitly |
| `issued_at` | timestamp | unset | Return the newest forecast issued **at or before** this instant. Unset means the newest issue available |
| `horizon_hours` | integer | `48` | Valid times from `generated_at` onwards. Maximum `168` |
| `step_hours` | integer | `1` | Grid resolution of `points` |

**Issue time and horizon.** `issued_at` is the provider's own model issue /
run time, taken from the stored reading's `model_run_at`. When the provider
states none, the time the tracker fetched the response stands in for it and
the entry says so with `issued_at_estimated: true` — the two are never
silently conflated.

The horizon is measured from `generated_at`, **not** from `issued_at`: a
forecast fetched hours ago still carries valid future points, and measuring
from its own issue time discarded them. Points whose `valid_at` is already in
the past are not a forecast and are excluded.

#### 5.6.2 Response

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | |
| `horizon_hours` | integer | no | Echo |
| `step_hours` | integer | no | Echo |
| `forecasts` | array | no | One entry per (provider, location, issue) |
| `forecasts[].provider` | string | no | |
| `forecasts[].location` | string | no | |
| `forecasts[].kind` | string | no | Always `forecast` |
| `forecasts[].issued_at` | string (time) | no | Provider's issue / model-run time, or the fetch time when the provider states none |
| `forecasts[].issued_at_estimated` | boolean | no | `true` when `issued_at` is the fetch time standing in for an issue time the provider did not give |
| `forecasts[].requested_at` | string (time) | no | When the tracker fetched it |
| `forecasts[].fetch_id` | string | no | |
| `forecasts[].provenance` | object | no | [`provenance`](#31-provenance). Its `model_run_at` is the provider's stated run time, `null` when there is none — it is never the fetch time |
| `forecasts[].variables` | array of string | no | Variable ids present in `points` |
| `forecasts[].units` | object | no | Map of variable id → unit id. An `x_` extension reports its stored unit |
| `forecasts[].point_count` | integer | no | |
| `forecasts[].points` | array | no | Ascending by `valid_at` |
| `forecasts[].points[].valid_at` | string (time) | no | The instant the forecast is for |
| `forecasts[].points[].lead_seconds` | integer | no | `valid_at - issued_at`; always `>= 0` |
| `forecasts[].points[].values` | object | no | Map of variable id → number \| string \| `null`. `null` for a variable the issue does not cover at that step |
| `warnings` | array | no | |

Forecast points carry bare numbers, not full `value` objects: unit, provenance,
`kind` and issue time are constant for the whole forecast and are stated once on
the parent. This keeps a 48-hour multi-variable forecast small enough for the
dashboard to fetch on every refresh.

#### 5.6.3 Example

```http
GET /api/v1/forecast?location=office&provider=met-no&variables=temperature,precipitation&horizon_hours=3
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "horizon_hours": 3,
  "step_hours": 1,
  "forecasts": [
    {
      "provider": "met-no",
      "location": "office",
      "kind": "forecast",
      "issued_at": "2026-09-17T06:00:00Z",
      "issued_at_estimated": false,
      "requested_at": "2026-09-17T08:20:03Z",
      "fetch_id": "f_01J9Q2P4R7T0QA",
      "provenance": {
        "provider": "met-no",
        "source": "locationforecast/2.0/complete",
        "model": "MEPS",
        "model_run_at": "2026-09-17T06:00:00Z",
        "station": null,
        "interval_seconds": 3600,
        "fetch_id": "f_01J9Q2P4R7T0QA",
        "schema_version": 1
      },
      "variables": ["temperature", "precipitation"],
      "units": {
        "temperature": "degC",
        "precipitation": "mm"
      },
      "point_count": 3,
      "points": [
        {
          "valid_at": "2026-09-17T09:00:00Z",
          "lead_seconds": 10800,
          "values": {
            "temperature": 26.3,
            "precipitation": 0.0
          }
        },
        {
          "valid_at": "2026-09-17T10:00:00Z",
          "lead_seconds": 14400,
          "values": {
            "temperature": 27.1,
            "precipitation": 0.0
          }
        },
        {
          "valid_at": "2026-09-17T11:00:00Z",
          "lead_seconds": 18000,
          "values": {
            "temperature": 27.8,
            "precipitation": null
          }
        }
      ]
    }
  ],
  "warnings": []
}
```

### 5.7 GET /stats

Collection health: **due versus stored fetch counts per provider over a
window**, computed from the stored fetch records themselves, never estimated
from the schedule alone.

#### 5.7.1 Request

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `window` | duration string | `24h` | Integer plus one of `s`, `m`, `h`, `d` — for example `90m`, `24h`, `7d`. Window ends at `generated_at` |
| `from` | timestamp | unset | Explicit window start. When given with `to`, overrides `window` |
| `to` | timestamp | `generated_at` | Explicit window end |
| `provider` | string, repeatable | all registered | |
| `location` | string, repeatable | all | |
| `bucket` | integer seconds | unset | When given, adds a `buckets` array per provider for a collection-health chart. Must be `>= 300`, and the window divided by `bucket` must not exceed `1000` |

`from` must be earlier than `to`, and the window — whether it came from
`window` or from `from`/`to` — must not exceed the span cap in
[section 2.7](#27-limits); otherwise `invalid_parameter`, with `span_seconds`
and `max_span_seconds` in `detail`.

Within an accepted window, `stored_count` and `completeness` come from a count
query, and at most `20000` fetch records per row are read for the remaining
figures; a row that hits that cap carries a `truncated` warning naming it (see
[section 2.7](#27-limits)).

#### 5.7.2 Response

`due_count` is how many fetches *should* have happened in the window for that
provider and location:

- `interval` strategy — `floor(window_seconds / interval_seconds)`, exact.
- `http-expires` strategy — counted from the `Expires` / `Last-Modified` headers
  recorded on the stored fetch records, so it is exact only where records exist;
  where the window predates the first record it is extrapolated from the
  provider's `expected_update_seconds` and `due_estimated` is `true`.
- `station-cadence` strategy — from the provider's declared station cadence;
  `due_estimated` is `true`.

`completeness` is `stored_count / due_count`, rounded to four decimals, and is
`null` when `due_count` is `0`. This is the figure the 95 %-of-due-fetches
success signal is read from.

| Field | Type | Null? | Meaning |
| --- | --- | --- | --- |
| `generated_at` | string (time) | no | |
| `from` | string (time) | no | Window start |
| `to` | string (time) | no | Window end |
| `window_seconds` | integer | no | |
| `bucket_seconds` | integer | yes | Echo of `bucket`; `null` when not requested |
| `totals.due_count` | integer | no | Sum over all rows |
| `totals.stored_count` | integer | no | |
| `totals.completeness` | number | yes | |
| `totals.bytes_stored` | integer | no | Raw response bytes stored in the window |
| `providers` | array | no | One row per (provider, location) |
| `providers[].provider` | string | no | |
| `providers[].location` | string | yes | `null` for a provider whose fetches are not per-location |
| `providers[].enabled` | boolean | no | |
| `providers[].freshness_strategy` | string | no | `interval`, `http-expires`, `station-cadence` |
| `providers[].interval_seconds` | integer | yes | Effective interval used for `due_count` |
| `providers[].due_count` | integer | no | |
| `providers[].due_estimated` | boolean | no | `true` when `due_count` could not be derived exactly |
| `providers[].stored_count` | integer | no | Fetch records stored in the window, successes and failures alike |
| `providers[].completeness` | number | yes | |
| `providers[].ok_count` | integer | no | HTTP 2xx |
| `providers[].not_modified_count` | integer | no | HTTP 304, stored as a fetch record |
| `providers[].client_error_count` | integer | no | HTTP 4xx other than 429 |
| `providers[].rate_limited_count` | integer | no | HTTP 429 |
| `providers[].server_error_count` | integer | no | HTTP 5xx |
| `providers[].transport_error_count` | integer | no | Timeouts, DNS and connection failures stored as records |
| `providers[].reading_count` | integer | no | Normalized readings derived in the window |
| `providers[].bytes_stored` | integer | no | |
| `providers[].first_fetch_at` | string (time) | yes | First stored fetch in the window |
| `providers[].newest_fetch_at` | string (time) | yes | |
| `providers[].newest_fetch_age_seconds` | integer | yes | |
| `providers[].mean_interval_seconds` | number | yes | Mean gap between consecutive stored fetches; `null` with fewer than two |
| `providers[].longest_gap_seconds` | integer | yes | Longest gap between consecutive stored fetches; `null` with fewer than two |
| `providers[].buckets` | array | yes | `null` unless `bucket` was given |
| `providers[].buckets[].t` | string (time) | no | Bucket start |
| `providers[].buckets[].due_count` | integer | no | |
| `providers[].buckets[].stored_count` | integer | no | |
| `providers[].buckets[].ok_count` | integer | no | |
| `warnings` | array | no | |

#### 5.7.3 Example

```http
GET /api/v1/stats?window=24h&location=home
```

```json
{
  "generated_at": "2026-09-17T08:35:12Z",
  "from": "2026-09-16T08:35:12Z",
  "to": "2026-09-17T08:35:12Z",
  "window_seconds": 86400,
  "bucket_seconds": null,
  "totals": {
    "due_count": 168,
    "stored_count": 164,
    "completeness": 0.9762,
    "bytes_stored": 8216344
  },
  "providers": [
    {
      "provider": "open-meteo",
      "location": "home",
      "enabled": true,
      "freshness_strategy": "interval",
      "interval_seconds": 900,
      "due_count": 96,
      "due_estimated": false,
      "stored_count": 94,
      "completeness": 0.9792,
      "ok_count": 93,
      "not_modified_count": 0,
      "client_error_count": 0,
      "rate_limited_count": 0,
      "server_error_count": 1,
      "transport_error_count": 0,
      "reading_count": 93,
      "bytes_stored": 3744772,
      "first_fetch_at": "2026-09-16T08:45:02Z",
      "newest_fetch_at": "2026-09-17T08:35:02Z",
      "newest_fetch_age_seconds": 10,
      "mean_interval_seconds": 918.4,
      "longest_gap_seconds": 2702,
      "buckets": null
    },
    {
      "provider": "met-no",
      "location": "home",
      "enabled": true,
      "freshness_strategy": "http-expires",
      "interval_seconds": 1800,
      "due_count": 48,
      "due_estimated": true,
      "stored_count": 47,
      "completeness": 0.9792,
      "ok_count": 31,
      "not_modified_count": 16,
      "client_error_count": 0,
      "rate_limited_count": 0,
      "server_error_count": 0,
      "transport_error_count": 0,
      "reading_count": 31,
      "bytes_stored": 4471572,
      "first_fetch_at": "2026-09-16T08:50:03Z",
      "newest_fetch_at": "2026-09-17T08:20:03Z",
      "newest_fetch_age_seconds": 909,
      "mean_interval_seconds": 1841.0,
      "longest_gap_seconds": 3644,
      "buckets": null
    },
    {
      "provider": "ims",
      "location": "home",
      "enabled": false,
      "freshness_strategy": "station-cadence",
      "interval_seconds": null,
      "due_count": 0,
      "due_estimated": true,
      "stored_count": 0,
      "completeness": null,
      "ok_count": 0,
      "not_modified_count": 0,
      "client_error_count": 0,
      "rate_limited_count": 0,
      "server_error_count": 0,
      "transport_error_count": 0,
      "reading_count": 0,
      "bytes_stored": 0,
      "first_fetch_at": null,
      "newest_fetch_at": null,
      "newest_fetch_age_seconds": null,
      "mean_interval_seconds": null,
      "longest_gap_seconds": null,
      "buckets": null
    }
  ],
  "warnings": [
    {
      "code": "due_estimated",
      "message": "met-no due counts are derived from recorded Expires headers and are approximate.",
      "provider": "met-no",
      "location": "home"
    },
    {
      "code": "provider_disabled",
      "message": "ims is disabled: CLIMATE_IMS_API_TOKEN is not set.",
      "provider": "ims",
      "location": "home"
    }
  ]
}
```

## 6. Status codes

| Status | When |
| --- | --- |
| `200` | Success, including an empty result and including stale data |
| `400` | A query parameter is missing, malformed, out of range, or names an unknown provider, location, variable or kind |
| `404` | Unknown route or missing static file |
| `405` | Any method other than `GET` or `HEAD`; carries `Allow: GET, HEAD` |
| `500` | Unexpected server fault. Message is generic; details go to the service log, never to the client |
| `503` | The store is unreachable, on a **data** route (`/latest`, `/series`, `/forecast`, `/stats`, `/locations`). `/health` and `/providers` still answer `200` so a client can diagnose |

## 7. Error codes

| `error.code` | Status | Meaning |
| --- | --- | --- |
| `invalid_parameter` | 400 | Wrong type, bad format, out of range, or `from` not before `to` |
| `missing_parameter` | 400 | A required parameter (`series.variable`) was not supplied |
| `unknown_provider` | 400 | `provider` names an id not in the registry |
| `unknown_location` | 400 | `location` names a label that is not configured |
| `unknown_variable` | 400 | `variable` / `variables` names an id outside the vocabulary |
| `unknown_kind` | 400 | `kind` is not `observation`, `model` or `forecast`, or is `forecast` on `/latest` |
| `not_found` | 404 | Unknown route or static file |
| `method_not_allowed` | 405 | Non-`GET`/`HEAD` method — the API is read-only |
| `internal_error` | 500 | Unexpected fault |
| `store_unavailable` | 503 | MongoDB is unreachable |

Example of a `405`, which is also the shape a write attempt gets:

```http
POST /api/v1/latest
```

```json
{
  "error": {
    "code": "method_not_allowed",
    "message": "The weather API is read-only; only GET and HEAD are supported.",
    "status": 405,
    "detail": {
      "method": "POST",
      "allow": ["GET", "HEAD"]
    }
  }
}
```

## 8. Client obligations

### 8.1 Dashboard

- Fetches only the routes above, same-origin, relative (`/api/v1/...`).
- Renders `observation`, `model` and `forecast` visually distinct; never labels
  a `model` value a measurement.
- Breaks lines at `null` points; never interpolates across a gap.
- Renders `warnings` as explicit empty or partial states (first hour of
  collection, disabled provider, truncated window).
- Renders `attribution.text` linked to `attribution.url` for every provider
  whose data is on screen, from `GET /providers` — the footer's only source.
- Converts UTC to local time for display only. It never sends a local time back.

### 8.2 Host CLI

| Verb | Route |
| --- | --- |
| `climate weather latest` | `GET /latest` (`--provider`, `--location`, `--max-age`) |
| `climate weather series` | `GET /series` |
| `climate weather forecast` | `GET /forecast` |
| `climate weather stats` | `GET /stats` |
| `climate providers` | the local registry, with `GET /providers` only when it needs live state |
| `climate doctor` | `GET /health` and `GET /stats` |

Exit-code mapping, which the API does not itself express:

| Situation | API result | CLI exit |
| --- | --- | --- |
| Data returned, fresh | `200`, `stale: false` | `0` |
| No data yet | `200`, empty arrays, `no_data` warning | `0` |
| `--max-age` exceeded | `200`, `stale: true` | `3` (dedicated stale code) |
| Connection refused, timeout, `503` | no response or `503` | `2`, with a hint to run `climate stack status` |
| `400` from a bad flag value | `400` | `1` |

## 9. Deliberate non-features

- **No write routes, no auth routes, no admin routes.** Lifecycle is
  `climate stack up|down|status`, not HTTP.
- **No coordinate, geometry or timezone field**, anywhere — each would leak the
  location the labels exist to hide.
- **No raw-fetch download route.** `fetch_id` is an opaque correlation handle;
  serving stored provider bytes verbatim would re-publish third-party data whose
  licence terms have only been read in summary.
- **No pagination, no cursors.** Bounded windows and `max_points` instead.
- **No server-sent events or websockets.** The dashboard polls; the data changes
  every five minutes at best.

## 10. Open points

These are known gaps in the contract, recorded rather than guessed:

- The AC-control agent does not exist yet, so its required variable set, unit
  preferences and maximum acceptable age are unverified. `/latest` is
  deliberately provider-complete so it can adapt without a `v2`.
- `quota` figures for OpenWeather and Open-Meteo come from issue 5 and
  third-party pages, which is why every quota row carries `source`. They are
  provider metadata, re-verified when the adapters are written, not contract.
- Whether `ims` reports one `station` per value or one per reading depends on
  the station and channel ids that only a token reveals; the per-value
  `provenance.station` field is sized for the worse case.
