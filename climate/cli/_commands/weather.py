"""``climate-cli weather`` — query the weather-tracking HTTP API.

Talks only to the read-only web API described in ``docs/weather-api.md``,
never to MongoDB. Every call goes through :func:`fetch`, a single
injectable urllib wrapper with a timeout, so tests never open a real socket
(``tests/conftest.py`` blocks sockets suite-wide; inject a fake ``fetch``
instead).

Base URL resolution: ``CLIMATE_WEATHER_URL`` (no ``/api/v1`` suffix), default
``http://127.0.0.1:8095``. Markdown is the default rendering (for humans and
agents alike); ``--json`` prints the API payload structure unchanged.

Exit-code mapping (see docs/weather-api.md §8.2 "Client obligations"):

* ``0`` — success, including "no data yet"
* ``1`` — a bad flag value
* ``2`` — the web service is unreachable (connection failure, timeout, 503)
* ``3`` — ``--max-age`` exceeded (:data:`~climate.cli._errors.EXIT_STALE`)

This module imports only the standard library plus ``climate.cli``
internals — no third-party packages, and no other ``climate`` subpackage
(the CLI never talks to MongoDB or the store directly).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from climate.cli._commands.overview import emit_overview
from climate.cli._errors import EXIT_ENV_ERROR, EXIT_STALE, EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from climate.cli._output import emit_result

#: Env var overriding the API base URL. No trailing slash, no ``/api/v1``.
BASE_URL_ENV_VAR = "CLIMATE_WEATHER_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8095"
API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 10.0

_STACK_STATUS_HINT = "run 'climate stack status' to check the weather stack"
_FLAG_HINT = "check the flag value against docs/weather-api.md"

_JSON_HELP = "Emit structured JSON."
_PROVIDER_HELP = "Restrict to this provider id."
_LOCATION_HELP = "Restrict to this location label."

_DURATION_RE = re.compile(r"^(\d+)\s*([smh]?)$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 3600}


@dataclass
class ApiResult:
    """Outcome of one GET against the weather API — always this type.

    ``error`` is set for a transport failure (connection refused, timeout,
    DNS, ...); ``status`` is ``None`` in that case. A response that arrived,
    even a non-2xx one, sets ``status`` and leaves ``error`` ``None``.
    """

    status: int | None = None
    body: bytes = b""
    error: str | None = None


def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> ApiResult:
    """Perform one GET request. The single injectable seam for tests.

    Never raises: every outcome (success, HTTP error status, transport
    failure) comes back as an :class:`ApiResult`. Tests replace this
    function (``monkeypatch.setattr(weather, "fetch", fake)``) instead of
    letting a real socket open — ``tests/conftest.py`` blocks those anyway.
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            # URL is built from a fixed API prefix plus an operator-controlled
            # base (CLIMATE_WEATHER_URL or the documented loopback default),
            # never from unsanitized user input, so urlopen here is safe.
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            return ApiResult(status=status, body=response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return ApiResult(status=exc.code, body=body)
    except urllib.error.URLError as exc:
        return ApiResult(error=str(exc.reason) if exc.reason else str(exc))
    except (OSError, ValueError) as exc:
        return ApiResult(error=str(exc))


def _base_url() -> str:
    return os.environ.get(BASE_URL_ENV_VAR, DEFAULT_BASE_URL).rstrip("/")


def _build_url(path: str, params: Mapping[str, Any] | None) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(item)) for item in value if item is not None)
        else:
            pairs.append((key, str(value)))
    query = f"?{urlencode(pairs)}" if pairs else ""
    return f"{_base_url()}{API_PREFIX}{path}{query}"


def _error_envelope_message(body: bytes, fallback: str) -> str:
    try:
        envelope = json.loads(body.decode("utf-8"))
        message = envelope.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except (ValueError, AttributeError):
        pass
    return fallback


def _request(path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Call one API route and return its decoded JSON payload.

    Raises :class:`CliError` with the exit code the client-obligations table
    in docs/weather-api.md prescribes: 2 for an unreachable service (no
    response, or a data route's 503), 1 for a 400 (a bad flag value).
    """
    url = _build_url(path, params)
    result = fetch(url, timeout=DEFAULT_TIMEOUT)

    if result.error is not None or result.status is None:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"weather API unreachable at {url}: {result.error or 'no response'}",
            remediation=_STACK_STATUS_HINT,
        )

    if result.status == 503:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="weather API reports its store is unreachable (503)",
            remediation=_STACK_STATUS_HINT,
        )

    if 200 <= result.status < 300:
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except ValueError as exc:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"weather API returned invalid JSON: {exc}",
                remediation=_STACK_STATUS_HINT,
            ) from exc
        if not isinstance(payload, dict):
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="weather API returned an unexpected (non-object) JSON payload",
                remediation=_STACK_STATUS_HINT,
            )
        return payload

    fallback = f"weather API returned HTTP {result.status}"
    message = _error_envelope_message(result.body, fallback)
    if result.status == 400:
        raise CliError(code=EXIT_USER_ERROR, message=message, remediation=_FLAG_HINT)
    raise CliError(code=EXIT_ENV_ERROR, message=message, remediation=_STACK_STATUS_HINT)


def _parse_max_age(raw: str) -> int:
    """Parse ``--max-age``: a bare integer of seconds, or ``<n>s|m|h``."""
    match = _DURATION_RE.match(raw.strip()) if isinstance(raw, str) else None
    if not match:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid --max-age value: {raw!r}",
            remediation="use a positive integer number of seconds, or a duration like '5m' or '1h'",
        )
    amount, unit = match.groups()
    seconds = int(amount) * _DURATION_MULTIPLIERS[unit.lower()]
    if seconds <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid --max-age value: {raw!r}: must be positive",
            remediation="use a positive integer number of seconds, or a duration like '5m' or '1h'",
        )
    return seconds


# --- rendering --------------------------------------------------------------


def _render_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, str]]) -> str:
    if not rows:
        return ""
    headers = [label for label, _ in columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        cells = [_cell(row.get(key)) for _, key in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_warnings(warnings: Sequence[Mapping[str, Any]]) -> list[str]:
    if not warnings:
        return []
    return ["", "Warnings:"] + [f"- {w.get('message', '')}" for w in warnings]


def _render_missing(missing: Sequence[Mapping[str, Any]]) -> list[str]:
    if not missing:
        return []
    lines = ["", "Missing:"]
    for entry in missing:
        lines.append(f"- {entry.get('provider')}/{entry.get('location')}: {entry.get('reason')}")
    return lines


def _no_readings_markdown(
    missing: Sequence[Mapping[str, Any]], warnings: Sequence[Mapping[str, Any]]
) -> str:
    lines = ["No weather data available yet."]
    lines.extend(_render_missing(missing))
    lines.extend(_render_warnings(warnings))
    return "\n".join(lines)


def _latest_table_row(
    location: Any, provider: Any, variable: str, value: Any, unit: Any, age_seconds: Any, stale: Any
) -> dict[str, Any]:
    return {
        "location": location,
        "provider": provider,
        "variable": variable,
        "value": value,
        "unit": unit,
        "age_seconds": age_seconds,
        "stale": "STALE" if stale else "",
    }


def _latest_table_rows(readings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reading in readings:
        location = reading.get("location")
        provider = reading.get("provider")
        values = reading.get("values") or {}
        if not values:
            rows.append(
                _latest_table_row(
                    location,
                    provider,
                    "-",
                    None,
                    None,
                    reading.get("age_seconds"),
                    reading.get("stale"),
                )
            )
            continue
        for name, value in values.items():
            rows.append(
                _latest_table_row(
                    location,
                    provider,
                    name,
                    value.get("value"),
                    value.get("unit"),
                    value.get("age_seconds"),
                    value.get("stale"),
                )
            )
    return rows


_LATEST_TABLE_COLUMNS = [
    ("location", "location"),
    ("provider", "provider"),
    ("variable", "variable"),
    ("value", "value"),
    ("unit", "unit"),
    ("age_s", "age_seconds"),
    ("stale", "stale"),
]


def _render_latest_markdown(payload: dict[str, Any], max_age_seconds: int | None) -> str:
    readings = payload.get("readings") or []
    missing = payload.get("missing") or []
    warnings = payload.get("warnings") or []

    if not readings:
        return _no_readings_markdown(missing, warnings)

    table = _render_table(_latest_table_rows(readings), _LATEST_TABLE_COLUMNS)
    lines = [table]
    if max_age_seconds is not None:
        lines.append("")
        lines.append(f"max_age: {max_age_seconds}s")
    lines.extend(_render_warnings(warnings))
    return "\n".join(lines)


def _render_series_markdown(payload: dict[str, Any]) -> str:
    series = payload.get("series") or []
    warnings = payload.get("warnings") or []
    if not series:
        lines = ["No series data available yet."]
        lines.extend(_render_warnings(warnings))
        return "\n".join(lines)
    table = _render_table(
        series,
        [
            ("provider", "provider"),
            ("location", "location"),
            ("kind", "kind"),
            ("unit", "unit"),
            ("value_count", "value_count"),
            ("null_count", "null_count"),
            ("min", "min"),
            ("max", "max"),
            ("first_at", "first_at"),
            ("last_at", "last_at"),
        ],
    )
    variable = payload.get("variable")
    step = payload.get("step_seconds")
    lines = [f"variable: {variable}  step_seconds: {step}", "", table]
    lines.extend(_render_warnings(warnings))
    return "\n".join(lines)


def _render_forecast_markdown(payload: dict[str, Any]) -> str:
    forecasts = payload.get("forecasts") or []
    warnings = payload.get("warnings") or []
    if not forecasts:
        lines = ["No forecast data available yet."]
        lines.extend(_render_warnings(warnings))
        return "\n".join(lines)
    table = _render_table(
        forecasts,
        [
            ("provider", "provider"),
            ("location", "location"),
            ("issued_at", "issued_at"),
            ("point_count", "point_count"),
        ],
    )
    lines = [table]
    lines.extend(_render_warnings(warnings))
    return "\n".join(lines)


def _render_stats_markdown(payload: dict[str, Any]) -> str:
    providers = payload.get("providers") or []
    warnings = payload.get("warnings") or []
    if not providers:
        lines = ["No stats available yet."]
        lines.extend(_render_warnings(warnings))
        return "\n".join(lines)
    table = _render_table(
        providers,
        [
            ("provider", "provider"),
            ("location", "location"),
            ("due", "due_count"),
            ("stored", "stored_count"),
            ("completeness", "completeness"),
        ],
    )
    window = payload.get("window_seconds")
    lines = [f"window_seconds: {window}", "", table]
    lines.extend(_render_warnings(warnings))
    return "\n".join(lines)


# --- verbs -------------------------------------------------------------------


def _sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "overview — this descriptive snapshot",
                "latest — newest reading per (provider, location) "
                "[--provider --location --max-age]",
                "series — one variable over time "
                "[--variable --provider --location --from --to --step --agg --max-points]",
                "forecast — stored forecasts "
                "[--provider --location --variables --issued-at --horizon-hours --step-hours]",
                "stats — due-vs-stored fetch counts "
                "[--window --from --to --provider --location --bucket]",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                f"base URL: ${BASE_URL_ENV_VAR} (default {DEFAULT_BASE_URL})",
                "markdown by default; --json prints the API payload structure unchanged",
                "exit codes: 0 ok, 1 bad flag, 2 web service unreachable, 3 stale "
                "(--max-age exceeded)",
            ],
        },
    ]


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "climate-cli weather",
        _sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return EXIT_SUCCESS


def cmd_latest(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    max_age_raw = getattr(args, "max_age", None)
    max_age_seconds = _parse_max_age(max_age_raw) if max_age_raw is not None else None

    params: dict[str, Any] = {}
    if getattr(args, "provider", None):
        params["provider"] = args.provider
    if getattr(args, "location", None):
        params["location"] = args.location
    if max_age_seconds is not None:
        params["max_age"] = max_age_seconds

    payload = _request("/latest", params)

    readings = payload.get("readings") or []
    local_stale = max_age_seconds is not None and any(
        isinstance(reading.get("age_seconds"), (int, float))
        and reading["age_seconds"] > max_age_seconds
        for reading in readings
    )
    stale = bool(payload.get("stale", False)) or local_stale
    payload["stale"] = stale

    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_latest_markdown(payload, max_age_seconds), json_mode=False)

    return EXIT_STALE if stale else EXIT_SUCCESS


def cmd_series(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    params: dict[str, Any] = {"variable": args.variable}
    if getattr(args, "provider", None):
        params["provider"] = args.provider
    if getattr(args, "location", None):
        params["location"] = args.location
    if getattr(args, "kind", None):
        params["kind"] = args.kind
    if getattr(args, "from_", None):
        params["from"] = args.from_
    if getattr(args, "to", None):
        params["to"] = args.to
    if getattr(args, "step", None) is not None:
        params["step"] = args.step
    if getattr(args, "agg", None):
        params["agg"] = args.agg
    if getattr(args, "max_points", None) is not None:
        params["max_points"] = args.max_points

    payload = _request("/series", params)
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_series_markdown(payload), json_mode=False)
    return EXIT_SUCCESS


def cmd_forecast(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    params: dict[str, Any] = {}
    if getattr(args, "provider", None):
        params["provider"] = args.provider
    if getattr(args, "location", None):
        params["location"] = args.location
    if getattr(args, "variables", None):
        params["variables"] = args.variables
    if getattr(args, "issued_at", None):
        params["issued_at"] = args.issued_at
    if getattr(args, "horizon_hours", None) is not None:
        params["horizon_hours"] = args.horizon_hours
    if getattr(args, "step_hours", None) is not None:
        params["step_hours"] = args.step_hours

    payload = _request("/forecast", params)
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_forecast_markdown(payload), json_mode=False)
    return EXIT_SUCCESS


def cmd_stats(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    params: dict[str, Any] = {}
    if getattr(args, "window", None):
        params["window"] = args.window
    if getattr(args, "from_", None):
        params["from"] = args.from_
    if getattr(args, "to", None):
        params["to"] = args.to
    if getattr(args, "provider", None):
        params["provider"] = args.provider
    if getattr(args, "location", None):
        params["location"] = args.location
    if getattr(args, "bucket", None) is not None:
        params["bucket"] = args.bucket

    payload = _request("/stats", params)
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_stats_markdown(payload), json_mode=False)
    return EXIT_SUCCESS


def _no_verb(args: argparse.Namespace) -> int:
    # `climate-cli weather` with no sub-verb prints the noun's overview.
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "weather",
        help="Query the weather-tracking HTTP API (see docs/weather-api.md).",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # Propagate the caller's parser class so verb-level parse errors route
    # through the same structured error contract as the top-level parser
    # (see climate/cli/_commands/cli.py for the same pattern).
    noun_sub = p.add_subparsers(dest="weather_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the weather query verbs.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    latest = noun_sub.add_parser(
        "latest", help="Newest reading per (provider, location). GET /latest."
    )
    latest.add_argument("--provider", action="append", help=_PROVIDER_HELP)
    latest.add_argument("--location", action="append", help=_LOCATION_HELP)
    latest.add_argument(
        "--max-age",
        dest="max_age",
        default=None,
        help="Staleness threshold: seconds, or '5m'/'1h'. Exceeding it exits 3.",
    )
    latest.add_argument("--json", action="store_true", help=_JSON_HELP)
    latest.set_defaults(func=cmd_latest)

    series = noun_sub.add_parser("series", help="One variable over time. GET /series.")
    series.add_argument("--variable", required=True, help="Variable id (required).")
    series.add_argument("--provider", action="append", help=_PROVIDER_HELP)
    series.add_argument("--location", action="append", help=_LOCATION_HELP)
    series.add_argument(
        "--kind", action="append", help="observation | model | forecast (repeatable)."
    )
    series.add_argument("--from", dest="from_", default=None, help="Inclusive lower bound.")
    series.add_argument("--to", dest="to", default=None, help="Exclusive upper bound.")
    series.add_argument("--step", type=int, default=None, help="Grid resolution in seconds.")
    series.add_argument(
        "--agg", default=None, help="last | first | mean | min | max (default last)."
    )
    series.add_argument("--max-points", dest="max_points", type=int, default=None)
    series.add_argument("--json", action="store_true", help=_JSON_HELP)
    series.set_defaults(func=cmd_series)

    forecast = noun_sub.add_parser("forecast", help="Stored forecasts. GET /forecast.")
    forecast.add_argument("--provider", action="append", help=_PROVIDER_HELP)
    forecast.add_argument("--location", action="append", help=_LOCATION_HELP)
    forecast.add_argument(
        "--variables", default=None, help="Comma-separated variable ids to restrict to."
    )
    forecast.add_argument("--issued-at", dest="issued_at", default=None)
    forecast.add_argument("--horizon-hours", dest="horizon_hours", type=int, default=None)
    forecast.add_argument("--step-hours", dest="step_hours", type=int, default=None)
    forecast.add_argument("--json", action="store_true", help=_JSON_HELP)
    forecast.set_defaults(func=cmd_forecast)

    stats = noun_sub.add_parser(
        "stats", help="Due-versus-stored fetch counts per provider. GET /stats."
    )
    stats.add_argument("--window", default=None, help="e.g. '90m', '24h', '7d' (default 24h).")
    stats.add_argument("--from", dest="from_", default=None)
    stats.add_argument("--to", dest="to", default=None)
    stats.add_argument("--provider", action="append", help=_PROVIDER_HELP)
    stats.add_argument("--location", action="append", help=_LOCATION_HELP)
    stats.add_argument("--bucket", type=int, default=None)
    stats.add_argument("--json", action="store_true", help=_JSON_HELP)
    stats.set_defaults(func=cmd_stats)
