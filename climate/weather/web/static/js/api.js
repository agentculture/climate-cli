/**
 * The only place this dashboard names an HTTP route.
 *
 * Every route below is one documented in `docs/weather-api.md` section 5,
 * same-origin and relative, exactly as section 8.1 requires. Nothing here
 * sends a local time, a coordinate or a place name: the only identifier a
 * location has is its user-chosen label.
 */

export const API_PREFIX = "/api/v1";

/** Route id -> path. Keep this list and docs/weather-api.md in step. */
export const ROUTES = Object.freeze({
  health: `${API_PREFIX}/health`,
  providers: `${API_PREFIX}/providers`,
  locations: `${API_PREFIX}/locations`,
  latest: `${API_PREFIX}/latest`,
  series: `${API_PREFIX}/series`,
  forecast: `${API_PREFIX}/forecast`,
  stats: `${API_PREFIX}/stats`,
});

/** Thrown for anything that stops a route returning a usable body. */
export class ApiError extends Error {
  constructor(message, { route, status = null, code = null, unreachable = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.route = route;
    this.status = status;
    this.code = code;
    this.unreachable = unreachable;
  }
}

function withQuery(path, params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null) continue;
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item === undefined || item === null || item === "") continue;
      search.append(key, String(item));
    }
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/**
 * GET one documented route and return its parsed body.
 *
 * The API answers `200` for empty results and for stale data (contract
 * section 4.3), so an empty page is never an error here — only a transport
 * failure, a non-2xx status or an unparseable body is.
 */
export async function get(route, params, { signal } = {}) {
  const path = ROUTES[route];
  if (!path) throw new ApiError(`unknown route ${route}`, { route });

  let response;
  try {
    response = await fetch(withQuery(path, params), {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (cause) {
    if (cause && cause.name === "AbortError") throw cause;
    throw new ApiError("The weather service is not answering.", {
      route,
      unreachable: true,
    });
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const error = body && body.error ? body.error : {};
    throw new ApiError(error.message || `Request failed with ${response.status}.`, {
      route,
      status: response.status,
      code: error.code || null,
    });
  }
  if (!body) throw new ApiError("The service sent a response this page cannot read.", { route });
  return body;
}

/** Run several route reads at once, keeping each one's failure separate. */
export async function getAll(requests, options) {
  const entries = Object.entries(requests);
  const settled = await Promise.allSettled(
    entries.map(([, [route, params]]) => get(route, params, options)),
  );
  const out = {};
  for (let i = 0; i < entries.length; i += 1) {
    const [name] = entries[i];
    const result = settled[i];
    out[name] = result.status === "fulfilled" ? { ok: true, data: result.value } : { ok: false, error: result.reason };
  }
  return out;
}
