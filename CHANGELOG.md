# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-09-17

### Added

- **The weather-tracking service.** A `climate-weather` docker compose project (MongoDB + a polling tracker + a read-only web service) samples six free weather providers on their own refresh policies, stores every response **verbatim** (body bytes, status, cache headers, content hash) as the system of record, and derives re-derivable normalized readings from it. Failed fetches are stored too, so a gap in the data is distinguishable from a gap in collection, and nothing is ever back-filled.
- **Six provider adapters**, auto-discovered by the registry: `open-meteo`, `met-no` (Expires/`If-Modified-Since` conditional GET with an identifying User-Agent), `openweather` (free 2.5 current call), `ims` (Envista stations, token-gated, ships disabled), `metar` (keyless airport observations) and `ims-forecast` (keyless city-forecast XML). Each declares its capabilities, auth requirement, quota, freshness strategy and licence attribution. No provider value is ever dropped: a field with no vocabulary row is stored as `x_<name>`.
- **Four CLI nouns**, registered in `climate/cli/__init__.py`: `stack up|down|status|overview` (docker compose lifecycle), `weather latest|series|forecast|stats|overview` (queries through the HTTP API — the host CLI never connects to MongoDB), `providers [--limits]` (adapter metadata) and `backup dump|list|restore|overview` (mongodump/mongorestore through `docker compose exec`).
- **Exit code `3` = stale data.** `climate weather latest --max-age 10m` exits 3 in both markdown and `--json` mode when the freshest reading is older than the threshold, so an agent controlling hardware cannot silently act on a dead tracker's last value.
- **A read-only HTTP API and a chart-first dashboard** on `http://127.0.0.1:8095` (loopback by default, no authentication — exposing it is an explicit opt-in), documented in `docs/weather-api.md` and built from vanilla ES modules with no Node toolchain.
- **Location privacy as an enforced invariant**: coordinates rounded to 2 decimals (never more than the 4 MET Norway allows), locations identified by user-chosen label in every API response and dashboard asset, config held outside the repo at `~/.config/climate-cli/weather.json`, keys only in the gitignored `docker/weather.env` — with `tests/test_repo_hygiene.py` failing the build on any coordinate literal in `climate/`, `tests/` or the docker files.
- **Weather-tracker `doctor` checks** — repo-checkout management, newest fetch age per provider, database size, per-provider credentials and backup freshness — in the existing rubric `{healthy, checks: [...]}` shape.
- `[project.optional-dependencies] weather = ["pymongo>=4"]` — the container image installs it (`pip install ".[weather]"`); `dependencies` stays empty and `tests/test_stdlib_only.py` proves the host CLI imports with every third-party package blocked, in a subprocess.
- Explain-catalog entries for every new noun and verb path, plus `tests/test_explain_covers_cli.py`, which walks the real argparse tree and fails on any undocumented path.

### Changed

- **`README.md` and `CLAUDE.md` rewritten for the weather tracker** — quickstart, the CLI table, exit codes, a per-provider section (auth, licence/attribution, freshness strategy and default interval, rate limits with unverified figures marked as such), privacy notes, how to expose the web service and the warning that it has no authentication. No template boilerplate remains in the README, the root explain entry, `learn`, `overview` or the parser description.
- `sonar-project.properties` excludes `climate/weather/web/static/**` from **coverage only** — the dashboard assets stay in analysis; the repo runs no JS test runner, so they would otherwise redden the quality gate.
- `main()` now scopes `_CliArgumentParser._json_hint` to a single call, so a `--json` invocation can no longer make the next invocation in the same process emit a JSON error for a text-mode command.
- `FetchError`'s docstring now names the error kinds the scheduler actually writes (`timeout`, `transport`, `rate_limited`, `http_error`) instead of a different, aspirational set, and `store.redact_url` / `http.redact` cross-reference each other (stored values vs logged text).

### Fixed

- **PR review round (Qodo + SonarCloud).** The debug MongoDB profile no longer
  starts a second `mongod` on the live data volume (it is now an override file
  that only publishes a port); the web container no longer receives provider
  credentials, and provider availability shown by the API and `climate doctor`
  now comes from the tracker's heartbeat, the one process that holds the keys;
  `/series` and `/stats` are bounded (366-day span, arithmetic grid sizing,
  count queries) so a large request cannot exhaust the service; every
  vocabulary variable is queryable through one shared `climate.weather.vocabulary`
  module that is tested against the contract tables; `replace_readings`
  validates and inserts before it deletes; the tracker releases its lease on
  every exit path; malformed or out-of-range configuration is a clean exit-2
  error; disabled providers never block startup; quota validation counts the
  requests an adapter really makes (ims: one per station); station and city
  parameters can be given per location; forecasts report the provider's model
  run time (`model_run_at`, `issued_at_estimated`) and measure their horizon
  from now; the dashboard no longer draws past forecast points over history or
  lets a stale refresh overwrite a newer one. 90 SonarCloud maintainability
  issues were fixed by refactoring, none suppressed.
- `tests/weather/test_compose.py` imports PyYAML outright instead of `pytest.importorskip`, and `pyyaml` is a declared dev dependency — the compose data-safety assertions can no longer skip themselves away silently.

## [0.4.0] - 2026-09-17

### Added

- **Full eight-skill devague operator family**, re-learned from `devague learn`
  and synced **directly from devague `main` (0.24.1)**: `scope` → `think` →
  `challenge` → `spec-to-plan` → `assign-to-workforce` → `deviate` →
  `validate-delivery` → `summarize-delivery`. `validate-delivery` is new (run
  the confirmed plan's behavioral tests agent-side and file `devague evidence` /
  `devague delta` before the delivery summary); `scope`, `challenge`, `deviate`
  and `summarize-delivery` land with this release. Five are method-only by
  design (SKILL.md only, no `scripts/`); three keep their CLI resolver script.
  All verbatim except the tracked `agex` → `devex` rename in
  `assign-to-workforce`.
- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention so the vendored
  `remember` / `recall` skills are actually used: `/recall` before non-trivial
  work, `/remember` when a non-obvious decision, constraint, fix-and-why, or
  gotcha surfaces. Memory here is **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared).
- `climate explain climate` — the installed console-script name now resolves
  to the root explain entry, fixing the `explain_self` failure in the
  `teken cli doctor --strict` rubric gate (the lint job).

### Changed

- **Refreshed the stale devague skills** (`think`, `spec-to-plan`,
  `assign-to-workforce`) to devague 0.24.1 — enriched `plan waves` split plan
  with an End state section and `split-plan --write`, instructions carried to
  the workforce brief, obligations/evidence/deltas, and the eight-leg ordering
  (authoring order vs. flow order now stated explicitly).
- **Re-vendored `remember` + `recall` from eidetic-cli 0.14.1** — upstream now
  defaults the personal scope to `--visibility public` itself, so the former
  local "public-default policy override" is gone and both `SKILL.md` files
  match the wrappers (visibility-aware routing: public → `<repo>/.eidetic/memory`,
  private → `$HOME/.eidetic/memory`). Also picks up the opt-in `--rerank` stage
  and the lobes gateway embed endpoint (`:8001`). Localized only in the
  `--scope climate-cli` examples.
- `docs/skill-sources.md` now records all eight devague skills, `remember` and
  `recall` with their true origins, plus the direct-from-devague re-sync
  procedure; `README.md` drops the stale hard-coded skill count and uses the
  real `climate` console script in the quickstart.

### Fixed

- `remember.sh` no longer hangs on an interactive terminal when called with
  flags but no record (`remember.sh --json`): the stdin guard now checks for a
  JSON record argument instead of an empty argument list.

## [0.3.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `~/.eidetic/memory` surface, so this agent (Claude and its colleague backend)
  can persist facts across sessions and recall them later, sharing one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/climate-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `climate/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=climate` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/climate-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/climate-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: climate-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
