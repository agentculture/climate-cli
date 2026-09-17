/**
 * Series identity: one colour per provider, one stroke style per kind.
 *
 * Colour
 * ------
 * The AgentCulture terminal palette recorded in
 * `docs/adr/0001-culture-design-source.md` was measured first, against this
 * dashboard's own two surfaces (`--surface` = #ffffff light, #161b36 dark),
 * with the dataviz skill's validator. It fails four hard gates in *both*
 * themes — it is a set of seven pale swatches designed for one fixed dark
 * terminal ground, not a categorical scale: lightness band FAIL, chroma
 * floor FAIL (teal and green read as grey), CVD separation FAIL (yellow vs
 * green, dE 5.1) and the normal-vision floor FAIL (dE 9.4, under the 15
 * gate). The dataviz skill's validated default categorical palette is used
 * instead; it passes every gate on both of these surfaces. The AgentCulture
 * identity carries the rest of the page — surfaces, ink, the aurora-teal
 * accent, the dawn washes, the type roles and the motion easings all come
 * from tokens.css untouched.
 *
 * Slots are assigned over the *enabled* providers in the order
 * `GET /providers` returns them, so a colour belongs to a provider, never to
 * its rank in the current view: filtering or reordering the page never
 * repaints a series. Only the operator enabling or disabling a provider
 * changes an assignment.
 *
 * Stroke
 * ------
 * The three kinds wear org's own diagram semantic, recorded verbatim in
 * culture-nodes' `web/src/culture-design/edges.ts`: SOLID is state something
 * actually put on the record, DOTTED is a reference carrying no authority of
 * its own, DASHED is a proposal nobody has confirmed. Mapped here:
 * `observation` (an instrument measured it) is solid, `model` (a model's
 * value for a time nobody measured) is dotted, `forecast` (a claim about a
 * time that has not happened) is dashed.
 */

/** The eight validated categorical slots, as CSS custom-property names. */
export const SERIES_SLOTS = 8;

/** Kind -> stroke treatment. Shape is the secondary, non-colour channel. */
export const KIND_STYLE = Object.freeze({
  observation: { dash: null, width: 2, marker: "dot" },
  model: { dash: "2 7", width: 2, marker: "square" },
  forecast: { dash: "9 7", width: 2, marker: "none" },
});

export function kindStyle(kind) {
  return KIND_STYLE[kind] || KIND_STYLE.model;
}

/**
 * Build a stable provider -> colour lookup.
 *
 * @param {Array} providers rows from `GET /providers`, in registry order
 * @returns {(providerId: string) => string} a CSS colour expression
 */
export function colorScale(providers) {
  const assigned = new Map();
  let slot = 0;
  for (const row of providers || []) {
    if (!row.enabled) continue;
    assigned.set(row.provider, `var(--series-${(slot % SERIES_SLOTS) + 1})`);
    slot += 1;
  }
  return (providerId) => assigned.get(providerId) || "var(--chart-muted)";
}

/** A stable key for one drawn series. */
export function seriesKey(providerId, kind) {
  return `${providerId}·${kind}`;
}
