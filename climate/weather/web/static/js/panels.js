/**
 * The non-chart panels: the now band, the collection strip, the credits and
 * the page-level states.
 *
 * Every string that comes from the API — a provider title, a warning
 * message, an attribution line — is inserted with `textContent`, never by
 * concatenating markup.
 */

import {
  KIND_LABEL,
  compassOf,
  formatAge,
  formatPercent,
  formatValue,
  labelOf,
  unitOf,
} from "./format.js";

/** The variable groups the now band shows, in reading order. */
export const TILE_GROUPS = Object.freeze([
  {
    id: "temperature",
    title: "Temperature",
    primary: ["temperature"],
    extras: ["apparent_temperature", "dew_point"],
  },
  { id: "humidity", title: "Humidity", primary: ["relative_humidity"], extras: ["dew_point"] },
  {
    id: "wind",
    title: "Wind",
    primary: ["wind_speed"],
    extras: ["wind_gust"],
    direction: "wind_direction",
  },
  {
    id: "pressure",
    title: "Pressure",
    primary: ["pressure_msl", "pressure_surface"],
    extras: [],
  },
  {
    id: "precipitation",
    title: "Precipitation",
    primary: ["precipitation"],
    extras: ["rain", "precipitation_probability"],
  },
  { id: "cloud", title: "Cloud", primary: ["cloud_cover"], extras: ["visibility"] },
  {
    id: "radiation",
    title: "Radiation",
    primary: ["shortwave_radiation"],
    extras: ["uv_index"],
  },
]);

const KIND_RANK = { observation: 0, model: 1, forecast: 2 };

/**
 * Pick the value this page shows for one variable.
 *
 * A real measurement outranks a model's value for the same instant
 * (contract section 4.2 forbids presenting a `model` value as a
 * measurement), a fresh value outranks a stale one, and a younger value
 * outranks an older one.
 */
export function pickValue(readings, variableId) {
  let best = null;
  for (const reading of readings || []) {
    const value = reading.values?.[variableId];
    if (value?.value === null || value?.value === undefined) continue;
    const candidate = { ...value, provider: reading.provider, readingKind: reading.kind };
    if (!best) {
      best = candidate;
      continue;
    }
    const rank = (item) => [
      item.stale ? 1 : 0,
      KIND_RANK[item.kind] === undefined ? 3 : KIND_RANK[item.kind],
      item.age_seconds === null ? Number.MAX_SAFE_INTEGER : item.age_seconds,
    ];
    const [a, b] = [rank(candidate), rank(best)];
    if (a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2])))) {
      best = candidate;
    }
  }
  return best;
}

function icon(kind) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", "0 0 16 16");
  node.setAttribute("class", "icon");
  node.setAttribute("aria-hidden", "true");
  const paths = {
    good: "M3.5 8.5l3 3 6-7",
    warning: "M8 2.5l6 11H2l6-11zM8 6.5v3.2M8 11.6v.1",
    serious: "M8 2.5l6 11H2l6-11zM8 6.5v3.2M8 11.6v.1",
    critical: "M4 4l8 8M12 4l-8 8",
    off: "M2.5 8a5.5 5.5 0 1011 0 5.5 5.5 0 10-11 0zM4.2 4.2l7.6 7.6",
  };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[kind] || paths.good);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.6");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  node.appendChild(path);
  return node;
}

/** A status chip. Colour never carries the meaning on its own. */
function chip(kind, label) {
  const node = document.createElement("span");
  node.className = `status status--${kind}`;
  node.appendChild(icon(kind));
  node.appendChild(document.createTextNode(label));
  return node;
}

function providerKey(colour) {
  const key = document.createElement("span");
  key.className = "key";
  key.style.background = colour;
  return key;
}

function arrow(degrees) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "wind-arrow");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  // Meteorological direction: the wind comes *from* `degrees`, so the arrow
  // points to where it is going.
  path.setAttribute("d", "M12 3.5l0 17M12 20.5l-4.2-5M12 20.5l4.2-5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.8");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("transform", `rotate(${degrees} 12 12)`);
  svg.appendChild(path);
  return svg;
}

/** The provider that actually measured a value, not the one that served it. */
function sourceOf(value) {
  return value.provenance?.provider ?? value.provider;
}

/** The first of a group's primary variables that any provider reports. */
function pickPrimary(readings, group) {
  for (const variableId of group.primary) {
    const chosen = pickValue(readings, variableId);
    if (chosen) return { chosen, chosenVariable: variableId };
  }
  return { chosen: null, chosenVariable: null };
}

/** The wind rose that rides alongside the figure, when the group has one. */
function directionNode(readings, group) {
  if (!group.direction) return null;
  const direction = pickValue(readings, group.direction);
  if (!direction || direction.value === null) return null;
  const wrap = document.createElement("span");
  wrap.className = "tile__direction";
  wrap.appendChild(arrow(direction.value));
  const compass = document.createElement("span");
  compass.textContent = `${compassOf(direction.value)} ${formatValue(direction.value, "deg")}°`;
  wrap.appendChild(compass);
  return wrap;
}

/** The hero figure: the number, its unit, and any direction rose. */
function tileValue(readings, group, chosen) {
  const value = document.createElement("p");
  value.className = "tile__value";
  const number = document.createElement("span");
  number.className = "tile__number";
  number.textContent = chosen ? formatValue(chosen.value, chosen.unit) : "—";
  value.appendChild(number);
  if (chosen && unitOf(chosen.unit)) {
    const unit = document.createElement("span");
    unit.className = "tile__unit";
    unit.textContent = unitOf(chosen.unit);
    value.appendChild(unit);
  }
  const direction = directionNode(readings, group);
  if (direction) value.appendChild(direction);
  return value;
}

/** The secondary readings under the figure, or `null` when there are none. */
function tileExtras(readings, group, chosenVariable) {
  const extras = document.createElement("ul");
  extras.className = "tile__extras";
  for (const variableId of group.extras) {
    if (variableId === chosenVariable) continue;
    const extra = pickValue(readings, variableId);
    if (!extra || extra.value === null) continue;
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = labelOf(variableId);
    const reading = document.createElement("b");
    reading.textContent = `${formatValue(extra.value, extra.unit)}${unitOf(extra.unit)}`;
    item.appendChild(name);
    item.appendChild(reading);
    extras.appendChild(item);
  }
  return extras.childElementCount ? extras : null;
}

/** The provenance line: who measured it, what kind of value, how old. */
function tileMeta(chosen, { colorOf, providerTitleOf, hasStore }) {
  const meta = document.createElement("p");
  meta.className = "tile__meta";
  if (!chosen) {
    const waiting = document.createElement("span");
    waiting.className = "tile__waiting";
    waiting.textContent = hasStore ? "No provider reports this" : "Waiting for the first fetch";
    meta.appendChild(waiting);
    return meta;
  }

  const source = document.createElement("span");
  source.className = "tile__source";
  source.appendChild(providerKey(colorOf(sourceOf(chosen))));
  source.appendChild(document.createTextNode(providerTitleOf(sourceOf(chosen))));
  meta.appendChild(source);

  const kind = document.createElement("span");
  kind.className = `tile__kind tile__kind--${chosen.kind}`;
  kind.textContent = KIND_LABEL[chosen.kind] || chosen.kind;
  meta.appendChild(kind);

  const age = document.createElement("span");
  age.className = "tile__age";
  age.textContent = formatAge(chosen.age_seconds);
  meta.appendChild(age);

  if (chosen.stale) meta.appendChild(chip("warning", "Stale"));
  return meta;
}

function tileStateClass(chosen) {
  if (!chosen) return " tile--empty";
  return chosen.stale ? " tile--stale" : "";
}

/** One tile of the now band. */
function renderTile(group, { readings, colorOf, providerTitleOf, hasStore }) {
  const { chosen, chosenVariable } = pickPrimary(readings, group);
  const tile = document.createElement("li");
  tile.className = `tile${tileStateClass(chosen)}`;

  const heading = document.createElement("h3");
  heading.textContent = group.title;
  tile.appendChild(heading);
  tile.appendChild(tileValue(readings, group, chosen));
  const extras = tileExtras(readings, group, chosenVariable);
  if (extras) tile.appendChild(extras);
  tile.appendChild(tileMeta(chosen, { colorOf, providerTitleOf, hasStore }));
  return tile;
}

/** Render the now band. */
export function renderTiles(container, { readings, colorOf, providerTitleOf, hasStore }) {
  container.textContent = "";
  for (const group of TILE_GROUPS) {
    container.appendChild(renderTile(group, { readings, colorOf, providerTitleOf, hasStore }));
  }
}

/** Render the collection-health strip from `/stats` and `/health`. */
export function renderHealth(container, { rows, colorOf, providerTitleOf }) {
  container.textContent = "";
  if (!rows.length) {
    container.appendChild(emptyNote("No provider is registered."));
    return;
  }
  for (const row of rows) {
    const line = document.createElement("div");
    line.className = "health__row";
    if (!row.enabled) line.classList.add("health__row--off");

    const name = document.createElement("div");
    name.className = "health__name";
    name.appendChild(providerKey(row.enabled ? colorOf(row.provider) : "var(--chart-muted)"));
    const title = document.createElement("span");
    title.textContent = providerTitleOf(row.provider);
    name.appendChild(title);
    line.appendChild(name);

    const meter = document.createElement("div");
    meter.className = "meter";
    meter.setAttribute("role", "img");
    meter.setAttribute(
      "aria-label",
      row.completeness === null
        ? `${row.provider}: nothing due in this window`
        : `${row.provider}: ${formatPercent(row.completeness)} of due fetches stored`,
    );
    const fill = document.createElement("div");
    fill.className = `meter__fill meter__fill--${row.meterSeverity}`;
    fill.style.width = `${Math.min(100, Math.round((row.completeness || 0) * 100))}%`;
    meter.appendChild(fill);
    line.appendChild(meter);

    const figure = document.createElement("div");
    figure.className = "health__figure";
    figure.textContent = formatPercent(row.completeness);
    line.appendChild(figure);

    const counts = document.createElement("div");
    counts.className = "health__counts";
    counts.textContent = `${row.stored_count} / ${row.due_count}`;
    if (row.due_estimated) counts.title = "Due count is estimated, not exact";
    line.appendChild(counts);

    const age = document.createElement("div");
    age.className = "health__age";
    age.textContent = row.newest_fetch_age_seconds === null ? "never" : formatAge(row.newest_fetch_age_seconds);
    line.appendChild(age);

    const state = document.createElement("div");
    state.className = "health__state";
    state.appendChild(chip(row.severity, row.stateLabel));
    if (row.detail) {
      const detail = document.createElement("span");
      detail.className = "health__detail";
      detail.textContent = row.detail;
      state.appendChild(detail);
    }
    line.appendChild(state);

    container.appendChild(line);
  }
}

/**
 * Fold `/stats`, `/health` and `/providers` into one row per provider.
 *
 * `/stats` returns one row per (provider, location); the page is scoped to
 * one label, so rows for other labels are dropped rather than summed.
 */
export function healthRows({ stats, health, providers, location }) {
  const statRows = new Map();
  for (const row of stats?.providers || []) {
    if (row.location !== null && row.location !== location) continue;
    statRows.set(row.provider, row);
  }
  const healthRowsById = new Map((health?.providers || []).map((row) => [row.provider, row]));
  const disabledReason = new Map(
    (providers?.providers || []).map((row) => [row.provider, row.enabled_reason]),
  );

  return (providers?.providers || []).map((row) => {
    const stat = statRows.get(row.provider) || {};
    const live = healthRowsById.get(row.provider) || {};
    const errors =
      (stat.client_error_count || 0) +
      (stat.rate_limited_count || 0) +
      (stat.server_error_count || 0) +
      (stat.transport_error_count || 0);

    let severity = "good";
    let stateLabel = "Collecting";
    let detail = "";
    if (!row.enabled) {
      severity = "off";
      stateLabel = "Disabled";
      detail = disabledReason.get(row.provider) || "";
    } else if ((stat.stored_count || 0) === 0) {
      severity = "warning";
      stateLabel = "No fetch yet";
    } else if (live.stale) {
      severity = "warning";
      stateLabel = "Stale";
    } else if (errors > 0) {
      severity = "serious";
      stateLabel = errors === 1 ? "1 failed fetch" : `${errors} failed fetches`;
    }

    const ratio = stat.completeness === undefined || stat.completeness === null ? null : stat.completeness;
    let meterSeverity = "good";
    if (!row.enabled || ratio === null) meterSeverity = "off";
    else if (ratio < 0.8) meterSeverity = "critical";
    else if (ratio < 0.95) meterSeverity = "warning";

    return {
      provider: row.provider,
      meterSeverity,
      enabled: row.enabled,
      due_count: stat.due_count || 0,
      stored_count: stat.stored_count || 0,
      completeness: stat.completeness === undefined ? null : stat.completeness,
      due_estimated: Boolean(stat.due_estimated),
      newest_fetch_age_seconds:
        stat.newest_fetch_age_seconds === undefined ? null : stat.newest_fetch_age_seconds,
      severity,
      stateLabel,
      detail,
    };
  });
}

/** Credits for every provider whose data is on screen. */
export function renderCredits(container, { providers, onScreen }) {
  container.textContent = "";
  const rows = (providers?.providers || []).filter((row) => onScreen.has(row.provider));
  if (!rows.length) {
    container.appendChild(emptyNote("Nothing on screen yet, so nothing to credit."));
    return;
  }
  for (const row of rows) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = row.attribution.url;
    link.rel = "noopener noreferrer external";
    link.target = "_blank";
    link.textContent = row.attribution.text;
    item.appendChild(link);
    if (row.attribution.licence) {
      const licence = document.createElement("span");
      licence.className = "credits__licence";
      licence.textContent = row.attribution.licence;
      item.appendChild(licence);
    }
    container.appendChild(item);
  }
}

function emptyNote(message) {
  const note = document.createElement("p");
  note.className = "muted empty-note";
  note.textContent = message;
  return note;
}

/**
 * The page-level state banner: unreachable service, unreachable store,
 * signed out, or the first hour of collection.
 */
export function renderPageState(container, state) {
  if (!state) {
    container.hidden = true;
    container.textContent = "";
    return;
  }
  container.hidden = false;
  container.textContent = "";
  container.className = `page-state page-state--${state.kind}`;
  const heading = document.createElement("h2");
  heading.textContent = state.title;
  container.appendChild(heading);
  const body = document.createElement("p");
  body.textContent = state.message;
  container.appendChild(body);
  if (state.hint) {
    const hint = document.createElement("p");
    hint.className = "page-state__hint";
    hint.textContent = state.hint;
    container.appendChild(hint);
  }
  if (state.reload) {
    const link = document.createElement("a");
    link.className = "page-state__reload";
    link.href = window.location.pathname + window.location.search;
    link.textContent = "Reload to sign in";
    container.appendChild(link);
  }
}

/** A short, factual note for a band head. Never a sentence of filler. */
export function bandNote(element, message) {
  element.textContent = message || "";
}
