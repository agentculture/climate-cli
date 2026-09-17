"""Tests for scripts/check-culture-design.py's comparison logic.

These tests never depend on /home/spark/git/org existing: they build a
throwaway "fake org" git repo under a pytest tmp_path, commit a known
global.css body to it, and point the script's ORG_REPO at that fake repo
instead. That keeps the test hermetic and CI-safe (CI has no org checkout).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-culture-design.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_culture_design", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ccd = _load_module()

SAMPLE_BODY = "/* ---------- tokens ---------- */\n\n:root {\n  --bg: #f4f5fb;\n}\n"
OTHER_BODY = "/* ---------- tokens ---------- */\n\n:root {\n  --bg: #000000;\n}\n"


def _init_fake_org_repo(tmp_path: Path, body: str) -> tuple[Path, str]:
    """Create a fake org git repo with one commit holding `body` at
    site-astro/src/styles/global.css. Returns (repo_path, commit_sha)."""
    org_repo = tmp_path / "fake-org"
    org_repo.mkdir()
    css_dir = org_repo / "site-astro" / "src" / "styles"
    css_dir.mkdir(parents=True)
    (css_dir / "global.css").write_text(body, encoding="utf-8")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(org_repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-q", "-m", "seed")
    sha = run("rev-parse", "HEAD").stdout.strip()
    return org_repo, sha


def _tokens_css_text(pin: str, body: str) -> str:
    header = "/*\n" " * climate-cli test fixture header.\n" f" * Pinned commit: {pin}\n" " */\n\n"
    return header + body


def _adr_text(pin: str) -> str:
    return f"# ADR fixture\n\npin:       {pin}\n"


class TestSplitHeaderAndBody:
    def test_splits_header_and_body(self) -> None:
        tokens_css = _tokens_css_text("a" * 40, SAMPLE_BODY)
        header, body = ccd.split_header_and_body(tokens_css)
        assert header.startswith("/*")
        assert header.endswith("*/")
        assert body == SAMPLE_BODY

    def test_missing_header_raises(self) -> None:
        with pytest.raises(ccd.CheckFailed):
            ccd.split_header_and_body("no header here\n")


class TestPinExtraction:
    def test_reads_pin_from_adr(self) -> None:
        pin = "b4d939ba0aa354a5ae53065319a773e0013de698"
        assert ccd.read_pin_from_adr(_adr_text(pin)) == pin

    def test_missing_pin_in_adr_raises(self) -> None:
        with pytest.raises(ccd.CheckFailed):
            ccd.read_pin_from_adr("# ADR fixture\n\nno pin line\n")

    def test_reads_pin_from_tokens_header(self) -> None:
        pin = "b4d939ba0aa354a5ae53065319a773e0013de698"
        tokens_css = _tokens_css_text(pin, SAMPLE_BODY)
        assert ccd.read_pin_from_tokens_header(tokens_css) == pin


class TestCheckAgainstFakeOrgRepo:
    def test_byte_match_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        org_repo, sha = _init_fake_org_repo(tmp_path, SAMPLE_BODY)
        monkeypatch.setattr(ccd, "ORG_REPO", org_repo)

        tokens_css = _tokens_css_text(sha, SAMPLE_BODY)
        adr_text = _adr_text(sha)

        # Should not raise.
        ccd.check_tokens_match_org(tokens_css, adr_text)

    def test_byte_mismatch_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        org_repo, sha = _init_fake_org_repo(tmp_path, OTHER_BODY)
        monkeypatch.setattr(ccd, "ORG_REPO", org_repo)

        # tokens.css claims the same pin but its body diverges from what's
        # actually at that pin in the (fake) org repo.
        tokens_css = _tokens_css_text(sha, SAMPLE_BODY)
        adr_text = _adr_text(sha)

        with pytest.raises(ccd.CheckFailed, match="no longer byte-matches"):
            ccd.check_tokens_match_org(tokens_css, adr_text)

    def test_pin_mismatch_between_adr_and_header_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org_repo, sha = _init_fake_org_repo(tmp_path, SAMPLE_BODY)
        monkeypatch.setattr(ccd, "ORG_REPO", org_repo)

        tokens_css = _tokens_css_text(sha, SAMPLE_BODY)
        other_pin = "c" * 40
        adr_text = _adr_text(other_pin)

        with pytest.raises(ccd.CheckFailed, match="pin mismatch"):
            ccd.check_tokens_match_org(tokens_css, adr_text)

    def test_missing_org_repo_skips_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing_repo = tmp_path / "does-not-exist"
        monkeypatch.setattr(ccd, "ORG_REPO", missing_repo)

        pin = "d" * 40
        tokens_css = _tokens_css_text(pin, SAMPLE_BODY)
        adr_text = _adr_text(pin)

        with pytest.raises(ccd.CheckSkipped):
            ccd.check_tokens_match_org(tokens_css, adr_text)

    def test_unreachable_pin_skips_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org_repo, _sha = _init_fake_org_repo(tmp_path, SAMPLE_BODY)
        monkeypatch.setattr(ccd, "ORG_REPO", org_repo)

        unreachable_pin = "e" * 40
        tokens_css = _tokens_css_text(unreachable_pin, SAMPLE_BODY)
        adr_text = _adr_text(unreachable_pin)

        with pytest.raises(ccd.CheckSkipped):
            ccd.check_tokens_match_org(tokens_css, adr_text)


class TestMainAgainstRealFile:
    def test_main_runs_against_repo_tokens_css(self) -> None:
        """Smoke test: main() against the real committed tokens.css/ADR
        must exit 0. If /home/spark/git/org happens to be present with the
        pinned commit, it verifies byte-equality for real; otherwise the
        script's own skip path (also exit 0) covers it. Either way this
        test never depends on that checkout existing."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
