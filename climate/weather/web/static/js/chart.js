/**
 * The dashboard's one chart: past and forecast on a single shared y-axis,
 * split at now.
 *
 * Why a split and not two cards: the reader's question is "do the providers
 * agree, and where is it going" — one question, one y-scale. The seam at
 * `now` is the page's single loud element; the future band sits on the
 * token file's own dawn wash (`--sky-glow`), so the part of the chart the
 * sun has not reached yet is literally the part that has not happened yet.
 * There is exactly one y-axis; the two x-bands are separated by a visible
 * rule and labelled, never blended.
 *
 * Each band draws only what belongs to it: a point is plotted only when its
 * own band's scale places it inside that band's pixel domain, so a forecast
 * issued for a time already past can never be extrapolated left across the
 * seam and drawn over the history.
 *
 * Contract obligations honoured here (docs/weather-api.md section 8.1):
 * a `null` point is a break in the line, never an interpolated segment;
 * observation, model and forecast are drawn with distinct strokes and
 * markers as well as distinct colours; every value is also reachable from
 * the table view, so the tooltip only ever enhances.
 */

import { formatClock, formatDayClock, formatFull, formatValue, unitOf } from "./format.js";
import { kindStyle } from "./series.js";

const NS = "http://www.w3.org/2000/svg";

const MARGIN = { top: 14, right: 16, bottom: 26, left: 48 };
const SEAM_GAP = 10;
const FUTURE_FRACTION = 0.36;
const MIN_LABEL_GAP = 13;
/** Below this width the chart drops to the compact height and tick counts. */
const COMPACT_WIDTH = 560;
/** Past this span an axis stamp needs a weekday, not just a clock. */
const DAY_STAMP_SPAN_MS = 26 * 36e5;
/** Half a pixel of slack, so a point exactly on a bound still draws. */
const EDGE_SLACK = 0.5;
/** Arrow key -> how far the keyboard readout moves. */
const KEY_STEP = Object.freeze({ ArrowLeft: -1, ArrowRight: 1 });

function el(name, attrs) {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

function text(node, value) {
  node.textContent = value;
  return node;
}

/** The point's value, or `null` when the series has no value for it. */
function pointValue(point) {
  const value = point?.v;
  return value === null || value === undefined ? null : value;
}

/** The point's value when it is a real number, `null` otherwise. */
function finiteValue(point) {
  const value = pointValue(point);
  return Number.isFinite(value) ? value : null;
}

function niceTicks(min, max, count) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [0, 1], lo: 0, hi: 1 };
  if (min === max) {
    const pad = Math.abs(min) > 1 ? Math.abs(min) * 0.05 : 0.5;
    min -= pad;
    max += pad;
  }
  const raw = (max - min) / Math.max(2, count);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * magnitude);
  const step = candidates.find((value) => value >= raw) || candidates.at(-1);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let value = lo; value <= hi + step / 2; value += step) {
    ticks.push(Number(value.toFixed(6)));
  }
  return { ticks, lo, hi };
}

function timeTicks(from, to, count) {
  const span = to - from;
  if (!Number.isFinite(span) || span <= 0) return [];
  const steps = [
    5 * 6e4, 15 * 6e4, 30 * 6e4, 36e5, 2 * 36e5, 3 * 36e5, 6 * 36e5, 12 * 36e5,
    864e5, 2 * 864e5,
  ];
  const target = span / count;
  const step = steps.find((value) => value >= target) || steps.at(-1);
  const first = Math.ceil(from / step) * step;
  const out = [];
  for (let value = first; value <= to; value += step) out.push(new Date(value));
  return out;
}

/** Split a series into runs of consecutive non-null points. */
function runsOf(points) {
  const runs = [];
  let current = null;
  for (const point of points) {
    if (finiteValue(point) === null) {
      current = null;
      continue;
    }
    if (!current) {
      current = [];
      runs.push(current);
    }
    current.push(point);
  }
  return runs;
}

/** The last drawn point of a series, or `null` when it draws nothing. */
function lastDrawnPoint(series) {
  return runsOf(series.points).at(-1)?.at(-1) ?? null;
}

function extent(seriesList) {
  let min = Infinity;
  let max = -Infinity;
  for (const series of seriesList) {
    for (const point of series.points) {
      const value = finiteValue(point);
      if (value === null) continue;
      if (value < min) min = value;
      if (value > max) max = value;
    }
  }
  return Number.isFinite(min) ? [min, max] : null;
}

function bandScale(from, to, x0, x1) {
  const span = to - from || 1;
  return (date) => x0 + ((date - from) / span) * (x1 - x0);
}

function placeLabels(entries) {
  const sorted = entries.slice().sort((a, b) => a.y - b.y);
  for (let i = 1; i < sorted.length; i += 1) {
    const gap = sorted[i].y - sorted[i - 1].y;
    if (gap < MIN_LABEL_GAP) sorted[i].y = sorted[i - 1].y + MIN_LABEL_GAP;
  }
  return entries;
}

/**
 * Everything the drawing steps need: the box, the two x-bands with their
 * pixel domains, the shared y-scale and the tick set.
 */
function layoutOf(container, spec) {
  const width = Math.max(280, container.clientWidth || 640);
  const compact = width < COMPACT_WIDTH;
  const height = compact ? 236 : 300;
  const past = spec.past || [];
  const future = spec.future || [];
  const hasFuture = future.some((series) => runsOf(series.points).length > 0);

  const plotLeft = MARGIN.left;
  const plotRight = width - MARGIN.right;
  const plotTop = MARGIN.top;
  const plotBottom = height - MARGIN.bottom;
  const futureWidth = hasFuture ? (plotRight - plotLeft) * FUTURE_FRACTION : 0;
  const seam = plotRight - futureWidth;
  const pastEnd = hasFuture ? seam - SEAM_GAP / 2 : plotRight;
  const futureStart = seam + SEAM_GAP / 2;

  const pastFrom = spec.from.getTime();
  const nowValue = spec.now.getTime();
  const futureTo = spec.to.getTime();

  const span = extent(past.concat(hasFuture ? future : []));
  const { ticks: yTicks, lo, hi } = niceTicks(
    span ? span[0] : 0,
    span ? span[1] : 1,
    compact ? 3 : 5,
  );

  return {
    width,
    height,
    compact,
    past,
    future,
    hasFuture,
    plotLeft,
    plotRight,
    plotTop,
    plotBottom,
    seam,
    pastEnd,
    futureStart,
    pastFrom,
    nowValue,
    futureTo,
    hasValues: span !== null,
    yTicks,
    xPast: bandScale(pastFrom, nowValue, plotLeft, pastEnd),
    xFuture: bandScale(nowValue, futureTo, futureStart, plotRight),
    y: (value) => plotBottom - ((value - lo) / (hi - lo || 1)) * (plotBottom - plotTop),
  };
}

/** The future band: the part the sun has not reached. */
function drawFutureBand(svg, layout) {
  if (!layout.hasFuture) return;
  svg.appendChild(
    el("rect", {
      class: "chart__future",
      x: layout.futureStart,
      y: layout.plotTop,
      width: Math.max(0, layout.plotRight - layout.futureStart),
      height: layout.plotBottom - layout.plotTop,
      rx: 6,
    }),
  );
}

function drawGrid(svg, layout, spec) {
  for (const tick of layout.hasValues ? layout.yTicks : []) {
    const yy = layout.y(tick);
    if (yy < layout.plotTop - EDGE_SLACK || yy > layout.plotBottom + EDGE_SLACK) continue;
    svg.appendChild(
      el("line", { class: "chart__grid", x1: layout.plotLeft, x2: layout.plotRight, y1: yy, y2: yy }),
    );
    svg.appendChild(
      text(
        el("text", {
          class: "chart__tick chart__tick--y",
          x: layout.plotLeft - 8,
          y: yy + 4,
          "text-anchor": "end",
        }),
        formatValue(tick, spec.unit),
      ),
    );
  }
  svg.appendChild(
    el("line", {
      class: "chart__axis",
      x1: layout.plotLeft,
      x2: layout.plotRight,
      y1: layout.plotBottom,
      y2: layout.plotBottom,
    }),
  );
}

const stampFor = (span) => (span > DAY_STAMP_SPAN_MS ? formatDayClock : formatClock);

function drawTickRow(svg, layout, { ticks, scale, stamp, limit }) {
  for (const tick of ticks) {
    const xx = scale(tick.getTime());
    if (xx > limit) continue;
    svg.appendChild(
      text(
        el("text", {
          class: "chart__tick",
          x: xx,
          y: layout.plotBottom + 17,
          "text-anchor": "middle",
        }),
        stamp(tick),
      ),
    );
  }
}

function drawTimeAxis(svg, layout) {
  drawTickRow(svg, layout, {
    ticks: timeTicks(layout.pastFrom, layout.nowValue, layout.compact ? 2 : 4),
    scale: layout.xPast,
    stamp: stampFor(layout.nowValue - layout.pastFrom),
    limit: (layout.hasFuture ? layout.seam - SEAM_GAP : layout.plotRight) - 4,
  });
  if (!layout.hasFuture) return;
  drawTickRow(svg, layout, {
    ticks: timeTicks(layout.nowValue, layout.futureTo, layout.compact ? 1 : 2),
    scale: layout.xFuture,
    stamp: stampFor(layout.futureTo - layout.nowValue),
    limit: layout.plotRight - 12,
  });
}

/**
 * Draw one series inside one band.
 *
 * `clipFrom`/`clipTo` are that band's own pixel domain and BOTH are
 * enforced: a point the scale places outside the band — a forecast for a
 * time already past, or one beyond the horizon — is dropped rather than
 * drawn across the seam.
 */
function drawSeries(svg, layout, series, { scale, clipFrom, clipTo }) {
  const style = kindStyle(series.kind);
  const group = el("g", { class: "chart__series" });
  for (const run of runsOf(series.points)) {
    const coords = run
      .map((point) => [scale(point.t.getTime()), layout.y(point.v)])
      .filter(([px]) => px >= clipFrom - EDGE_SLACK && px <= clipTo + EDGE_SLACK);
    if (coords.length === 0) continue;
    if (coords.length === 1) {
      group.appendChild(
        el("circle", {
          class: "chart__point",
          cx: coords[0][0],
          cy: coords[0][1],
          r: 4,
          fill: series.color,
        }),
      );
      continue;
    }
    group.appendChild(
      el("path", {
        class: "chart__line",
        d: coords.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" "),
        stroke: series.color,
        "stroke-width": style.width,
        "stroke-dasharray": style.dash,
      }),
    );
  }
  svg.appendChild(group);
}

function drawAllSeries(svg, layout) {
  for (const series of layout.past) {
    drawSeries(svg, layout, series, {
      scale: layout.xPast,
      clipFrom: layout.plotLeft,
      clipTo: layout.pastEnd,
    });
  }
  if (!layout.hasFuture) return;
  for (const series of layout.future) {
    drawSeries(svg, layout, series, {
      scale: layout.xFuture,
      clipFrom: layout.futureStart,
      clipTo: layout.plotRight,
    });
  }
}

/** End markers on the past band; returns the direct-label anchors. */
function drawEndMarkers(svg, layout) {
  const labels = [];
  for (const series of layout.past) {
    const last = lastDrawnPoint(series);
    if (!last) continue;
    const px = layout.xPast(last.t.getTime());
    const py = layout.y(last.v);
    const marker =
      kindStyle(series.kind).marker === "square"
        ? el("rect", {
            class: "chart__end",
            x: px - 4,
            y: py - 4,
            width: 8,
            height: 8,
            fill: series.color,
          })
        : el("circle", { class: "chart__end", cx: px, cy: py, r: 4.5, fill: series.color });
    svg.appendChild(marker);
    labels.push({ x: px, y: py, value: last.v, anchor: py });
  }
  return labels;
}

function drawValueLabels(svg, layout, spec, labels) {
  const side = layout.hasFuture ? -1 : 1;
  for (const entry of placeLabels(labels)) {
    if (Math.abs(entry.y - entry.anchor) > 2) {
      svg.appendChild(
        el("line", {
          class: "chart__leader",
          x1: entry.x + side * 6,
          y1: entry.anchor,
          x2: entry.x + side * 11,
          y2: entry.y - 4,
        }),
      );
    }
    svg.appendChild(
      text(
        el("text", {
          class: "chart__value",
          x: entry.x + side * 13,
          y: entry.y,
          "text-anchor": layout.hasFuture ? "end" : "start",
          "dominant-baseline": "middle",
        }),
        formatValue(entry.value, spec.unit),
      ),
    );
  }
}

/** The seam: one rule at now, the page's single loud element. */
function drawSeam(svg, layout, spec) {
  if (!layout.hasFuture) return;
  svg.appendChild(
    el("line", {
      class: "chart__now",
      x1: layout.seam,
      x2: layout.seam,
      y1: layout.plotTop - 6,
      y2: layout.plotBottom + 4,
    }),
  );
  svg.appendChild(
    text(
      el("text", { class: "chart__seam-label", x: layout.seam + SEAM_GAP, y: layout.plotTop - 2 }),
      spec.futureLabel || "Forecast",
    ),
  );
}

function drawBlank(svg, layout, spec) {
  if (layout.hasValues) return;
  svg.appendChild(
    text(
      el("text", {
        class: "chart__blank",
        x: (layout.plotLeft + layout.plotRight) / 2,
        y: (layout.plotTop + layout.plotBottom) / 2,
        "text-anchor": "middle",
      }),
      spec.blankLabel || "Nothing stored for this window yet",
    ),
  );
}

// --- hover / focus readout ---------------------------------------------------

function instantsOf(seriesList) {
  return Array.from(
    new Set(seriesList.flatMap((series) => series.points.map((point) => point.t.getTime()))),
  ).sort((a, b) => a - b);
}

function createReadout(container, cursor, layout, spec) {
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  container.appendChild(tip);

  const all = layout.past.concat(layout.hasFuture ? layout.future : []);
  const instants = instantsOf(all);
  return {
    tip,
    cursor,
    layout,
    spec,
    all,
    instants,
    index: instants.length - 1,
    xAt: (value) => (value <= layout.nowValue ? layout.xPast(value) : layout.xFuture(value)),
  };
}

/** Fill the tip's definition list; returns how many series had a value. */
function fillTipRows(readout, list, instant) {
  let rows = 0;
  for (const series of readout.all) {
    const value = pointValue(series.points.find((item) => item.t.getTime() === instant));
    if (value === null) continue;
    const term = document.createElement("dt");
    const key = document.createElement("span");
    key.className = "chart-tip__key";
    key.style.background = series.color;
    term.appendChild(key);
    term.appendChild(document.createTextNode(series.label));
    const cell = document.createElement("dd");
    cell.textContent = `${formatValue(value, readout.spec.unit)} ${unitOf(readout.spec.unit)}`.trim();
    list.appendChild(term);
    list.appendChild(cell);
    rows += 1;
  }
  return rows;
}

function showAt(readout, instant) {
  const { tip, cursor, layout } = readout;
  const px = readout.xAt(instant);
  cursor.setAttribute("x1", px);
  cursor.setAttribute("x2", px);
  cursor.setAttribute("visibility", "visible");

  tip.textContent = "";
  const when = document.createElement("p");
  when.className = "chart-tip__when";
  when.textContent = formatFull(new Date(instant));
  tip.appendChild(when);

  const list = document.createElement("dl");
  list.className = "chart-tip__list";
  if (fillTipRows(readout, list, instant)) {
    tip.appendChild(list);
  } else {
    const none = document.createElement("p");
    none.className = "chart-tip__none";
    none.textContent = "No value stored for this moment";
    tip.appendChild(none);
  }
  tip.hidden = false;
  const tipWidth = tip.offsetWidth || 160;
  tip.style.left = `${Math.min(Math.max(px - tipWidth / 2, 4), layout.width - tipWidth - 4)}px`;
  tip.style.top = `${layout.plotTop}px`;
}

function hideReadout(readout) {
  readout.cursor.setAttribute("visibility", "hidden");
  readout.tip.hidden = true;
}

function nearestInstant(readout, px) {
  let best = null;
  let bestDistance = Infinity;
  for (const instant of readout.instants) {
    const distance = Math.abs(readout.xAt(instant) - px);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = instant;
    }
  }
  return best;
}

function onReadoutKey(readout, event) {
  if (!readout.instants.length) return;
  if (event.key === "Escape") {
    hideReadout(readout);
    return;
  }
  const step = KEY_STEP[event.key];
  if (step === undefined) return;
  readout.index = Math.min(readout.instants.length - 1, Math.max(0, readout.index + step));
  event.preventDefault();
  showAt(readout, readout.instants[readout.index]);
}

function bindReadout(svg, readout) {
  svg.addEventListener("pointermove", (event) => {
    const instant = nearestInstant(readout, event.clientX - svg.getBoundingClientRect().left);
    if (instant === null) return;
    readout.index = readout.instants.indexOf(instant);
    showAt(readout, instant);
  });
  svg.addEventListener("pointerleave", () => hideReadout(readout));
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("focus", () => {
    if (!readout.instants.length) return;
    showAt(readout, readout.instants[Math.min(readout.index, readout.instants.length - 1)]);
  });
  svg.addEventListener("blur", () => hideReadout(readout));
  svg.addEventListener("keydown", (event) => onReadoutKey(readout, event));
}

/**
 * Draw the chart into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} spec
 * @returns {void}
 */
export function renderChart(container, spec) {
  const layout = layoutOf(container, spec);
  container.textContent = "";

  const svg = el("svg", {
    class: "chart",
    width: layout.width,
    height: layout.height,
    viewBox: `0 0 ${layout.width} ${layout.height}`,
    role: "img",
    "aria-label": spec.summary || "",
  });

  drawFutureBand(svg, layout);
  drawGrid(svg, layout, spec);
  drawTimeAxis(svg, layout);
  drawAllSeries(svg, layout);
  const labels = drawEndMarkers(svg, layout);
  if (!layout.compact) drawValueLabels(svg, layout, spec, labels);
  drawSeam(svg, layout, spec);

  const cursor = el("line", {
    class: "chart__cursor",
    x1: 0,
    x2: 0,
    y1: layout.plotTop,
    y2: layout.plotBottom,
    visibility: "hidden",
  });
  svg.appendChild(cursor);
  drawBlank(svg, layout, spec);

  container.appendChild(svg);
  bindReadout(svg, createReadout(container, cursor, layout, spec));
}

/** One sentence describing the plot, for the chart's `aria-label`. */
export function describeChart(spec) {
  const parts = [];
  for (const series of (spec.past || []).concat(spec.future || [])) {
    const values = series.points.map((point) => point.v).filter((value) => Number.isFinite(value));
    if (!values.length) continue;
    const min = Math.min(...values);
    const max = Math.max(...values);
    parts.push(
      `${series.label} from ${formatValue(min, spec.unit)} to ${formatValue(max, spec.unit)}`,
    );
  }
  if (!parts.length) return `${spec.title}: nothing stored for this window yet.`;
  return `${spec.title} in ${unitOf(spec.unit) || spec.unit}, ${parts.join("; ")}.`;
}

function valueAt(series, instant) {
  return pointValue(series.points.find((item) => item.t.getTime() === instant));
}

/** The table view: every plotted value, reachable without a pointer. */
export function renderTable(container, spec) {
  container.textContent = "";
  const all = (spec.past || []).concat(spec.future || []);
  if (!all.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "Nothing stored for this window yet.";
    container.appendChild(empty);
    return;
  }
  const instants = instantsOf(all);

  const table = document.createElement("table");
  table.className = "data-table";
  const caption = document.createElement("caption");
  caption.textContent = `${spec.title} in ${unitOf(spec.unit) || spec.unit}, shown in ${spec.zone}`;
  table.appendChild(caption);

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.scope = "col";
  corner.textContent = "Time";
  headRow.appendChild(corner);
  for (const series of all) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = series.label;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement("tbody");
  for (const instant of instants) {
    if (!all.some((series) => valueAt(series, instant) !== null)) continue;
    const row = document.createElement("tr");
    const when = document.createElement("th");
    when.scope = "row";
    when.textContent = formatFull(new Date(instant));
    row.appendChild(when);
    for (const series of all) {
      const value = valueAt(series, instant);
      const cell = document.createElement("td");
      cell.textContent = value === null ? "—" : formatValue(value, spec.unit);
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
  table.appendChild(body);
  container.appendChild(table);
}
