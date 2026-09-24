/**
 * The dashboard's one loop: read the documented routes, fold them into a
 * view model, draw it, and do it again in a minute.
 *
 * Nothing here holds state the API does not hand it, so a refresh is always
 * a redraw of what the service currently says — never a merge with
 * something remembered.
 *
 * Exactly one refresh owns the page at a time. Every request of a refresh
 * carries that refresh's abort signal, and nothing it read is allowed to
 * touch state, the drawing, the error banner or the busy flag once a newer
 * refresh has taken over.
 */

import { ApiError, get } from "./api.js";
import { describeChart, renderChart, renderTable } from "./chart.js";
import { KIND_LABEL, LOCAL_ZONE, formatAge, labelOf, parseTime, unitOf } from "./format.js";
import {
  bandNote,
  healthRows,
  renderCredits,
  renderHealth,
  renderPageState,
  renderTiles,
} from "./panels.js";
import { colorScale, kindStyle } from "./series.js";

const REFRESH_MS = 60_000;

/** The windows the reader can choose, and the grid each one asks for. */
const WINDOWS = Object.freeze({
  "6h": { seconds: 6 * 3600, step: 900, stats: "6h" },
  "24h": { seconds: 24 * 3600, step: 1800, stats: "24h" },
  "7d": { seconds: 7 * 86400, step: 3600, stats: "7d" },
});

/** The variables the comparison chart offers, in reading order. */
const CHART_VARIABLES = Object.freeze([
  "temperature",
  "relative_humidity",
  "wind_speed",
  "pressure_msl",
  "precipitation",
  "cloud_cover",
  "shortwave_radiation",
]);

const FORECAST_HORIZON_HOURS = 48;

const SVG_NS = "http://www.w3.org/2000/svg";

const dom = {
  status: document.getElementById("service-status"),
  statusText: document.querySelector('[data-role="status-text"]'),
  locationSelect: document.getElementById("location-select"),
  windowControl: document.getElementById("window-control"),
  pageState: document.getElementById("page-state"),
  tiles: document.getElementById("tiles"),
  nowNote: document.querySelector('[data-role="now-note"]'),
  trendTitle: document.querySelector('[data-role="trend-title"]'),
  trendNote: document.querySelector('[data-role="trend-note"]'),
  variableTabs: document.getElementById("variable-tabs"),
  legend: document.getElementById("chart-legend"),
  plot: document.getElementById("chart-plot"),
  table: document.getElementById("chart-table"),
  health: document.getElementById("health"),
  collectionNote: document.querySelector('[data-role="collection-note"]'),
  credits: document.getElementById("credits-list"),
};

const state = {
  location: null,
  window: "24h",
  variable: "temperature",
  providers: null,
  lastSpec: null,
  inFlight: null,
  refreshTimer: null,
};

/** Stop the minute-poll. Called once, when a signed-out response arrives. */
function stopPolling() {
  if (state.refreshTimer === null) return;
  window.clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

function isoZ(date) {
  return `${date.toISOString().slice(0, 19)}Z`;
}

function setBusy(busy) {
  document.body.classList.toggle("is-refreshing", busy);
}

function setStatus(kind, message) {
  dom.status.querySelector(".dot").className = `dot dot--${kind}`;
  dom.statusText.textContent = message;
}

/** A contract value as a number, or `null` when the API sent none. */
function numberOrNull(value) {
  return value === null || value === undefined ? null : Number(value);
}

/** Build the chart's past series from a `/series` response. */
function pastSeries(body, colorOf, titleOf) {
  return (body.series || [])
    .filter((entry) => entry.kind !== "forecast")
    .map((entry) => ({
      key: `${entry.provider}-${entry.kind}`,
      provider: entry.provider,
      kind: entry.kind,
      label: `${titleOf(entry.provider)} ${(KIND_LABEL[entry.kind] || entry.kind).toLowerCase()}`,
      color: colorOf(entry.provider),
      points: (entry.points || []).map((point) => ({
        t: parseTime(point.t),
        v: numberOrNull(point.value),
      })),
    }))
    .filter((series) => series.points.every((point) => point.t));
}

/**
 * Build the chart's future series from a `/forecast` response.
 *
 * A forecast run can carry points whose valid time has already passed —
 * the provider issued them before now. They belong to the history, which
 * the page reads from `/series`, so they are dropped here: the future band
 * never shows a time that is not in the future, and the y-extent is taken
 * over what is actually drawn.
 */
function futureSeries(body, variable, colorOf, titleOf, now) {
  const notBefore = now.getTime();
  return (body.forecasts || [])
    .map((entry) => ({
      key: `${entry.provider}-forecast`,
      provider: entry.provider,
      kind: "forecast",
      label: `${titleOf(entry.provider)} forecast`,
      color: colorOf(entry.provider),
      points: (entry.points || [])
        .map((point) => ({
          t: parseTime(point.valid_at),
          v: numberOrNull(point.values?.[variable]),
        }))
        .filter((point) => point.t?.getTime() >= notBefore),
    }))
    .filter((series) => series.points.some((point) => point.v !== null));
}

function legendSwatch(series) {
  const style = kindStyle(series.kind);
  const swatch = document.createElementNS(SVG_NS, "svg");
  swatch.setAttribute("viewBox", "0 0 28 10");
  swatch.setAttribute("class", "legend__swatch");
  swatch.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(SVG_NS, "line");
  line.setAttribute("x1", "1");
  line.setAttribute("x2", "27");
  line.setAttribute("y1", "5");
  line.setAttribute("y2", "5");
  line.setAttribute("stroke", series.color);
  line.setAttribute("stroke-width", String(style.width));
  line.setAttribute("stroke-linecap", "round");
  if (style.dash) line.setAttribute("stroke-dasharray", style.dash);
  swatch.appendChild(line);
  if (style.marker === "dot") {
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", "14");
    dot.setAttribute("cy", "5");
    dot.setAttribute("r", "3");
    dot.setAttribute("fill", series.color);
    swatch.appendChild(dot);
  } else if (style.marker === "square") {
    const square = document.createElementNS(SVG_NS, "rect");
    square.setAttribute("x", "11");
    square.setAttribute("y", "2");
    square.setAttribute("width", "6");
    square.setAttribute("height", "6");
    square.setAttribute("fill", series.color);
    swatch.appendChild(square);
  }
  return swatch;
}

function renderLegend(container, seriesList) {
  container.textContent = "";
  for (const series of seriesList) {
    const item = document.createElement("span");
    item.className = "legend__item";
    item.appendChild(legendSwatch(series));
    item.appendChild(document.createTextNode(series.label));
    container.appendChild(item);
  }
}

function renderVariableTabs() {
  dom.variableTabs.textContent = "";
  for (const variable of CHART_VARIABLES) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "variable-tab";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(variable === state.variable));
    tab.textContent = labelOf(variable);
    tab.addEventListener("click", () => {
      if (state.variable === variable) return;
      state.variable = variable;
      renderVariableTabs();
      refresh();
    });
    dom.variableTabs.appendChild(tab);
  }
}

function warningsFor(bodies, code) {
  const out = [];
  for (const body of bodies) {
    for (const warning of body?.warnings || []) {
      if (!code || warning.code === code) out.push(warning);
    }
  }
  return out;
}

/** The three shape reads, all carrying this refresh's signal. */
async function loadShape(signal) {
  const [health, providers, locations] = await Promise.all([
    get("health", null, { signal }),
    get("providers", null, { signal }),
    get("locations", null, { signal }),
  ]);
  return { health, providers, locations };
}

function fillLocations(locations) {
  const labels = (locations.locations || []).map((row) => row.location);
  if (!state.location || !labels.includes(state.location)) {
    state.location = labels[0] || null;
  }
  const select = dom.locationSelect;
  const current = state.location;
  select.textContent = "";
  for (const label of labels) {
    const option = document.createElement("option");
    option.value = label;
    option.textContent = label;
    if (label === current) option.selected = true;
    select.appendChild(option);
  }
  select.disabled = labels.length < 2;
  if (!labels.length) {
    const option = document.createElement("option");
    option.textContent = "none configured";
    select.appendChild(option);
  }
}

function isAbort(error) {
  return error?.name === "AbortError";
}

/** The four windowed reads, all carrying this refresh's signal. */
function loadView({ location, win, from, signal }) {
  return Promise.all([
    get("latest", { location }, { signal }),
    get(
      "series",
      {
        variable: state.variable,
        location,
        from: isoZ(from),
        step: win.step,
        kind: ["observation", "model"],
      },
      { signal },
    ),
    get(
      "forecast",
      { location, variables: state.variable, horizon_hours: FORECAST_HORIZON_HOURS },
      { signal },
    ),
    get("stats", { window: win.stats, location }, { signal }),
  ]);
}

async function refresh() {
  if (state.inFlight) state.inFlight.abort();
  const controller = new AbortController();
  state.inFlight = controller;
  const isCurrent = () => state.inFlight === controller;
  setBusy(true);

  try {
    const shape = await loadShape(controller.signal);
    if (!isCurrent()) return;
    state.providers = shape.providers;
    fillLocations(shape.locations);

    const now = parseTime(shape.health.generated_at) || new Date();
    const win = WINDOWS[state.window];
    const from = new Date(now.getTime() - win.seconds * 1000);
    const location = state.location;
    const colorOf = colorScale(shape.providers.providers);
    const titleOf = (id) =>
      (shape.providers.providers || []).find((item) => item.provider === id)?.title || id;

    const [latest, series, forecast, stats] = await loadView({
      location,
      win,
      from,
      signal: controller.signal,
    });
    if (!isCurrent()) return;

    draw({ shape, latest, series, forecast, stats, now, from, colorOf, titleOf });
  } catch (error) {
    if (isAbort(error) || !isCurrent()) return;
    handleFailure(error);
  } finally {
    if (isCurrent()) {
      state.inFlight = null;
      setBusy(false);
    }
  }
}

/** What the page says about one failed refresh. One branch, one shape. */
function failureState(error) {
  const message = error?.message || String(error);
  if (error instanceof ApiError && error.signedOut) {
    return {
      status: "Signed out",
      kind: "signed-out",
      title: "Signed out",
      message: "This dashboard's Access session has expired.",
      hint: "",
      reload: true,
    };
  }
  if (error instanceof ApiError && error.unreachable) {
    return {
      status: "Not answering",
      kind: "unreachable",
      title: "The weather service is not answering",
      message: "Nothing was read, so nothing below is current. This page keeps trying once a minute.",
      hint: "Start it with climate stack up, then this page will pick up again by itself.",
    };
  }
  if (error instanceof ApiError && error.code === "store_unavailable") {
    return {
      status: "Degraded",
      kind: "error",
      title: "The store is unreachable",
      message,
      hint: "The service is up; its database is not. Nothing was lost — collection resumes when the store comes back.",
    };
  }
  return {
    status: "Degraded",
    kind: "error",
    title: "That request did not work",
    message,
    hint: "",
  };
}

function handleFailure(error) {
  const view = failureState(error);
  setStatus("down", view.status);
  renderPageState(dom.pageState, {
    kind: view.kind,
    title: view.title,
    message: view.message,
    hint: view.hint,
    reload: Boolean(view.reload),
  });
  if (view.kind === "signed-out") stopPolling();
}

/** The banner and the service dot: store down, first hour, or running. */
function renderServiceState({ shape, bodies, nothingStored }) {
  if (shape.health.store?.reachable === false) {
    setStatus("down", "Store unreachable");
    renderPageState(dom.pageState, {
      kind: "error",
      title: "The store is unreachable",
      message: "The service is answering, but its database is not, so there is nothing to read.",
      hint: "Collection resumes by itself when the store comes back.",
    });
    return;
  }
  if (nothingStored) {
    const reasons = warningsFor(bodies).map((warning) => warning.message);
    setStatus("idle", "Waiting for data");
    renderPageState(dom.pageState, {
      kind: "empty",
      title: "Nothing stored for this location yet",
      message:
        reasons[0] ||
        "The tracker has not recorded a fetch for this label. The first values appear as soon as one provider is polled.",
      hint: "",
    });
    return;
  }
  const newest = shape.health.newest_fetch || {};
  const age = newest.age_seconds;
  setStatus(
    shape.health.status === "ok" ? "ok" : "warn",
    age === null || age === undefined ? "No fetch recorded" : `Newest fetch ${formatAge(age)} ago`,
  );
  renderPageState(dom.pageState, null);
}

function renderNowBand({ shape, readings, colorOf, titleOf, nothingStored }) {
  renderTiles(dom.tiles, {
    readings,
    colorOf,
    providerTitleOf: titleOf,
    hasStore: !nothingStored,
  });
  const reporting = new Set(readings.map((reading) => reading.provider)).size;
  const total = (shape.providers.providers || []).length;
  bandNote(
    dom.nowNote,
    nothingStored
      ? "No provider has reported yet"
      : `${reporting} of ${total} providers reporting, times in ${LOCAL_ZONE}`,
  );
}

function trendNoteFor({ past, future, series }) {
  const gaps = past.reduce(
    (total, entry) => total + entry.points.filter((point) => point.v === null).length,
    0,
  );
  const notes = [];
  if (!past.length) notes.push("no stored value for this variable in this window");
  else if (gaps) notes.push(`${gaps} grid points with no stored fetch, drawn as breaks`);
  if (!future.length) notes.push("no provider stores a forecast for this variable");
  if (warningsFor([series], "truncated").length > 0) notes.push("window truncated to the point limit");
  return notes.join("; ");
}

function renderTrendBand({ series, past, future, now, from }) {
  const unit = series.unit || "";
  const unitLabel = unitOf(unit);
  const title = labelOf(state.variable);
  dom.trendTitle.textContent = unitLabel ? `${title} in ${unitLabel}` : title;

  const spec = {
    title,
    unit,
    zone: LOCAL_ZONE,
    from,
    now,
    to: new Date(now.getTime() + FORECAST_HORIZON_HOURS * 3600 * 1000),
    past,
    future,
    futureLabel: `Next ${FORECAST_HORIZON_HOURS} h`,
  };
  spec.summary = describeChart(spec);
  state.lastSpec = spec;
  renderLegend(dom.legend, past.concat(future));
  renderChart(dom.plot, spec);
  renderTable(dom.table, spec);
  bandNote(dom.trendNote, trendNoteFor({ past, future, series }));
}

function renderCollectionBand({ shape, stats, colorOf, titleOf }) {
  const rows = healthRows({
    stats,
    health: shape.health,
    providers: shape.providers,
    location: state.location,
  });
  renderHealth(dom.health, { rows, colorOf, providerTitleOf: titleOf });
  const live = rows.filter((row) => row.enabled);
  const due = live.reduce((total, row) => total + row.due_count, 0);
  const stored = live.reduce((total, row) => total + row.stored_count, 0);
  bandNote(
    dom.collectionNote,
    due
      ? `${stored} of ${due} due fetches stored over ${state.window}, across ${live.length} enabled providers`
      : `Nothing due over ${state.window}`,
  );
}

function renderCreditsBand({ shape, readings, past, future }) {
  const onScreen = new Set([
    ...readings.map((reading) => reading.provider),
    ...past.map((entry) => entry.provider),
    ...future.map((entry) => entry.provider),
  ]);
  renderCredits(dom.credits, { providers: shape.providers, onScreen });
}

function draw({ shape, latest, series, forecast, stats, now, from, colorOf, titleOf }) {
  const past = pastSeries(series, colorOf, titleOf);
  const future = futureSeries(forecast, state.variable, colorOf, titleOf, now);
  const readings = latest.readings || [];
  const nothingStored = !readings.length && !past.length && !future.length;

  renderServiceState({ shape, bodies: [latest, series, forecast, stats], nothingStored });
  renderNowBand({ shape, readings, colorOf, titleOf, nothingStored });
  renderTrendBand({ series, past, future, now, from });
  renderCollectionBand({ shape, stats, colorOf, titleOf });
  renderCreditsBand({ shape, readings, past, future });
}

// --- wiring -----------------------------------------------------------------

dom.locationSelect.addEventListener("change", (event) => {
  state.location = event.target.value;
  refresh();
});

dom.windowControl.addEventListener("change", (event) => {
  if (event.target.name !== "window") return;
  state.window = event.target.value;
  refresh();
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  if (!state.lastSpec) return;
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => renderChart(dom.plot, state.lastSpec), 150);
});

renderVariableTabs();
state.refreshTimer = window.setInterval(refresh, REFRESH_MS);
await refresh();
