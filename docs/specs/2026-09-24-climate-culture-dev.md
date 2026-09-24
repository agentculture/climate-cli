# climate.culture.dev

> The climate-cli weather dashboard is published at <https://climate.culture.dev> through a Cloudflare tunnel to the loopback-bound weather-web service, following the \*.culture.dev tunnel precedent

## Audience

- Signed-in viewers on the Access allow list (the operator for now); agents reading over HTTP need an Access service token, since `CLIMATE_WEATHER_URL` alone gets the SSO redirect
  - instruction: check README 'Exposing the web service' names both humans (dashboard) and agents (`CLIMATE_WEATHER_URL`)

## Before → After

- Before: The dashboard is reachable only at <http://127.0.0.1:8095> on the host running the stack, and README.md:273 warns against putting it on the public internet
  - instruction: compare README.md before/after the change
- After: <https://climate.culture.dev> serves the dashboard and /api/v1 over HTTPS through a Cloudflare tunnel, with a licence footer and an edge cache, while weather-web stays loopback-bound on the host
  - instruction: open <https://climate.culture.dev> in a browser; run ss -ltnp and confirm 8095 is bound to 127.0.0.1 only

## Why it matters

- Measured conditions can be viewed from anywhere, without LAN or VPN access, while only allow-listed people can read the collected history
  - instruction: n/a — motivation

## Requirements

- climate.culture.dev is served by a remote-managed Cloudflare tunnel created with 'cultureflare remote-login setup --hostname climate.culture.dev --service <http://127.0.0.1:8095> --apply', the same family as nodes/terminal/lobes.culture.dev — not Cloudflare Pages, because the dashboard is a live http.server over Mongo, not static content
  - honesty: A named tunnel exists in the culture.dev zone with remote-managed ingress climate.culture.dev → <http://127.0.0.1:8095>, and 'cultureflare remote-login show --hostname climate.culture.dev' reports it
- A cloudflared-climate.service systemd user unit runs the tunnel with its token injected by 'grant run --inject `TUNNEL_TOKEN`=`CLIMATE_CULTURE_DEV_TUNNEL_TOKEN`', never a token file on disk
  - honesty: systemctl --user status cloudflared-climate.service is active; the unit's ExecStart uses 'grant run --inject' and no token file exists under ~/.config/cloudflared for climate
- The public deployment is documented: README 'Exposing the web service' and docs/weather-api.md describe the tunnel + Access SSO path and the public base URL, and state that remote CLI/doctor access needs an Access service token, which is not provisioned yet — agents and the CLI read locally for now
  - honesty: README and docs/weather-api.md document climate.culture.dev behind Access SSO, drop the unqualified 'do not put it on the public internet' warning, and do not advertise `CLIMATE_WEATHER_URL`=<https://climate.culture.dev> for the CLI
- The dashboard footer (#credits) lists every enabled provider with a non-empty licence; the IMS observation provider's licence, blank today, is filled in from IMS's terms of use
  - honesty: GET /api/v1/providers returns a non-empty attribution.licence for all 6 providers, and the footer renders each one
- The edge cache rule is created with 'cultureflare cache-rule set' once agentculture/cultureflare#56 ships (the origin keeps sending Cache-Control: no-store, so the rule overrides the edge TTL); until then the tunnel + Access go live without the cache
  - honesty: The rule is recorded (rule id + TTLs) in a repo doc, and cf-cache-status: HIT appears on a repeated request
- When the Access session expires mid-poll, the dashboard says 'signed out — reload to sign in' instead of 'The weather service is not answering'
  - honesty: With the `CF_Authorization` cookie removed, the next poll shows the sign-in message, not the outage state

## Honesty conditions

- The public hostname resolves and serves the same dashboard as <http://127.0.0.1:8095>, over HTTPS
- docker compose ps shows weather-web as 127.0.0.1:8095->8095 after the change; docker/weather.env keeps `CLIMATE_WEB_BIND` unset or 127.0.0.1
- weather-mongodb has no published host port in docker-compose.yml
- api.py still emits provenance.station as null (GAP); any future change that fills it re-opens this spec's privacy review
- docker exec weather-web shows no `CLIMATE_IMS_API_TOKEN` or `CLIMATE_OPENWEATHER_API_KEY`
- An unauthenticated request to any climate.culture.dev path gets the Access login redirect, never weather data
- Today's stack publishes 127.0.0.1:8095 only (docker compose ps)
- Nothing is exposed except through the tunnel: no LAN or public port opens on the host
- No new cost: the Cloudflare tunnel and cache are on the existing free zone
- Every check in the signal is run and passes before the spec is marked delivered
- grep -r for the operator email across the repo, .devague and .eidetic finds nothing; 'cultureflare remote-login show --hostname climate.culture.dev' reports an Access app with one allow policy
- The README's exposure section states that the tunnel ingress is pinned to 127.0.0.1:8095 and that changing `CLIMATE_WEB_PORT`/BIND means re-running 'cultureflare remote-login setup'

## Success signals

- Unauthenticated curl <https://climate.culture.dev/api/v1/health> gets the Access login redirect (302), not data; after SSO login the dashboard loads; a repeated /api/v1/latest within 60 s returns cf-cache-status: HIT; 6 of 6 providers show a non-empty licence in the footer; 0 coordinates or secrets in any response
  - instruction: curl -sI twice and read cf-cache-status; GET /api/v1/providers and count non-empty attribution.licence; grep the served HTML/JSON for the private label's coordinates and key prefixes

## Scope / boundaries

- weather-web stays published on host loopback only (`CLIMATE_WEB_BIND` default 127.0.0.1); the tunnel reaches it locally, so docker-compose.yml and docker/weather.env need no change and the port is never opened to the LAN
- MongoDB is never exposed: it publishes no host port, and remote access goes through the read-only web API only
- provenance.station must stay unpopulated (or be re-audited) while the dashboard is public — METAR/IMS station identifiers would reveal the tracked location
- Provider secrets reach weather-tracker only: `CLIMATE_IMS_API_TOKEN` comes from the grant secret `IMS_API_TOKEN` into the gitignored docker/weather.env, and weather-web never receives it — which matters more once weather-web is public
- The tunnel ingress is pinned to <http://127.0.0.1:8095> in Cloudflare, so changing `CLIMATE_WEB_PORT` or `CLIMATE_WEB_BIND` means re-running remote-login setup, or climate.culture.dev returns 502

## Non-goals

- No code change to climate/weather/web/static/ — the dashboard already works behind a proxy on another hostname

## Assumptions

- Serving the API publicly leaks no coordinates, station ids or secrets today: responses carry location labels only, provenance.station is always null, provider URLs are redacted of keys and never served, and the web container gets no API keys

## Scope exploration

- `s1` — `culture-nodes/docs/operations/nodes-culture-dev.md + cultureflare/README.md:55,90`: Live local services on \*.culture.dev use a remote-managed cloudflared tunnel provisioned by 'cultureflare remote-login setup' (tunnel + CNAME + optional Access app); static sites (tools.culture.dev) use CF Pages instead. The dashboard is a live service → tunnel family.
  - seeds: `c2`
- `s2` — `~/.config/systemd/user/cloudflared-terminal.service`: Newest precedent unit uses 'grant run --inject `TUNNEL_TOKEN`=`TERMINAL_CF_TUNNEL_TOKEN` -- cloudflared tunnel --no-autoupdate run'; no ~/.cloudflared or /etc/cloudflared config exists — ingress is remote-managed. Older units use 0600 token files in ~/.config/cloudflared.
  - seeds: `c3`
- `s3` — `docker-compose.yml:111 + weather-web container (docker compose ps)`: Host publish is '${`CLIMATE_WEB_BIND`:-127.0.0.1}:${`CLIMATE_WEB_PORT`:-8095}:8095'; running stack shows 127.0.0.1:8095->8095 — exactly the loopback-publish shape nodes.culture.dev uses (127.0.0.1:18081).
  - seeds: `c4`
- `s4` — `docker-compose.yml weather-mongodb + .devague/frames/weather-tracking-service.json resolved question`: weather-mongodb has no ports: (reachable only on the compose network); prior spec resolved that outside access is served by the web service, not by opening the database.
  - seeds: `c5`
- `s5` — `climate/weather/web/static/js/api.js:1-20, index.html`: All API calls are same-origin relative paths; no hardcoded host, no CDN or external fonts — portable behind the tunnel unmodified.
  - seeds: `c6`
- `s6` — `docs/weather-api.md:26 + climate/weather/web/api.py:597,1140,1449 + store.py redact_url + docker-compose.yml weather-web (no env_file)`: Contract forbids coordinates in any response; implementation nulls provenance.station ('GAP'); endpoint names not URLs are served; web container deliberately receives no provider credentials.
  - seeds: `c7`
- `s7` — `docs/weather-api.md:163 vs api.py:597`: Contract earmarks provenance.station for a station id on ims/metar; implementation leaves it null today. Filling it later would turn a public API into a location leak.
  - seeds: `c8`
- `s8` — `README.md:261-279 + docs/weather-api.md:47-49,87 + climate/cli/_commands/doctor.py:209`: README currently says 'do not put it on the public internet'; the API doc lists <http://127.0.0.1:8095> as the base URL and justifies no-CORS by same-origin. CLI/doctor already honour `CLIMATE_WEATHER_URL`, so no code change — docs only.
  - seeds: `c9`
- `s9` — `README.md:273-279 (Exposing the web service warning)`: The web service has no authentication, and the README explicitly says not to put it on the public internet without a reverse proxy with auth. So whether climate.culture.dev is public or behind Access is a user decision, not something to infer.
  - seeds: `q1` (question, resolved)
- `s10` — `climate/weather/web/api.py:1844 + dispatch()`: GET/HEAD only (405 otherwise), generic 500 bodies, no access log, no throttling, and Cache-Control: no-store — a public endpoint relies entirely on the Cloudflare edge for abuse protection.
  - seeds: `q2` (question, resolved)
- `s11` — `climate/weather/mongo.py:361-362 + live explain() on weather.readings`: Only indexes are fetches(provider,location,`requested_at`) and readings(`fetch_id`). A 7-day series query explains as SORT over COLLSCAN: 172,882 docs examined to return a window. Measured /series 6h = 0.56s and 7d = 0.60s — flat, because the cost is the full scan, not the window.
  - seeds: `c10` (rejected)
- `s12` — `curl timings against 127.0.0.1:8095 + api.py /series (from/to/step/max_points push-down, MAX_SERIES_STORE_POINTS=100000)`: The API already pushes since/until/limit into store.series and grids the result (default 2000, ceiling 10000 points). The dashboard sends from and step per window (app.js:31-35), so responses are bounded; only the Mongo side reads everything.
  - seeds: `c11` (rejected)
- `s13` — `climate/weather/web/static/js/app.js:5-8,28,280-319,499-513 + docs/weather-api.md:1458`: Every 60 s poll, window change and variable-tab change re-runs the full refresh(): 7 requests (health, providers, locations, latest, series, forecast, stats) re-downloading the whole window. There is no cache ('never a merge with something remembered'). The contract rules out pagination and cursors (§9), but from/to are documented, so chunking by time needs no API change. Open chunk-size question: q4 on c12.
  - seeds: `c12` (rejected)
- `s14` — `climate/weather/web/static/js/app.js:199-204 (variable tabs) + 220-278 (loadShape/loadView)`: A variable-tab click calls the same full refresh() as the poll, so 6 of the 7 requests fetch data that didn't change.
  - seeds: `c13` (rejected)
- `s15` — `climate/weather/web/static/ (grep IntersectionObserver = 0 hits) + app.js loadView`: All three bands (Now, Trend, Collection) are fetched unconditionally on every refresh; nothing is lazy today.
  - seeds: `c14` (rejected)
- `s16` — `climate/weather/web/api.py:126,1654-1686 + mongo.py _fetch_from_document`: `bytes_stored` = sum(len(record.body)) over `iter_fetches`(limit=20000); the Mongo query has no projection, so each /stats call pulls raw bodies (24 MB in the fetches collection after 7 days). Measured /stats 24h = 0.25s.
  - seeds: `c15` (rejected)
- `s17` — `climate/weather/web/static/js/chart.js:301-332 + app.js WINDOWS steps`: drawSeries emits every point as SVG, but points arrive pre-gridded (6h/900s = 24, 24h/1800s = 48, 7d/3600s = 168 buckets), so there's no rendering win without longer windows.
  - seeds: `c16` (rejected)
- `s18` — `live aggregation over weather.readings grouped by provider/kind`: open-meteo forecast 147,699 + met-no forecast 23,320 out of 172,882; observation/model rows are about 1,700. Without an index, every chart query scans all the forecast rows too.
  - seeds: `c17` (rejected)
- `s19` — `tests/weather/web/test_dashboard_static.py + tests/weather/web/test_api.py:655 + tests/weather/store_contract.py:384-408`: Several tests pin refresh()'s literal structure and the store limit kwarg; restructuring the refresh for chunks and lazy loading will need those tests rewritten, and a new JS module must be added to `EXPECTED_FILES`.
  - seeds: `c18` (rejected)
- `s20` — `docker/weather.env (gitignored, 0600) + docker-compose.yml weather-web (no env_file) + docker exec env check`: IMS token injected via 'grant run --inject' on 2026-09-24; after stack up the tracker has it (ims stations fetch http=200) and the web container does not.
  - seeds: `c19`
- `s21` — `climate/weather/web/static/index.html:92 + js/panels.js:419-428 + providers/ims.py:391-393 + GET /api/v1/providers`: A credits footer already renders attribution.text/url plus attribution.licence per provider. Live /providers shows five of the six with a licence; the IMS observation adapter has licence="" so its footer row shows no licence.
  - seeds: `c23`
- `s22` — `cultureflare --help + cultureflare/README.md`: cultureflare covers zones, dns and remote-login only — there's no cache-rule command, so the rule needs the Cloudflare API/dashboard directly (or a cultureflare feature request).
  - seeds: `c24`
- `s23` — `climate/weather/web/api.py:1844 (_write_json)`: Every API JSON response sends Cache-Control: no-store, and static files send no cache headers; a Cloudflare cache rule that honours origin headers would cache nothing on /api, so it must override the edge TTL.
  - seeds: `c24`
- `s24` — `culture-nodes/docs/operations/nodes-culture-dev.md:18,33 + cultureflare/README.md:74,89`: The Access-gated precedent is nodes.culture.dev: 'remote-login setup --allow EMAIL' creates tunnel + DNS + Access app + allow policy; README row 74 notes an optional service token for non-browser clients.
  - seeds: `c30`
- `s25` — `agentculture/cultureflare#56 (filed 2026-09-24)`: Asked cultureflare for a cache-rule list/set/delete group on the `http_request_cache_settings` phase, mirroring cf-redirect-create.sh. The issue also asks them to confirm `override_origin` beats no-store, and that cached responses are only served after the Access check.
  - seeds: `c24`
- `s26` — `challenge pass / unstated-assumptions lens: spec c9 vs c30/c32 + cultureflare remote-login show (service-token: not found)`: c9 asks the README to tell users to set `CLIMATE_WEATHER_URL`=<https://climate.culture.dev> for remote CLI/doctor use, but the host is SSO-gated with no service token (c32). Probe: unauthenticated GET /api/v1/health → 302 to agentculture.cloudflareaccess.com, so the CLI would fail. c9's doc text needs rewording.
  - seeds: `c9`
- `s27` — `challenge pass / failure-mode lens: climate/weather/web/static/js/api.js:60-71`: fetch() follows the 302 to the cross-origin Access login; that fails as a network TypeError, which api.js maps to ApiError('The weather service is not answering.', unreachable). An expired session (the Access default) therefore looks like an outage on a 60 s poll. Not probed with a real expired session.
  - seeds: `c33`
- `s28` — `challenge pass / security + adjacent-systems lens: gh issue 8 + culture-nodes/docs/operations/nodes-culture-dev.md:31-33`: Issue #8 (open) lists 'README: make exposure beyond loopback conditional on this being in place'. The nodes precedent honours the Access JWT at its loopback listener; climate web accepts any caller that reaches 127.0.0.1:8095. The spec never mentions either.
  - seeds: `q8` (question, resolved)
- `s29` — `challenge pass / operations lens: cultureflare remote-login show (ingress) + docker-compose.yml:111`: Ingress is remote-managed and hard-codes the port; the compose port is env-configurable. Nothing links the two.
  - seeds: `c34`
- `s30` — `challenge pass / lifecycle + reversibility lens: loginctl Linger=yes + ~/.config/systemd/user/cloudflared-climate.service`: Clean: linger is on, so the user unit starts at boot without a login; Restart=always. The token is injected by grant (no token file). Reversal = disable the unit + 'cultureflare remote-login teardown'. `CLIMATE_CLOUDFLARE_TUNNEL_ID` is stored non-hidden and unused by the token-mode connector (harmless).
- `s31` — `operator hand-turn evidence (2026-09-24)`: User provisioned tunnel climate-culture-dev + CNAME + Access app + 1 allow policy; the agent installed cloudflared-climate.service (4 registered connections). Unauthenticated /api/v1/health and /api/v1/latest → 302 to the Access login; 8095 still listens on 127.0.0.1 only.

## Decisions

- A Cloudflare cache rule fronts climate.culture.dev, so repeat reads are served from the edge instead of hitting the unauthenticated, unthrottled origin
- Provider data may be re-served publicly (user: 'We can read, it's not indexed yet though'); the licence for every provider is shown at the bottom of the dashboard
- For now climate.culture.dev sits behind Cloudflare Access SSO: `cultureflare remote-login setup ... --allow <operator email>`. The allow-list email is operator configuration supplied at provisioning time, never written into the repo, this spec or the public memory store (supersedes the earlier public/--no-access decision c20)
- Cache TTLs: /api/v1/\* 60 s at the edge (matching the dashboard's 60 s poll), /api/v1/health not cached, static assets 1 h
- No Access service token for agents for now: agents read locally (127.0.0.1:8095 or the climate CLI's markdown output)
- Cloudflare Access alone gates climate.culture.dev for now; issue #8 (Mongo auth + read-only web user) and Access-JWT validation at the origin stay follow-ups, not prerequisites

## Hard questions

- Public or gated? --no-access (anyone can read, like lobes.culture.dev) or Cloudflare Access SSO with an --allow email policy (like nodes.culture.dev)? The web service has no auth of its own (README.md:273). (resolved: User: public for now → --no-access (see c20))
- If public: is Cloudflare's edge enough protection, or do we need a rate-limit / cache rule? api.py has no throttling and sends Cache-Control: no-store on every JSON response. (resolved: User: add a Cloudflare cache rule (see c21))
- contradiction with c32? (resolved: User confirmed the reword: docs say remote CLI access needs a service token (not provisioned); `CLIMATE_WEATHER_URL`=<https://climate.culture.dev> is not advertised)
- Cache TTLs: static assets (/, /js, \*.css) for e.g. 1 hour and /api/v1/\* for 60 s (matching the dashboard's 60 s poll)? /health excluded from caching? (resolved: User confirmed: /api/v1/\* edge TTL 60 s (excluding /api/v1/health), static assets 1 h)
- Do agents need remote HTTP read now (an Access service token via cultureflare, sealed in grant), or is local 127.0.0.1:8095 enough for them for now? (resolved: User: 'no need, or application/markdown seam for now' — no Access service token for agents now)
- Issue #8 says to make exposure beyond loopback conditional on Mongo auth + a read-only web user, and nodes.culture.dev also validates Cf-Access-Jwt-Assertion at the origin. Is Access alone enough for now, or is #8 (and/or origin JWT validation) a prerequisite? (resolved: User: Access alone is enough for now; issue #8 (Mongo auth + read-only web user) and origin JWT validation are not prerequisites)

## Open parks

- [unknown_nonblocking] Cache rule depends on agentculture/cultureflare#56 (cache-rule commands) — also confirms the rule respects Cloudflare Access and overrides origin no-store
- [unknown_nonblocking] Tunnel health is invisible to climate-cli: 'climate doctor' / 'stack status' don't check cloudflared-climate.service or the public hostname; a dead connector is noticed only by a viewer (Restart=always mitigates)
- [unknown_nonblocking] Residual after the challenge pass: not examined — Access session length/policy settings, the Cloudflare zone's WAF/bot settings, or an authenticated end-to-end browser check (needs the user's SSO login)
- [out_of_scope] Data-efficiency claims c10–c18 moved to frame dashboard-data-efficiency when the user split the scope into two specs (2026-09-24)
- [follow_up] An application/markdown seam for agents reading climate.culture.dev remotely (user's words: 'or application/markdown seam for now') — shape not yet defined

## Resolved vagueness

- [unknown_blocking] Provider licence terms (MET Norway, OpenWeather, IMS, Open-Meteo, METAR/aviationweather) were read in summary only; re-serving their data on the public internet may need attribution on the dashboard or be disallowed by one provider — README.md:276-279 and the weather-tracking-service frame's open lapse say re-check before exposing beyond the LAN — resolved: User: we can read/re-serve the data; the site is not indexed yet; show the licence at the bottom of the dashboard
