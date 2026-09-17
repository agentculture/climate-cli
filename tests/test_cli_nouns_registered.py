"""End-to-end tests that the weather nouns are wired into the real CLI.

Every assertion here goes through ``climate.cli.main([...])`` — the same
entry point the ``climate`` console script calls — so a noun that exists as
a module but was never registered in ``climate/cli/__init__.py`` fails here.

No socket is ever opened: the only verb that would talk to the network
(``weather latest``) has its single fetch seam monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from climate.cli import main
from climate.cli._commands import weather as weather_cmd

NOUNS = ("stack", "weather", "providers", "backup")


# --- bare noun + overview ---------------------------------------------------


@pytest.mark.parametrize("noun", NOUNS)
def test_bare_noun_exits_zero_with_output(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun])
    assert rc == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("noun", NOUNS)
def test_noun_overview_text(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun, "overview"])
    assert rc == 0
    assert f"# climate-cli {noun}" in capsys.readouterr().out


@pytest.mark.parametrize("noun", NOUNS)
def test_noun_overview_json(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun, "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == f"climate-cli {noun}"
    assert payload["sections"]


# --- providers --------------------------------------------------------------


def test_providers_bare_lists_adapters(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["providers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "| id |" in out
    assert "open-meteo" in out


def test_providers_json_has_every_adapter(capsys: pytest.CaptureFixture[str]) -> None:
    from climate.weather import providers as registry

    rc = main(["providers", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["providers"]}
    assert ids == set(registry.provider_ids())
    assert ids  # the registry is not empty


# --- weather latest (fetch seam monkeypatched) ------------------------------


def _fake_latest_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-09-17T00:00:00Z",
        "readings": [
            {
                "provider": "open-meteo",
                "location": "somewhere",
                "age_seconds": 30,
                "stale": False,
                "values": {
                    "temperature": {"value": 21.5, "unit": "degC", "age_seconds": 30},
                },
            }
        ],
        "missing": [],
        "warnings": [],
    }


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the weather module's single HTTP seam; record requested URLs."""
    seen: list[str] = []

    def _fetch(url: str, *, timeout: float = 0.0) -> weather_cmd.ApiResult:
        seen.append(url)
        body = json.dumps(_fake_latest_payload()).encode("utf-8")
        return weather_cmd.ApiResult(status=200, body=body)

    monkeypatch.setattr(weather_cmd, "fetch", _fetch)
    return seen


def test_weather_latest_markdown(fake_fetch: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["weather", "latest"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "temperature" in out
    assert "21.5" in out
    assert fake_fetch
    assert fake_fetch[0].endswith("/api/v1/latest")


def test_weather_latest_json(fake_fetch: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["weather", "latest", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["readings"][0]["provider"] == "open-meteo"
    assert payload["stale"] is False


def test_weather_latest_max_age_exits_stale(
    fake_fetch: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from climate.cli._errors import EXIT_STALE

    rc = main(["weather", "latest", "--max-age", "10s", "--json"])
    assert rc == EXIT_STALE
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] is True
