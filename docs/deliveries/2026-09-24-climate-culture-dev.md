# Delivery Summary — climate.culture.dev

plan: `climate-culture-dev` · run: `partial` · date: `2026-09-24`
baseline: `devague summary skeleton`

Wave 1 (`t1`–`t4`) merged on branch `feat/climate-culture-dev` and is in
PR #10, which is open and not yet merged. `t5` (edge cache) is blocked on
agentculture/cultureflare#56. `t6` (end-to-end verification) is partial:
the unauthenticated checks ran, but the SSO-login and cache checks can't run
yet. The success signal `c29` is therefore **not** met in full.

## Intent

> The climate-cli weather dashboard is published at
> <https://climate.culture.dev> through a Cloudflare tunnel to the
> loopback-bound weather-web service, following the \*.culture.dev tunnel
> precedent

After: <https://climate.culture.dev> serves the dashboard and /api/v1 over
HTTPS through a Cloudflare tunnel, with a licence footer and an edge cache,
while weather-web stays loopback-bound on the host. The Cloudflare side
(tunnel, DNS, Access app and policy) was provisioned by the user by hand
before the run; the connector unit was installed by the agent. The run
executed `docs/plans/2026-09-24-climate-culture-dev.md` (6 tasks, 3 waves)
through `/assign-to-workforce`, with the split approved as proposed
(`docs/plans/2026-09-24-climate-culture-dev-split.md`).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Operations doc for climate.culture.dev: topology, the hand-turns as run, verification and reversal
- `t2` — README + docs/weather-api.md: document the tunnel + SSO exposure path
- `t3` — IMS observation adapter gets a non-empty licence, so every provider shows a licence in the footer
- `t4` — Dashboard shows 'signed out — reload to sign in' when the Access session expires
- `t5` — Edge cache rule via cultureflare once agentculture/cultureflare#56 ships
- `t6` — End-to-end verification of the success signal (agent-side + one user SSO login)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `docs/operations/climate-culture-dev.md` (commit `bec7002`, main agent, merged `b803fcc`) |
| `t2` | delivered | README "Exposing the web service" and `docs/weather-api.md` §2.1/§2.3 updated (commit `56f5a40`, sonnet subagent, merged `57ba673`) |
| `t3` | delivered | `ims.py` attribution: licence `IMS Terms of Use`, url `https://ims.gov.il/en/termOfuse`; registry-wide licence test (commit `891c214`, sonnet subagent, merged `9ab13ef`) |
| `t4` | delivered | `redirect: "manual"` + `signedOut` ApiError, signed-out page state with a reload link, polling stops; 6 static tests (commit `88e6303`, sonnet subagent, merged `f699f2c`); browser check run by the main agent before merge |
| `t5` | blocked | no cache rule exists; waits on the `cache-rule` commands requested in agentculture/cultureflare#56 |
| `t6` | partial | ran: unauthenticated 302 on `/`, `/api/v1/health`, `/api/v1/latest`; 6/6 licences on the loopback API; a leak scan of the served content (see Evidence). Not run: the user's SSO login and dashboard parity check, the cache-HIT check (needs `t5`). No delivery doc update beyond this summary. |

## Mid-work Decisions

No deviation records exist for this plan. Decisions not covered by a record:

- `t3` also moved the IMS observation attribution url from the site root to
  the terms page, matching `ims_forecast`. The acceptance criterion names
  that url, so this is within contract. The licence label was copied from
  `ims_forecast` because the terms page is JS-rendered and couldn't be read
  (lapse `l1`, proposed).
- `t4` touched both `js/app.js` and `js/panels.js`. The instruction allowed
  "js/app.js (or panels.js)"; the page-state renderer lives in `panels.js`
  and the polling lives in `app.js`, so both were needed. No CSS change: the
  signed-out state reuses the neutral `.page-state` styling rather than the
  red outage styling.
- `t4`'s browser check used a scratch gate proxy (a stand-in for Cloudflare
  Access that answers `/api/v1/*` with a 302 to another origin) in front of
  `scripts/dev_dashboard_server.py`. It drove the cached Playwright arm64
  Chromium directly, because the Playwright MCP expected Google Chrome, which
  isn't installed. The first run gave a false negative (it re-selected the
  already-checked 24 h window, so no refresh fired); the second run switched
  to 6 h and passed.
- The `t1` doc leaves out the tunnel and Access app ids. They aren't secrets,
  but nothing in the repo needs them.

## Drift From Plan

No deviation records exist; every entry below is recorded directly.

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t5` | blocked on an external dependency (agentculture/cultureflare#56); the plan anticipated this as risk `r1` | needs-follow-up |
| `t6` | only the agent-side, unauthenticated half of the success signal ran; the SSO-login and cache-HIT checks need the user and `t5` | needs-follow-up |
| `t4` | touched `app.js` and `panels.js` (instruction said "app.js (or panels.js)") | acceptable |

`t1`, `t2` and `t3` delivered their contracts with no drift.

## Evidence

- tests (at `475b8e3`):
  - `tests/weather/providers/test_base.py::test_every_real_registered_provider_declares_a_non_empty_licence` — pass
  - `tests/weather/web/test_dashboard_static.py::test_the_fetch_never_follows_a_cross_origin_redirect` — pass
  - `tests/weather/web/test_dashboard_static.py::test_an_opaque_redirect_is_flagged_signed_out_not_unreachable` — pass
  - `tests/weather/web/test_dashboard_static.py::test_api_error_carries_a_signed_out_flag` — pass
  - `tests/weather/web/test_dashboard_static.py::test_a_signed_out_response_stops_polling_and_renders_its_own_state` — pass
  - `tests/weather/web/test_dashboard_static.py::test_signed_out_state_renders_a_reload_link_built_with_text_content` — pass
  - `tests/weather/web/test_dashboard_static.py::test_no_static_javascript_ever_uses_innerHTML` — pass
  - full suite `uv run pytest -n auto --cov=climate -q` — 1003 passed, coverage 91.6%
- browser (agent-side, merged `t4` code): the signed-out state rendered with
  "Reload to sign in"; no outage state; 0 API requests in the 65 s after
  sign-out.
- live (2026-09-24T08:30Z):
  - `curl` via 1.1.1.1 to `/`, `/api/v1/health`, `/api/v1/latest` — all 302
    to `agentculture.cloudflareaccess.com/cdn-cgi/access/login/climate.culture.dev`
  - `GET 127.0.0.1:8095/api/v1/providers` — 6/6 non-empty licences
  - `docker compose ps` — `weather-web 127.0.0.1:8095->8095`; `weather-mongodb`
    no host port
  - `docker exec weather-web env` (names only) — no API/TOKEN variables; the
    tracker has both provider keys
  - `systemctl --user is-active cloudflared-climate.service` — active
  - leak scan of 9 served paths (`/`, `/js/app.js`, `/api/v1/{health,providers,locations,latest,series,forecast,stats}`, 226 KB): 0 coordinate keys, 0 of 2 configured coordinates (raw or 2 dp), neither provider key value present (keys injected with `grant run`, never printed)
  - `cultureflare remote-login show --hostname climate.culture.dev` — tunnel,
    ingress `→ http://127.0.0.1:8095`, proxied CNAME, Access app, 1 allow rule
- docs: `grep` finds 0 occurrences of `CLIMATE_WEATHER_URL=https://climate.culture.dev`
  in README and `docs/weather-api.md`; markdownlint clean.
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit` — clean;
  `teken cli doctor . --strict` — pass
- commits: `main..475b8e3` on `feat/climate-culture-dev`
- PRs / issues: PR #10 (open; CI green, Qodo review pending);
  agentculture/cultureflare#56 (open); #8 (follow-up)
- validation records (`/validate-delivery`, all `proposed`): obligations
  `o1`–`o7`, evidence `e1`–`e7`; `o6` (cache) and `o7` (end-to-end) have no
  evidence yet

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| an unauthenticated request to climate.culture.dev gets the Access login redirect, never data | high | live `curl` 302 on three paths · evidence `e5` |
| weather-web stays loopback-only, Mongo has no host port, and the web container holds no provider keys | high | live `docker compose ps` / `env` · evidence `e6` |
| the dashboard shows a signed-out state and stops polling when the session expires | high | browser check · evidence `e2`; static tests · commit `88e6303` |
| every provider shows a non-empty licence | medium | test `tests/weather/providers/test_base.py::test_every_real_registered_provider_declares_a_non_empty_licence` · live 6/6 · lapse `l1` is pending: the IMS label wasn't checked against the terms page |
| served HTML/JSON carry no coordinate, configured location or provider key | high | leak scan above (loopback, same origin the tunnel serves) |
| the operations doc describes the setup as run, with no email or secret | high | file `docs/operations/climate-culture-dev.md` · commit `bec7002` |
| README and API docs document the SSO path without advertising `CLIMATE_WEATHER_URL` | medium | evidence `e7` (manual grep, coverage strength) · commit `56f5a40` |
| after SSO login the dashboard matches the loopback one over HTTPS | unverified | needs the user's SSO login (`t6`) — not claimed done |
| repeat reads are served from the Cloudflare edge cache | unverified | `t5` blocked on agentculture/cultureflare#56 — not claimed done |

Lapse ledger evidence:

pending approval (not yet evidence): `l1`

## Remaining Work / Follow-up

- `t5` — once agentculture/cultureflare#56 ships: `cultureflare cache-rule set`
  for `/api/v1/*` (60 s, not `/health`) and static assets (1 h), then record
  the rule ids in `docs/operations/climate-culture-dev.md` — owner: main agent.
- `t6` — the user logs in via SSO once and confirms parity with
  `127.0.0.1:8095` and the 6-licence footer; the agent then checks
  `cf-cache-status: HIT` after `t5` — lands in a patch PR.
- Adjudicate lapse `l1` (IMS licence label) — owner: the user; the fix, if
  wanted, is to read the rendered IMS terms page in a JS-capable browser.
- Adjudicate the `/validate-delivery` records (`o1`–`o7`, `e1`–`e7`) — owner:
  the user.
- Merge PR #10 after the Qodo review is triaged — owner: the user (gate 3).
- Parked follow-ups from the frame: tunnel health isn't visible to
  `climate doctor` (`v5`); the application/markdown seam for remote agents
  (`v4`); Mongo auth with a read-only web user and origin JWT validation
  (issue #8, decision `c35`).
