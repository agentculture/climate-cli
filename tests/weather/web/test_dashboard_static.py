"""Browser-free guards for the static dashboard.

The dashboard's look is verified with a real browser (see
``docs/design/README.md``); these tests pin the properties that must hold
without one, and that a browser check would not catch anyway:

* the build-free file set is present and self-contained,
* nothing loads from a third-party origin,
* every ``/api/v1`` path the JavaScript names is a route
  ``docs/weather-api.md`` actually documents,
* no static file carries a coordinate or a place name.

The privacy guard here is deliberately stricter than
``tests/test_repo_hygiene.py``'s repo-wide scan: that one looks for a
lat/lon keyword next to a decimal literal, while a dashboard could leak a
place through a bare name in a label or a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_ROOT = REPO_ROOT / "climate" / "weather" / "web" / "static"
API_CONTRACT = REPO_ROOT / "docs" / "weather-api.md"
DESIGN_DIR = REPO_ROOT / "docs" / "design"

#: Files the build-free dashboard is made of. No bundler, no lockfile, no
#: node_modules: the browser loads exactly these.
EXPECTED_FILES = (
    "index.html",
    "dashboard.css",
    "tokens.css",
    "js/api.js",
    "js/app.js",
    "js/chart.js",
    "js/format.js",
    "js/panels.js",
    "js/series.js",
)

TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".svg"}


def _static_files() -> list[Path]:
    return sorted(path for path in STATIC_ROOT.rglob("*") if path.is_file())


def _text_files() -> list[Path]:
    return [path for path in _static_files() if path.suffix in TEXT_SUFFIXES]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- the file set ------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_static_file_present_and_non_empty(name: str) -> None:
    path = STATIC_ROOT / name
    assert path.is_file(), f"missing dashboard file: {path}"
    assert path.stat().st_size > 0, f"empty dashboard file: {path}"


def test_no_build_artefacts_under_static() -> None:
    """A Node toolchain would leave traces; there is none to leave."""
    forbidden = {"package.json", "package-lock.json", "yarn.lock", "node_modules", "dist"}
    offenders = [
        str(path.relative_to(STATIC_ROOT))
        for path in STATIC_ROOT.rglob("*")
        if path.name in forbidden
    ]
    assert not offenders, f"build artefacts under static/: {offenders}"


def test_index_loads_only_local_relative_assets() -> None:
    """Every script, stylesheet and module specifier resolves inside static/."""
    html = _read(STATIC_ROOT / "index.html")
    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', html)
    assert refs, "index.html references no assets at all"
    for ref in refs:
        if ref.startswith("#") or ref.startswith("data:"):
            continue
        assert not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref), f"absolute-scheme asset: {ref}"
        assert not ref.startswith("//"), f"protocol-relative asset: {ref}"
        assert not ref.startswith("/"), f"root-absolute asset: {ref}"
        assert (STATIC_ROOT / ref).is_file(), f"asset does not exist: {ref}"


def test_no_external_origin_anywhere_in_the_page_assets() -> None:
    """No CDN, no web font, no analytics — in HTML, CSS or JavaScript.

    Attribution URLs are the deliberate exception: the contract requires the
    footer to link each provider's licence, and those links are built at
    runtime from ``GET /providers``, never hard-coded here.
    """
    pattern = re.compile(r"""(?:https?:)?//[a-z0-9.-]+""", re.IGNORECASE)
    allowed = {"//www.w3.org", "http://www.w3.org"}
    offenders: list[str] = []
    for path in _text_files():
        if path.name == "tokens.css":
            continue  # the pinned upstream copy; its header names the source repo
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            for hit in pattern.findall(line):
                if any(hit.lower().startswith(prefix) for prefix in allowed):
                    continue
                offenders.append(f"{path.relative_to(STATIC_ROOT)}:{lineno}: {hit}")
    assert not offenders, "external origins referenced by the dashboard:\n" + "\n".join(offenders)


def test_javascript_never_imports_from_outside_static() -> None:
    for path in STATIC_ROOT.rglob("*.js"):
        for specifier in re.findall(r"""import[^"']*["']([^"']+)["']""", _read(path)):
            assert specifier.startswith("./") or specifier.startswith(
                "../"
            ), f"{path.name} imports a non-relative module: {specifier}"


# --- the API contract --------------------------------------------------------


def _documented_routes() -> set[str]:
    """Route paths ``docs/weather-api.md`` defines, as full `/api/v1/...`."""
    contract = _read(API_CONTRACT)
    section = contract.split("## 5. Routes", 1)[1]
    names = set(re.findall(r"`GET /([a-z]+)`", section))
    assert names, "no routes parsed out of the contract"
    return {f"/api/v1/{name}" for name in names}


def test_contract_lists_the_seven_routes() -> None:
    assert len(_documented_routes()) == 7


def test_every_api_path_in_the_javascript_is_documented() -> None:
    documented = _documented_routes()
    used: dict[str, str] = {}
    for path in _text_files():
        for match in re.findall(r"/api/v1(?:/[a-z_]+)?", _read(path)):
            used[match] = str(path.relative_to(STATIC_ROOT))
    assert used, "the dashboard names no API path at all"
    for route, where in used.items():
        if route == "/api/v1":
            continue  # the prefix constant the route table is built from
        assert route in documented, f"{where} calls undocumented route {route}"


def test_the_route_table_covers_every_documented_route() -> None:
    """The dashboard may use fewer routes than exist, but never more."""
    api_js = _read(STATIC_ROOT / "js" / "api.js")
    declared = {f"/api/v1/{name}" for name in re.findall(r"\$\{API_PREFIX\}/([a-z]+)", api_js)}
    assert declared <= _documented_routes()
    assert declared, "js/api.js declares no routes"


def test_the_dashboard_only_uses_documented_query_parameters() -> None:
    """Parameter names passed to `get()` must appear in the contract."""
    app_js = _read(STATIC_ROOT / "js" / "app.js")
    contract = _read(API_CONTRACT)
    names = (
        "variable|variables|location|provider|from|to|kind|step|agg|max_points"
        "|max_age|window|bucket|issued_at|horizon_hours|step_hours|enabled"
    )
    used = set(re.findall(rf"\b({names})\b\s*:", app_js))
    for name in used:
        assert f"| `{name}` |" in contract, f"app.js sends undocumented parameter {name}"


# --- privacy ------------------------------------------------------------------

#: The repo-hygiene scan's keyword-anchored coordinate pattern, reused here.
_COORD_RE = re.compile(
    r"\b(?:lat(?:itude)?|lon(?:gitude)?)\b\s*[:=]\s*[\"']?-?(?!0+\.0+(?!\d))\d{1,3}\.\d+",
    re.IGNORECASE,
)

#: A bare decimal-degree pair, the other shape a coordinate leak takes.
_PAIR_RE = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")

#: Words that would name a place. The dashboard identifies a location only
#: by the user's own label, which the API supplies at runtime, so no place
#: name has any reason to be written down here.
_PLACE_WORDS = (
    "greenwich",
    "london",
    "tel aviv",
    "tel-aviv",
    "jerusalem",
    "beer sheva",
    "haifa",
    "llbg",
    "ben gurion",
    "israel",
    "oslo",
    "new york",
    "san francisco",
    "berlin",
    "paris",
)


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.name))
def test_no_coordinate_literal_in_a_static_file(path: Path) -> None:
    for lineno, line in enumerate(_read(path).splitlines(), start=1):
        where = f"{path.name}:{lineno}"
        assert not _COORD_RE.search(line), f"{where} carries a coordinate: {line.strip()}"
        assert not _PAIR_RE.search(line), f"{where} carries a coordinate pair: {line.strip()}"


def _place_hits(text: str) -> list[str]:
    """Whole-word matches only — `comparison` must not read as `paris`."""
    lowered = text.lower()
    return [
        word
        for word in _PLACE_WORDS
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
    ]


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.name))
def test_no_place_name_in_a_static_file(path: Path) -> None:
    hits = _place_hits(_read(path))
    assert not hits, f"{path.name} names a place: {hits}"


def test_the_place_word_guard_would_actually_fire() -> None:
    """Guard the guard: it fires on a real name and not on a longer word."""
    assert _place_hits("const label = 'Greenwich';") == ["greenwich"]
    assert _place_hits("the multi-provider comparison chart") == []


# --- design record ------------------------------------------------------------


DESIGN_FILES = (
    "README.md",
    "dashboard-light.png",
    "dashboard-dark.png",
    "dashboard-phone.png",
    "dashboard-empty.png",
)


@pytest.mark.parametrize("name", DESIGN_FILES)
def test_design_record_present(name: str) -> None:
    path = DESIGN_DIR / name
    assert path.is_file(), f"missing design record: {path}"
    assert path.stat().st_size > 0


def test_dev_server_never_squats_the_production_port() -> None:
    script = _read(REPO_ROOT / "scripts" / "dev_dashboard_server.py")
    assert "PRODUCTION_PORT = 8095" in script
    assert 'parser.error(f"port {PRODUCTION_PORT}' in script
