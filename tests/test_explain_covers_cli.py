"""Every command path the real parser accepts must resolve under ``explain``.

This walks the *actual* argparse tree built by
``climate.cli._build_parser()`` — not a hand-maintained list — so adding a
noun or a verb without a catalog entry fails here rather than leaving an
agent with an undocumented command.
"""

from __future__ import annotations

import argparse

import pytest

from climate.cli import _build_parser, main
from climate.explain import ENTRIES, resolve


def _walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path under ``parser``, depth-first."""
    paths: list[tuple[str, ...]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            path = prefix + (name,)
            paths.append(path)
            paths.extend(_walk(subparser, path))
    return paths


ALL_PATHS = _walk(_build_parser())


def test_walk_found_the_weather_nouns() -> None:
    # Guard the guard: if the walk silently found nothing, the coverage test
    # below would pass vacuously.
    assert ("stack", "up") in ALL_PATHS
    assert ("weather", "latest") in ALL_PATHS
    assert ("providers", "overview") in ALL_PATHS
    assert ("backup", "restore") in ALL_PATHS
    assert len(ALL_PATHS) >= 20


@pytest.mark.parametrize("path", ALL_PATHS, ids=lambda p: " ".join(p))
def test_every_command_path_resolves_in_explain(path: tuple[str, ...]) -> None:
    markdown = resolve(path)
    assert markdown.strip()


@pytest.mark.parametrize("path", ALL_PATHS, ids=lambda p: " ".join(p))
def test_every_command_path_has_its_own_catalog_entry(path: tuple[str, ...]) -> None:
    assert path in ENTRIES, f"climate explain {' '.join(path)} has no catalog entry"


def test_explain_verb_prints_a_noun_entry(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "weather", "latest"])
    assert rc == 0
    assert "latest" in capsys.readouterr().out


def test_root_entry_has_no_template_boilerplate() -> None:
    root = resolve(())
    assert "clonable template" not in root
    assert "weather" in root.lower()


def test_root_entry_documents_the_stale_exit_code() -> None:
    root = resolve(())
    assert "3" in root
    assert "stale" in root.lower()
