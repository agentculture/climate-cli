# Weather dashboard — design record

The chart-first dashboard served at `/` by `climate/weather/web/`. This
file records what was decided and why, and how to bring the page up again
to look at it.

## Screenshots

| File | What it shows |
| --- | --- |
| `dashboard-light.png` | The full page at 1280 px in the light theme: now band, the past-and-forecast chart, the collection strip, the credits footer. One provider is stale, one has a failed fetch, three are disabled. |
| `dashboard-dark.png` | The same page and the same data at 1280 px with `prefers-color-scheme: dark`. |
| `dashboard-phone.png` | The same page at 390 px. No horizontal scroll; the tile grid, the chart and the collection strip all reflow. |
| `dashboard-empty.png` | The first-hour state, served from an empty store: outlined tiles, an axis-free plot with one line of explanation, `0 / 168` collection, and a footer that says there is nothing to credit yet. |

All four were taken against the dev server below, never against real data.

## The idea

The page measures the sky, and the design system it wears — AgentCulture's
"First light over the mesh" — *is* a sky at two hours. So the organising
device is a horizon.

Time runs left to right through the whole page and the one loud element is
the **now rule**: a single aurora-teal vertical line where the past stops.
Left of it is what was recorded, on the plain page surface. Right of it is
the forecast, drawn on the token file's own `--sky-glow` wash — the part of
the chart the sun has not reached yet is literally the part that has not
happened yet. Everything else is deliberately quiet: hairline grid, 2 px
strokes, one accent used only for interaction and that rule, no card
shadow anywhere except on the chart.

The past and the forecast share **one y-axis** and sit in two x-bands
separated by that rule. They are never blended and there is never a second
y-scale.

## Identity

`tokens.css` is consumed exactly as
`docs/adr/0001-culture-design-source.md` pinned it and is not edited. It
supplies every surface, ink, line, accent, sky wash, type role, easing and
the reduced-motion kill switch. `dashboard.css` adds only the layer tokens
has no opinion about: a chart layer.

Two deliberate departures from the org site's defaults, both because this
is an instrument panel rather than a page to read:

- the page title is set small (1.45 rem) instead of the `--ac-type-page-title`
  clamp — a dashboard leads with its data, not its name;
- no tracked-out uppercase eyebrows, even though `.eyebrow` exists in the
  token file. Every band already has a heading; a label above it would be
  decoration.

Type roles: Fraunces (`--font-display`) on the page title and the band
headings only. Albert Sans (`--font-body`) for everything else, including
every figure — a serif hero number reads as off-brand decoration. Neither
family is fetched; the token file's fallback stacks carry the page, because
a web-font request would be an external dependency the page must work
without anyway.

## Series colour — the AgentCulture palette was measured, not assumed

The categorical palette recorded in ADR 0001 (the seven `ColleagueTerminal`
swatches) was the first candidate. It was run through the dataviz skill's
validator against this dashboard's *own* two surfaces — `--surface` is
`#ffffff` in light and `#161b36` in dark — and it fails four hard gates in
both themes:

```text
node scripts/validate_palette.js \
  "#7fdcc9,#7fb3f2,#f2b774,#b49cf2,#f2789a,#9fd6a3,#e6cd7a" \
  --mode light --surface "#ffffff"

  [FAIL] Lightness band       four slots at L 0.82-0.85, outside 0.43-0.77
  [FAIL] Chroma floor         teal 0.094 and green 0.091 read as grey
  [FAIL] CVD separation       yellow vs green dE 5.1 (protan), below the 6 floor
  [FAIL] Normal-vision floor  yellow vs green dE 9.4, below the hard 15 gate
  [WARN] Contrast vs surface  all seven below 3:1 on white
```

The dark run against `#161b36` fails the same four checks. That is not a
defect in the palette — it is a set of seven pale swatches tuned for one
fixed dark terminal ground (`#10142b`), used there for a category stripe and
a dot, never as a line-chart scale.

So series colour uses the **dataviz skill's validated default categorical
palette**, which passes every gate on both of this page's surfaces (worst
adjacent CVD dE 9.1 light / 8.4 dark; worst normal-vision dE 19.6 / 19.3).
Three light-mode slots sit below 3:1 on white, so the relief rule applies
and is honoured: every series is direct-labelled at its last value and every
plotted value is also in the table view. The AgentCulture identity carries
the whole of the rest of the page.

Slots are assigned over the **enabled** providers in the order
`GET /providers` returns them, so a colour belongs to a provider and not to
its rank: filtering the view or reordering it never repaints a series. Only
the operator enabling or disabling a provider changes an assignment.

Status colours are the fixed dataviz status scale, never reused for a
series, and always shipped as an icon plus a word, so the colour never
carries the meaning alone.

## Kind — solid, dotted, dashed

The three kinds wear org's own diagram semantic, recorded verbatim in
culture-nodes' `web/src/culture-design/edges.ts`:

| Kind | Stroke | Marker | Why |
| --- | --- | --- | --- |
| `observation` | solid 2 px | filled dot | SOLID is state something actually put on the record — an instrument measured it |
| `model` | dotted `2 7` | filled square | DOTTED is a reference carrying no authority of its own — a model's value for a time nobody measured |
| `forecast` | dashed `9 7` | none | DASHED is a proposal nobody has confirmed — a claim about a time that has not happened |

Colour is never the only channel: stroke and marker say the same thing, and
the legend names every series.

## Honesty rules the page keeps

- A `null` point in `/series` breaks the line. A run of one non-null point
  renders as a dot, never as a segment drawn through a gap.
- A provider whose own interval is coarser than the grid legitimately
  produces mostly-null series; the window's `step` is chosen to match the
  providers' cadences (900 s / 1800 s / 3600 s for 6 h / 24 h / 7 d) and
  whatever is still sparse is drawn as points.
- A stale value keeps its tile but loses its ink weight and gains a
  "Stale" chip with its age.
- A disabled provider stays in the collection strip with its reason, and
  takes no series colour.
- The first hour looks intentional: outlined tiles, a plot with no
  meaningless 0-to-1 axis, and the honest `0 / 168` count.
- Every provider whose data is on screen is credited in the footer from
  `GET /providers`, using its `attribution.text` linked to
  `attribution.url` — the one place on the page where prose is the point.
- Times arrive UTC and are shown in the browser's own zone, which the now
  band names once. No local time is ever sent back.

## Accessibility

- Both themes are WCAG AA per the token file's own contrast contract; the
  chart layer was validated separately against each surface.
- The plot is focusable: `ArrowLeft` / `ArrowRight` move the readout, and
  focus shows exactly what hover shows. `Escape` dismisses it.
- The chart carries an `aria-label` summarising each series' range, and a
  `Data table` disclosure holds every plotted value.
- Meters carry an `aria-label` with their figure; status is icon plus word.
- Refresh is every 60 s and holds the previous render at reduced opacity —
  no skeleton, no layout jump. The opacity change is the only motion on the
  page and is gated behind `prefers-reduced-motion: no-preference` on top
  of the token file's kill switch.

## Running it

There is no build step. The page is `index.html`, one stylesheet and five ES
modules, served straight from `climate/weather/web/static/`.

To look at it without MongoDB or docker, use the dev harness. It builds an
`InMemoryWeatherStore`, replays `tests/fixtures/` through the real adapters'
`normalize()`, time-shifts the readings over the last 26 hours, and serves
the result with the real `create_server`:

```bash
uv run python scripts/dev_dashboard_server.py            # a free port
uv run python scripts/dev_dashboard_server.py --port 8123
uv run python scripts/dev_dashboard_server.py --empty    # the first-hour state
```

It prints the URL it bound to, and refuses port 8095 so it can never be
mistaken for the real service. The replayed data deliberately contains a
one-hour collection gap, one failed fetch, one provider three hours stale
and three providers disabled.

`tests/weather/web/test_dashboard_static.py` pins what a browser would not
catch: the file set, that nothing loads from a third-party origin, that
every `/api/v1` path the JavaScript names is one `docs/weather-api.md`
documents, and that no static file carries a coordinate or a place name.

## Known gaps behind this page

- `GET /providers` returns `capabilities.variables: []` (a gap recorded in
  `climate/weather/web/api.py`), so the page cannot ask which provider
  supplies which variable. It discovers that from the readings it actually
  gets, which is why a variable with no data shows an explicit note rather
  than being hidden from the tab row.
- `providers[].title` is derived from the provider id, so legends read
  "Met No" and "Metar". The dashboard renders the contract's `title` field
  as given rather than second-guessing it.
- The `openweather` adapter emits variable ids and unit ids outside the
  contract's vocabulary, so the dev harness does not replay it. See
  `SKIPPED_PROVIDERS` in `scripts/dev_dashboard_server.py`.
