# ADR 0001: weather-tracking web design tokens sourced from agentculture/org, pinned commit

- **Status:** accepted
- **Date:** 2026-09-17
- **Task:** t7 (weather-tracking-service implementation plan, task t7)

## Context

The weather-tracking web dashboard (`climate/weather/web/`) needs to carry
the AgentCulture design system rather than invent its own — the org site is
the canonical visual identity for everything in the AgentCulture family,
including this product. The org site (`/home/spark/git/org`, an Astro
project at `site-astro/`) is developed independently of climate-cli and has
no versioned package we can depend on; it is a sibling repo on this
machine, checked out read-only for this purpose.

This follows the precedent set by `agentculture/culture-nodes`
(`docs/adr/0001-culture-design-source.md` there): rather than take a live
dependency on a moving sibling repo, this ADR records a **point-in-time
extraction** of specific files, copied at one pinned commit and checked
into this repo under `climate/weather/web/static/`. The org repo is never
modified by climate-cli work, and climate-cli never imports from
`/home/spark/git/org` at build or run time — everything climate-cli needs
is copied here.

## Pinned commit

```text
org repo:  /home/spark/git/org  (agentculture/org)
pin:       b4d939ba0aa354a5ae53065319a773e0013de698
```

Obtained with:

```bash
git -C /home/spark/git/org rev-parse HEAD
```

at the time task t7 was executed (2026-09-17).

## What was extracted

| File | Extracted from (at the pin) | What it carries |
| --- | --- | --- |
| `climate/weather/web/static/tokens.css` | `site-astro/src/styles/global.css` | The full design-token contract, copied **verbatim** below a header comment naming the pin: color tokens (light + `@media (prefers-color-scheme: dark)` overrides), the mesh palette (`--mesh-node`, `--mesh-thread`, `--mesh-halo*`), type scale/leading/tracking roles, layout rail widths, motion easings, and the reduced-motion kill switch. Framework-agnostic CSS custom properties — no Astro-specific syntax. |

Nothing else from the org repo was copied for this task. In particular, no
fonts, no JavaScript/hydration behavior, no Astro components, and no page
content.

### Categorical palette (for the dashboard task to evaluate)

culture-nodes (`web/src/culture-design/palette.ts`, itself extracted from
org's `site-astro/src/components/ColleagueTerminal.astro` at the same pin)
records the following 7-color categorical palette plus a shared neutral
fallback. It is reproduced here so the weather-tracking dashboard task can
evaluate reusing the same categorical set for its own per-series / per-alert
identity, without having to re-derive it from culture-nodes or org:

| Name | Hex |
| --- | --- |
| teal | `#7fdcc9` |
| blue | `#7fb3f2` |
| amber | `#f2b774` |
| violet | `#b49cf2` |
| pink | `#f2789a` |
| green | `#9fd6a3` |
| yellow | `#e6cd7a` |
| neutral | `#a9b0cf` |

culture-nodes also records a fixed, theme-invariant "terminal ground" set
(background `#10142b`, ink `#e9ecf8`, ink-soft `#a9b0cf`, ink-faint
`#8790b8`, body `#c7cde8`, border `rgba(233, 236, 248, 0.12)`) used for
dark-backdrop surfaces such as run-log/evidence panels. These are recorded
here for reference only; this task does not consume them — climate-cli
copies only `tokens.css` (see above). Whether/how the dashboard task adopts
either set is left to that task's own judgment.

## Re-pin procedure

When the org design system changes in a way climate-cli should pick up:

1. `git -C /home/spark/git/org rev-parse HEAD` to get the new commit.
2. Re-copy `site-astro/src/styles/global.css` into
   `climate/weather/web/static/tokens.css`, keeping the header comment but
   updating its `Pinned commit:` line to the new hash. The copied body
   below the header must stay byte-identical to the org source — do not
   hand-edit it.
3. Update the pinned commit hash in this ADR (the "Pinned commit" section
   and the extraction table above).
4. If the dashboard task's use of the categorical palette above needs to
   track org/culture-nodes drift, re-check
   `culture-nodes/web/src/culture-design/palette.ts` (or org's
   `ColleagueTerminal.astro` directly) and update the table in this ADR.
5. Run `uv run python scripts/check-culture-design.py` — it re-derives the
   pin from this ADR, re-fetches the org source at that pin via
   `git -C /home/spark/git/org show <pin>:<path>`, and fails loudly if
   `tokens.css` no longer byte-matches.

The script never mutates the org checkout and never assumes org's working
tree — it always reads through `git show <pin>:<path>`, so it verifies
against the exact pinned revision even if `/home/spark/git/org`'s HEAD has
since moved on. When the org checkout (or the pinned commit within it) is
unavailable — for example, in CI, which has no `/home/spark/git/org`
checkout — the script prints a clear "skipped" message and exits 0 rather
than failing.

## License note

The org repo (`/home/spark/git/org`) is licensed Apache License 2.0 (see
its `LICENSE` file). Apache-2.0 grants a copyright license broad enough to
cover copying these source files into climate-cli, including the required
verbatim-notice handling for `tokens.css`. climate-cli is MIT-licensed
(see this repo's `LICENSE`); the copied file itself is reproduced verbatim
under Apache-2.0's grant and retains its own header comment noting its
origin and pin.

**Apache-2.0 does not grant any trademark license** (License §6,
"Trademarks"). "AgentCulture" as a name/brand is not licensed for use as a
trademark by this extraction — this ADR covers reuse of the *code* (CSS
custom properties and color values), not permission to represent
climate-cli as an official AgentCulture product.

## Dark mode

`tokens.css`'s dark values live entirely under
`@media (prefers-color-scheme: dark)` — there is no light/dark toggle
anywhere in the org site, and none is introduced here. climate-cli's
consumption of `tokens.css` follows the same rule: dark mode is derived
purely from the visitor's OS/browser preference, never from an
application-level toggle, unless a future ADR explicitly revisits this.

## Consequences

- `climate/weather/web/static/tokens.css` has no live dependency on
  `/home/spark/git/org` at build or run time; it is fully self-contained
  once copied.
- Drift between org and climate-cli's copy is possible and expected over
  time; `scripts/check-culture-design.py` catches only drift in
  `tokens.css` against the *recorded* pin — it does not warn when org's
  HEAD moves further. Picking up new upstream design changes is a
  deliberate, manual re-pin (see above), not automatic.
- The categorical palette table above is a reference snapshot for the
  dashboard task; it is not independently verified by
  `scripts/check-culture-design.py` (which checks `tokens.css` only). If a
  future task consumes those hex values directly in this repo, that task
  should extend the check accordingly, following culture-nodes'
  `palette.ts` precedent.
