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
  const step = candidates.find((value) => value >= raw) || candidates[candidates.length - 1];
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
  if (!(span > 0)) return [];
  const steps = [
    5 * 6e4, 15 * 6e4, 30 * 6e4, 36e5, 2 * 36e5, 3 * 36e5, 6 * 36e5, 12 * 36e5,
    864e5, 2 * 864e5,
  ];
  const target = span / count;
  const step = steps.find((value) => value >= target) || steps[steps.length - 1];
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
    if (point.v === null || point.v === undefined || !Number.isFinite(point.v)) {
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

function extent(seriesList) {
  let min = Infinity;
  let max = -Infinity;
  for (const series of seriesList) {
    for (const point of series.points) {
      if (point.v === null || point.v === undefined || !Number.isFinite(point.v)) continue;
      if (point.v < min) min = point.v;
      if (point.v > max) max = point.v;
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
 * Draw the chart into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} spec
 * @returns {void}
 */
export function renderChart(container, spec) {
  const width = Math.max(280, container.clientWidth || 640);
  const compact = width < 560;
  const height = compact ? 236 : 300;
  const past = spec.past || [];
  const future = spec.future || [];
  const hasFuture = future.some((series) => runsOf(series.points).length > 0);

  container.textContent = "";

  const svg = el("svg", {
    class: "chart",
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": spec.summary || "",
  });

  const plotLeft = MARGIN.left;
  const plotRight = width - MARGIN.right;
  const plotTop = MARGIN.top;
  const plotBottom = height - MARGIN.bottom;
  const futureWidth = hasFuture ? (plotRight - plotLeft) * FUTURE_FRACTION : 0;
  const seam = plotRight - futureWidth;

  const pastFrom = spec.from.getTime();
  const nowValue = spec.now.getTime();
  const futureTo = spec.to.getTime();
  const xPast = bandScale(pastFrom, nowValue, plotLeft, hasFuture ? seam - SEAM_GAP / 2 : plotRight);
  const xFuture = bandScale(nowValue, futureTo, seam + SEAM_GAP / 2, plotRight);

  const span = extent(past.concat(hasFuture ? future : []));
  const hasValues = span !== null;
  const { ticks: yTicks, lo, hi } = niceTicks(
    span ? span[0] : 0,
    span ? span[1] : 1,
    compact ? 3 : 5,
  );
  const y = (value) => plotBottom - ((value - lo) / (hi - lo || 1)) * (plotBottom - plotTop);

  // --- the future band: the part the sun has not reached ------------------
  if (hasFuture) {
    svg.appendChild(
      el("rect", {
        class: "chart__future",
        x: seam + SEAM_GAP / 2,
        y: plotTop,
        width: Math.max(0, plotRight - seam - SEAM_GAP / 2),
        height: plotBottom - plotTop,
        rx: 6,
      }),
    );
  }

  // --- gridlines and the y axis -------------------------------------------
  for (const tick of hasValues ? yTicks : []) {
    const yy = y(tick);
    if (yy < plotTop - 0.5 || yy > plotBottom + 0.5) continue;
    svg.appendChild(
      el("line", { class: "chart__grid", x1: plotLeft, x2: plotRight, y1: yy, y2: yy }),
    );
    svg.appendChild(
      text(
        el("text", { class: "chart__tick chart__tick--y", x: plotLeft - 8, y: yy + 4, "text-anchor": "end" }),
        formatValue(tick, spec.unit),
      ),
    );
  }
  svg.appendChild(
    el("line", { class: "chart__axis", x1: plotLeft, x2: plotRight, y1: plotBottom, y2: plotBottom }),
  );

  // --- x axis, one band at a time -----------------------------------------
  const stampFor = (span) => (span > 26 * 36e5 ? formatDayClock : formatClock);
  const stamp = stampFor(nowValue - pastFrom);
  for (const tick of timeTicks(pastFrom, nowValue, compact ? 2 : 4)) {
    const xx = xPast(tick.getTime());
    if (xx > (hasFuture ? seam - SEAM_GAP : plotRight) - 4) continue;
    svg.appendChild(
      text(
        el("text", { class: "chart__tick", x: xx, y: plotBottom + 17, "text-anchor": "middle" }),
        stamp(tick),
      ),
    );
  }
  if (hasFuture) {
    const futureStamp = stampFor(futureTo - nowValue);
    for (const tick of timeTicks(nowValue, futureTo, compact ? 1 : 2)) {
      const xx = xFuture(tick.getTime());
      if (xx > plotRight - 12) continue;
      svg.appendChild(
        text(
          el("text", { class: "chart__tick", x: xx, y: plotBottom + 17, "text-anchor": "middle" }),
          futureStamp(tick),
        ),
      );
    }
  }

  // --- the series ----------------------------------------------------------
  const drawSeries = (series, scale, clipTo) => {
    const style = kindStyle(series.kind);
    const group = el("g", { class: "chart__series" });
    for (const run of runsOf(series.points)) {
      const coords = run
        .filter((point) => scale(point.t.getTime()) <= clipTo + 0.5)
        .map((point) => [scale(point.t.getTime()), y(point.v)]);
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
  };

  for (const series of past) drawSeries(series, xPast, hasFuture ? seam - SEAM_GAP / 2 : plotRight);
  if (hasFuture) for (const series of future) drawSeries(series, xFuture, plotRight);

  // --- end markers and direct labels on the past band ----------------------
  const labels = [];
  for (const series of past) {
    const runs = runsOf(series.points);
    const last = runs.length ? runs[runs.length - 1][runs[runs.length - 1].length - 1] : null;
    if (!last) continue;
    const px = xPast(last.t.getTime());
    const py = y(last.v);
    const style = kindStyle(series.kind);
    if (style.marker === "square") {
      svg.appendChild(
        el("rect", { class: "chart__end", x: px - 4, y: py - 4, width: 8, height: 8, fill: series.color }),
      );
    } else {
      svg.appendChild(el("circle", { class: "chart__end", cx: px, cy: py, r: 4.5, fill: series.color }));
    }
    labels.push({ x: px, y: py, value: last.v, anchor: py });
  }
  if (!compact) {
    for (const entry of placeLabels(labels)) {
      const side = hasFuture ? -1 : 1;
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
            "text-anchor": hasFuture ? "end" : "start",
            "dominant-baseline": "middle",
          }),
          formatValue(entry.value, spec.unit),
        ),
      );
    }
  }

  // --- the seam: one rule at now ------------------------------------------
  if (hasFuture) {
    svg.appendChild(
      el("line", { class: "chart__now", x1: seam, x2: seam, y1: plotTop - 6, y2: plotBottom + 4 }),
    );
    svg.appendChild(
      text(
        el("text", { class: "chart__seam-label", x: seam + SEAM_GAP, y: plotTop - 2 }),
        spec.futureLabel || "Forecast",
      ),
    );
  }

  const cursor = el("line", {
    class: "chart__cursor",
    x1: 0,
    x2: 0,
    y1: plotTop,
    y2: plotBottom,
    visibility: "hidden",
  });
  svg.appendChild(cursor);

  if (!hasValues) {
    svg.appendChild(
      text(
        el("text", {
          class: "chart__blank",
          x: (plotLeft + plotRight) / 2,
          y: (plotTop + plotBottom) / 2,
          "text-anchor": "middle",
        }),
        spec.blankLabel || "Nothing stored for this window yet",
      ),
    );
  }

  container.appendChild(svg);

  // --- hover / focus readout ----------------------------------------------
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  container.appendChild(tip);

  const all = past.concat(hasFuture ? future : []);
  const instants = Array.from(
    new Set(all.flatMap((series) => series.points.map((point) => point.t.getTime()))),
  ).sort((a, b) => a - b);

  const xAt = (value) => (value <= nowValue ? xPast(value) : xFuture(value));

  function showAt(instant) {
    const px = xAt(instant);
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
    let rows = 0;
    for (const series of all) {
      const point = series.points.find((item) => item.t.getTime() === instant);
      if (!point || point.v === null || point.v === undefined) continue;
      const term = document.createElement("dt");
      const key = document.createElement("span");
      key.className = "chart-tip__key";
      key.style.background = series.color;
      term.appendChild(key);
      term.appendChild(document.createTextNode(series.label));
      const value = document.createElement("dd");
      value.textContent = `${formatValue(point.v, spec.unit)} ${unitOf(spec.unit)}`.trim();
      list.appendChild(term);
      list.appendChild(value);
      rows += 1;
    }
    if (!rows) {
      const none = document.createElement("p");
      none.className = "chart-tip__none";
      none.textContent = "No value stored for this moment";
      tip.appendChild(none);
    } else {
      tip.appendChild(list);
    }
    tip.hidden = false;
    const tipWidth = tip.offsetWidth || 160;
    const left = Math.min(Math.max(px - tipWidth / 2, 4), width - tipWidth - 4);
    tip.style.left = `${left}px`;
    tip.style.top = `${plotTop}px`;
  }

  function hide() {
    cursor.setAttribute("visibility", "hidden");
    tip.hidden = true;
  }

  function nearest(px) {
    let best = null;
    let bestDistance = Infinity;
    for (const instant of instants) {
      const distance = Math.abs(xAt(instant) - px);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = instant;
      }
    }
    return best;
  }

  let index = instants.length - 1;
  svg.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const instant = nearest(event.clientX - box.left);
    if (instant === null) return;
    index = instants.indexOf(instant);
    showAt(instant);
  });
  svg.addEventListener("pointerleave", hide);
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("focus", () => {
    if (instants.length) showAt(instants[Math.min(index, instants.length - 1)]);
  });
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (event) => {
    if (!instants.length) return;
    if (event.key === "ArrowLeft") index = Math.max(0, index - 1);
    else if (event.key === "ArrowRight") index = Math.min(instants.length - 1, index + 1);
    else if (event.key === "Escape") return hide();
    else return;
    event.preventDefault();
    showAt(instants[index]);
  });
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
  const instants = Array.from(
    new Set(all.flatMap((series) => series.points.map((point) => point.t.getTime()))),
  ).sort((a, b) => a - b);

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
    const hasValue = all.some((series) => {
      const point = series.points.find((item) => item.t.getTime() === instant);
      return point && point.v !== null && point.v !== undefined;
    });
    if (!hasValue) continue;
    const row = document.createElement("tr");
    const when = document.createElement("th");
    when.scope = "row";
    when.textContent = formatFull(new Date(instant));
    row.appendChild(when);
    for (const series of all) {
      const point = series.points.find((item) => item.t.getTime() === instant);
      const cell = document.createElement("td");
      cell.textContent =
        point && point.v !== null && point.v !== undefined ? formatValue(point.v, spec.unit) : "—";
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
  table.appendChild(body);
  container.appendChild(table);
}
