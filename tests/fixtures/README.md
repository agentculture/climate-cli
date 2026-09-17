# Weather provider fixtures

Captured or synthesized for the `weather-tracking-service` build (task t1).
Every fixture uses a neutral public reference point unrelated to the user —
the Royal Observatory Greenwich, `lat 51.4769 lon -0.0005` — for the
coordinate-based providers. No fixture, and no code outside this directory,
carries the user's own location.

Nothing here is a credential: no fixture contains a real API key or token.

## Captured live (one curl call per provider, 2026-09-17)

These four providers are keyless, so their fixtures are the verbatim,
unmodified bytes of one real response.

- **`open_meteo_forecast.json`** — Open-Meteo forecast API, keyless.

  ```bash
  curl "https://api.open-meteo.com/v1/forecast?latitude=51.4769&longitude=-0.0005\
  &current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,\
  rain,showers,snowfall,weather_code,cloud_cover,pressure_msl,surface_pressure,\
  wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index,shortwave_radiation,\
  direct_radiation,diffuse_radiation,dew_point_2m\
  &minutely_15=temperature_2m,precipitation\
  &hourly=temperature_2m,precipitation_probability,precipitation,relative_humidity_2m\
  &daily=temperature_2m_max,temperature_2m_min,precipitation_sum,uv_index_max\
  &forecast_hours=48&timezone=UTC"
  ```

  `forecast_hours=48` is the 48-hour horizon requested per the plan's hard
  question resolution. Response is the raw JSON body, byte-for-byte.

- **`met_no_locationforecast.json`** + **`met_no_locationforecast_headers.txt`**
  — MET Norway Locationforecast 2.0 compact, keyless but requires an
  identifying `User-Agent`.

  ```bash
  curl -H "User-Agent: climate-cli/0.4 github.com/agentculture/climate-cli" \
    "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51.4769&lon=-0.0005" \
    -D met_no_locationforecast_headers.txt
  ```

  Coordinates are sent with 4 decimal places, the maximum MET Norway's terms
  allow. The headers file is the exact response headers captured with
  `curl -D`, including `Expires` and `Last-Modified`, needed for t11's
  Expires-driven refresh logic and conditional-GET tests. Body is the raw
  JSON, byte-for-byte.

- **`metar_llbg.json`** — aviationweather.gov METAR API, keyless, station
  LLBG (Tel Aviv/Ben Gurion Airport) as named explicitly by this task's own
  acceptance criteria and already public in the exported spec's scope
  exploration (s12). This is a real published aviation observation for a
  public airport, not the user's private location; the raw ICAO METAR text
  and the station's public coordinates are part of the genuine provider
  response and are left untouched.

  ```bash
  curl "https://aviationweather.gov/api/data/metar?ids=LLBG&format=json"
  ```

- **`ims_isr_cities.xml`** — Israel Meteorological Service keyless daily
  city-forecast feed (all cities, not observations). Captured as raw bytes
  with no re-encoding, preserving the `ISO-8859-8` declared charset exactly
  (verified: the bytes decode cleanly as `iso-8859-8` and the XML prolog
  declares `encoding="ISO-8859-8"`).

  ```bash
  curl "https://ims.gov.il/sites/default/files/ims_data/xml_files/isr_cities.xml" \
    -o ims_isr_cities.xml
  ```

## Synthesized from documented shapes (no credentials available)

These two providers require credentials this environment does not have
(OpenWeather API key, IMS Envista `ApiToken`). Per this task's instruction,
their fixtures are hand-built from the documented response shape rather than
captured live, and are recorded here as synthesized, not measured:

- **`openweather_current.json`** — shape of the OpenWeather 2.5
  current-weather endpoint (`/data/2.5/weather`), built from OpenWeather's
  published API documentation. Coordinates are the same Greenwich neutral
  point; `name`/`id` are placeholder values (`"Example-Fixture-City"`, `0`),
  not a real OpenWeather city record. This fixture may not exactly match a
  real response; task t12's first live fetch is the recorded risk that
  verifies it (see plan risks).

- **`ims_stations.json`** — shape of the IMS Envista `/v1/stations` list,
  built from the IMS API PDF (`API_Explanation_en.pdf`) documented fields.
  Station id, name and coordinates are placeholders (`EXAMPLE-FIXTURE-STATION`,
  `latitude/longitude: 0.0`) — no real IMS station id or Tel-Aviv-area
  station was available without a token. Channel ids/aliases (`TD`, `RH`,
  `WS`, `WD`, `WSMax`, `Rain`, `BP`, `Grad`, `DiffR`, `NIP`) follow the PDF's
  documented channel names so adapter code can be written against real
  field names.

- **`ims_latest.json`** — shape of the IMS Envista `/v1/stations/{id}/data/latest`
  response for the same placeholder station, with a `datetime` label of
  `+03:00` while the documented IMS quirk (used by task t13) is that the
  observation instant is always `UTC+2` regardless of that label — this
  fixture intentionally keeps the mismatched label so t13's UTC-correction
  test has a summer-date value to convert.

## Neutral point and the coordinate-hygiene test

`tests/test_repo_hygiene.py` scans `climate/`, `tests/` (outside this
`tests/fixtures/` directory) and the repo's docker files for latitude/longitude
literals and fails the build if it finds one. Fixture bytes in this directory
are exempt by design: real provider responses (MET Norway, METAR) legitimately
contain public station/request coordinates as part of their genuine payload,
and those bytes must stay byte-exact per the spec's "stored as-is" decision.
No fixture in this directory encodes the user's own location.
