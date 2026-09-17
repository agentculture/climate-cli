"""``climate providers`` — list weather provider adapters (host CLI verb).

Reads the adapter registry (:mod:`climate.weather.providers`) only. No
network access, no MongoDB, standard library only.

The bare noun lists every registered adapter — id, capabilities, auth
requirement, enabled state and reason, freshness strategy and attribution —
as a markdown table by default, or as JSON with ``--json``. ``--limits``
adds the declared quota columns (per-minute/day/month plus the quota's
``verified``/``source`` provenance fields).

Enabled state comes from :func:`climate.weather.providers.describe_providers`,
fed by the user's weather config (:func:`climate.weather.config.load_config`)
when one is readable; a missing or unreadable config is tolerated — every
provider is then described with its adapter defaults, never a hard failure.
Location is private data (spec's privacy rule): this verb never prints a
place name or a coordinate — only provider metadata.

Like ``cli``, ``providers`` also exposes ``overview`` to satisfy the
agent-first rubric's noun/overview pairing; unlike ``cli`` the noun's *bare*
form is the listing (the acceptance criterion this task owns), not the
overview.
"""

from __future__ import annotations

import argparse
from typing import Any

from climate.cli._commands.overview import emit_overview
from climate.cli._output import emit_result
from climate.weather import providers as _registry

try:  # pragma: no cover - defensive: config is a sibling contract, not ours.
    from climate.weather.config import load_config as _load_config
except ImportError:  # pragma: no cover
    _load_config = None

_LIMIT_HEADERS = ("calls/min", "calls/day", "calls/month", "verified", "source")
_BASE_HEADERS = (
    "id",
    "capabilities",
    "auth",
    "enabled",
    "reason",
    "freshness",
    "attribution",
)


def _settings_map() -> dict[str, Any]:
    """Per-provider settings from the user's weather config, or none.

    Tolerates a missing ``load_config`` (the config module not on the path
    yet) and a missing/unreadable config file (``load_config`` itself already
    treats a missing file as empty) alike: every provider then falls back to
    its adapter defaults rather than failing this read-only verb.
    """
    if _load_config is None:
        return {}
    try:
        config = _load_config()
    except Exception:  # noqa: BLE001 - an unreadable config must not break `providers`.
        return {}
    return dict(getattr(config, "providers", {}) or {})


def _rows() -> list[dict[str, Any]]:
    return _registry.describe_providers(_settings_map())


def _fmt_cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _auth_cell(auth: dict[str, Any]) -> str:
    if not auth.get("required"):
        return "none"
    env_var = auth.get("env_var") or "?"
    return f"required ({env_var})"


def _attribution_cell(attribution: dict[str, Any] | None) -> str:
    if not attribution:
        return "MISSING ATTRIBUTION"
    text = attribution.get("text") or ""
    url = attribution.get("url") or ""
    if not text or not url:
        return "MISSING ATTRIBUTION"
    return f"{text} ({url})"


def _row_cells(row: dict[str, Any], *, limits: bool) -> list[str]:
    cells = [
        row["id"],
        ", ".join(row.get("capabilities") or []) or "-",
        _auth_cell(row.get("auth") or {}),
        _fmt_cell(row.get("enabled")),
        row.get("reason") or "-",
        row.get("freshness") or "-",
        _attribution_cell(row.get("attribution")),
    ]
    if limits:
        quota = row.get("quota") or {}
        cells += [
            _fmt_cell(quota.get("calls_per_minute")),
            _fmt_cell(quota.get("calls_per_day")),
            _fmt_cell(quota.get("calls_per_month")),
            _fmt_cell(quota.get("verified")),
            quota.get("source") or "-",
        ]
    return cells


def render_table(rows: list[dict[str, Any]], *, limits: bool = False) -> str:
    """Render provider rows as a markdown table (or a note when there are none)."""
    if not rows:
        return "no providers registered"
    headers = list(_BASE_HEADERS) + (list(_LIMIT_HEADERS) if limits else [])
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_row_cells(row, limits=limits)) + " |")
    return "\n".join(lines)


def cmd_providers_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    limits = bool(getattr(args, "limits", False))
    rows = _rows()
    if json_mode:
        emit_result({"providers": rows}, json_mode=True)
    else:
        emit_result(render_table(rows, limits=limits), json_mode=False)
    return 0


def _overview_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "providers — list registered adapters: capabilities, auth, "
                "enabled state and reason, freshness strategy, attribution",
                "providers --limits — add quota columns (calls/min, calls/day, "
                "calls/month, verified, source)",
                "providers --json — structured output",
                "providers overview — this description",
            ],
        },
        {
            "title": "Notes",
            "items": [
                "reads the adapter registry only (no network, no MongoDB)",
                "never prints a location name or coordinate",
            ],
        },
    ]


def cmd_providers_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "climate-cli providers",
        _overview_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "providers",
        help="List weather provider adapters (see 'climate providers overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--limits",
        action="store_true",
        help="Add the declared quota columns (calls/min/day/month, verified, source).",
    )
    p.set_defaults(func=cmd_providers_list, json=False, limits=False)
    # `p` may be a structured-error parser (see climate.cli._CliArgumentParser);
    # propagate its class so `providers overview` parse errors route through
    # the same contract instead of argparse's default stderr/exit 2.
    noun_sub = p.add_subparsers(dest="providers_command", parser_class=type(p))
    ov = noun_sub.add_parser("overview", help="Describe the providers noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_providers_overview)
