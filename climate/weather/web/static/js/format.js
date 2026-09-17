/**
 * Display vocabulary: unit ids, variable names, numbers, and UTC -> local time.
 *
 * The API speaks UTC ISO-8601 and canonical unit ids (contract sections 2.5
 * and 4.1) and never renders a symbol. Turning `degC` into `°C` and
 * `2026-09-17T08:35:00Z` into the reader's own clock happens here, in the
 * browser, and never travels back.
 */

/** Unit id -> display string (contract section 4.1). */
export const UNIT_DISPLAY = Object.freeze({
  degC: "°C",
  percent: "%",
  hPa: "hPa",
  m_s: "m/s",
  deg: "°",
  mm: "mm",
  w_m2: "W/m²",
  m: "m",
  index: "",
  code: "",
  other: "",
});

/** Variable id -> short human name. `x_` variables are not shown. */
export const VARIABLE_LABEL = Object.freeze({
  temperature: "Temperature",
  apparent_temperature: "Feels like",
  dew_point: "Dew point",
  relative_humidity: "Humidity",
  pressure_msl: "Pressure",
  pressure_surface: "Pressure, station",
  wind_speed: "Wind",
  wind_gust: "Gust",
  wind_direction: "Direction",
  precipitation: "Precipitation",
  rain: "Rain",
  precipitation_probability: "Chance of rain",
  cloud_cover: "Cloud",
  visibility: "Visibility",
  shortwave_radiation: "Radiation",
  direct_radiation: "Direct",
  diffuse_radiation: "Diffuse",
  uv_index: "UV index",
  weather_code: "Code",
});

/** Reading kind -> the word this page uses for it (contract section 4.2). */
export const KIND_LABEL = Object.freeze({
  observation: "Measured",
  model: "Modelled",
  forecast: "Forecast",
});

export function unitOf(unitId) {
  return UNIT_DISPLAY[unitId] !== undefined ? UNIT_DISPLAY[unitId] : unitId || "";
}

export function labelOf(variableId) {
  return VARIABLE_LABEL[variableId] || variableId;
}

/**
 * Units whose place count never depends on the magnitude: a percent, a
 * bearing, an irradiance and a distance in metres read as whole numbers,
 * and a millimetre of rain keeps its tenth however large the total is.
 */
const FIXED_DIGITS = Object.freeze({ percent: 0, deg: 0, w_m2: 0, m: 0, mm: 1 });

/** Every other unit — including hPa — drops the decimal from 100 up. */
function digitsFor(unitId, abs) {
  const fixed = FIXED_DIGITS[unitId];
  if (fixed !== undefined) return fixed;
  return abs >= 100 ? 0 : 1;
}

/** A measured value, rounded to a sensible number of places for its unit. */
export function formatValue(value, unitId) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  const digits = digitsFor(unitId, Math.abs(value));
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function parseTime(text) {
  if (!text) return null;
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const HOUR_FORMAT = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const DAY_HOUR_FORMAT = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});
const FULL_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

/** The reader's own time zone name, for the one place the page states it. */
export const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";

export function formatClock(date) {
  return date ? HOUR_FORMAT.format(date) : "—";
}

export function formatDayClock(date) {
  return date ? DAY_HOUR_FORMAT.format(date) : "—";
}

export function formatFull(date) {
  return date ? FULL_FORMAT.format(date) : "—";
}

/** "12 s", "4 min", "3 h", "2 d" — a compact age, never a sentence. */
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const value = Math.max(0, Math.round(seconds));
  if (value < 90) return `${value} s`;
  if (value < 5400) return `${Math.round(value / 60)} min`;
  if (value < 172800) return `${Math.round(value / 3600)} h`;
  return `${Math.round(value / 86400)} d`;
}

/** A ratio as a whole percent; `null` completeness stays a dash. */
export function formatPercent(ratio) {
  if (ratio === null || ratio === undefined) return "—";
  return `${Math.round(ratio * 100)}%`;
}

/** Compass point for a meteorological wind direction. */
const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function compassOf(degrees) {
  if (degrees === null || degrees === undefined) return "";
  return COMPASS[Math.round((((degrees % 360) + 360) % 360) / 22.5) % 16];
}

/** A provider id rendered the way its registry title would be. */
export function providerTitle(providerId, providers) {
  return (providers || []).find((item) => item.provider === providerId)?.title || providerId;
}
