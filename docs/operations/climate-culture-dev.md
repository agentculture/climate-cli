# climate.culture.dev — the tunnel, the loopback origin and the SSO gate

How the weather dashboard and its read-only API are published at
`https://climate.culture.dev`: what runs where, the one-off operator steps
(hand-turns) as they were run on 2026-09-24, how to verify it, and how to undo
it. The spec is
[`docs/specs/2026-09-24-climate-culture-dev.md`](../specs/2026-09-24-climate-culture-dev.md);
the recipe follows `culture-nodes/docs/operations/nodes-culture-dev.md`, the
first live service on a `*.culture.dev` tunnel.

## Topology

```text
browser ──HTTPS──▶ Cloudflare edge (Access SSO, team agentculture.cloudflareaccess.com)
                        │  tunnel "climate-culture-dev" (outbound from this host;
                        │  token-mode cloudflared user unit, remote-managed ingress)
                        ▼
host: cloudflared-climate.service ──HTTP──▶ 127.0.0.1:8095 (weather-web, loopback only)
                                                    │  compose network
                                                    ▼
                                            weather-mongodb (no host port)
```

- **Nothing opens on the host.** `weather-web` stays published as
  `127.0.0.1:8095->8095` (the compose default, `CLIMATE_WEB_BIND` unset).
  The tunnel dials out; no LAN or public port is added.
- **Access is the gate.** The web service itself still has no authentication,
  rate limiting or CORS (see [`docs/weather-api.md`](../weather-api.md)).
  Every request to the hostname passes Cloudflare Access first; an
  unauthenticated request gets a `302` to the Access login and never reaches
  the origin. Access alone is the gate for now: Mongo auth with a read-only web
  user ([issue #8](https://github.com/agentculture/climate-cli/issues/8)) and
  checking the Access JWT at the origin are follow-ups, not prerequisites.
- **No provider keys behind the tunnel.** `weather-web` has no `env_file:`, so
  it never receives `CLIMATE_OPENWEATHER_API_KEY` or `CLIMATE_IMS_API_TOKEN`;
  only `weather-tracker` does.
- **The ingress is pinned.** The hostname → `http://127.0.0.1:8095` mapping
  lives in Cloudflare, not in this repo. Changing `CLIMATE_WEB_PORT` or
  `CLIMATE_WEB_BIND` means re-running Step 1 with the new `--service`, or the
  hostname answers `502`.

## Prerequisites

- `cultureflare`, `cloudflared` and `grant` on `PATH` (`~/.local/bin`).
- `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` exported for Step 1. The
  `culture.dev` zone is on the **Free Website** plan (`cultureflare zones list`);
  the tunnel and the Access app add no paid resources to it.
- The stack running: `uv run climate stack up`.
- User services start at boot without a login: `loginctl show-user "$USER" -p Linger`
  must report `Linger=yes`.

## Step 1 — tunnel, DNS, Access app and policy (hand-turn, once)

```bash
cultureflare remote-login setup --hostname climate.culture.dev \
  --service http://127.0.0.1:8095 --allow <operator email> --apply
```

One idempotent run creates the named tunnel with remote-managed ingress, the
proxied `CNAME` in the `culture.dev` zone, the Access application and an
allow-by-email policy. The allow-list email is operator configuration: pass it
on the command line, and never write it into this repo, a spec or the public
memory store. Without `--apply` the command is a dry run.

## Step 2 — seal the tunnel token in grant (hand-turn)

The connector token is stored in `grant`, never in a file:

| grant name | hidden | used by |
|------------|--------|---------|
| `CLIMATE_CLOUDFLARE_TUNNEL_TOKEN` | yes | the unit below (`TUNNEL_TOKEN`) |
| `CLIMATE_CLOUDFLARE_TUNNEL_ID` | no | reference only — the token-mode connector doesn't read it |

Use `grant show NAME` to check that a secret exists; it prints metadata, never
the value.

## Step 3 — install and enable the unit (hand-turn)

`~/.config/systemd/user/cloudflared-climate.service`:

```ini
[Unit]
Description=Cloudflare tunnel for climate.culture.dev (climate-cli weather-web on 127.0.0.1:8095, behind Cloudflare Access SSO)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# The token is injected into the child environment, never the command line.
ExecStart=%h/.local/bin/grant run --inject TUNNEL_TOKEN=CLIMATE_CLOUDFLARE_TUNNEL_TOKEN -- %h/.local/bin/cloudflared tunnel --no-autoupdate run
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now cloudflared-climate.service
journalctl --user -u cloudflared-climate.service | grep 'Registered tunnel connection'
```

The `ICMP proxy feature is disabled` warning in the log is harmless; the other
tunnel units on this host print it too.

## Verify

Checks as run on 2026-09-24 (ids elided):

| Check | Command | Result |
|-------|---------|--------|
| Cloudflare side | `cultureflare remote-login show --hostname climate.culture.dev` | tunnel `climate-culture-dev`; ingress `climate.culture.dev → http://127.0.0.1:8095`; proxied `CNAME` ✓; Access app present; 1 allow rule; no service token |
| Connector | `systemctl --user is-active cloudflared-climate.service` | `active` (enabled), 4 registered connections |
| Loopback origin | `docker compose -p climate-weather ps` | `weather-web 127.0.0.1:8095->8095/tcp`; `weather-mongodb` has no host port |
| Keyless web | `docker exec weather-web env` (names only) | no `API`/`TOKEN`/`KEY` variable except the base image's `GPG_KEY` |
| SSO gate | `curl -sI https://climate.culture.dev/api/v1/health` | `302` → `agentculture.cloudflareaccess.com/cdn-cgi/access/login/climate.culture.dev` |

A freshly created hostname can take a few minutes to reach a local resolver
that cached the earlier miss; check through a public resolver meanwhile:

```bash
ip=$(dig +short @1.1.1.1 climate.culture.dev A | head -1)
curl -sI --resolve "climate.culture.dev:443:$ip" https://climate.culture.dev/api/v1/health
```

## Agents and the CLI

There is no Access service token, so remote CLI or `climate doctor` access
through the hostname gets the SSO redirect. Agents read locally for now
(`http://127.0.0.1:8095` or the `climate` CLI's markdown output). Don't point
`CLIMATE_WEATHER_URL` at `https://climate.culture.dev` until a service token
exists.

## Edge cache (pending)

A Cloudflare cache rule is planned: `/api/v1/*` for 60 s at the edge (except
`/api/v1/health`) and static assets for 1 h. It waits on the `cache-rule`
commands requested in
[agentculture/cultureflare#56](https://github.com/agentculture/cultureflare/issues/56).
Until then every request reaches the origin.

## Undo

```bash
systemctl --user disable --now cloudflared-climate.service
rm ~/.config/systemd/user/cloudflared-climate.service && systemctl --user daemon-reload
cultureflare remote-login teardown --hostname climate.culture.dev --apply
```

The stack itself is untouched: `weather-web` keeps serving `127.0.0.1:8095`
locally.
