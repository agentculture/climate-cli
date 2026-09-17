#!/usr/bin/env python3
"""Verify climate/weather/web/static/tokens.css stays faithful to the pinned
agentculture/org revision recorded in docs/adr/0001-culture-design-source.md
(task t7).

Check: tokens.css's copied body (everything after its header comment) is
byte-identical to org's site-astro/src/styles/global.css AT THE RECORDED
PIN, read via `git show <pin>:<path>` — never org's working tree / current
HEAD, so org's HEAD is free to move on without failing this check.

Skips cleanly (exit 0, printing a "skipped" message) when the org checkout
is not present on disk, or when the pinned commit is not reachable in it
(for example, in CI, which has no /home/spark/git/org checkout). Fails
(non-zero exit) on an actual byte mismatch or a malformed ADR/header.

Python 3.12 stdlib only; no third-party dependencies.

Run with: python3 scripts/check-culture-design.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TOKENS_CSS_PATH = REPO_ROOT / "climate" / "weather" / "web" / "static" / "tokens.css"
ADR_PATH = REPO_ROOT / "docs" / "adr" / "0001-culture-design-source.md"

ORG_REPO = Path("/home/spark/git/org")
ORG_TOKENS_PATH = "site-astro/src/styles/global.css"

PIN_RE = re.compile(r"^pin:\s+([0-9a-f]{40})$", re.MULTILINE)
HEADER_PIN_RE = re.compile(r"Pinned commit:\s*([0-9a-f]{40})")


class CheckSkipped(Exception):
    """Raised when the comparison cannot be performed and should be skipped."""


class CheckFailed(Exception):
    """Raised on an actual verification failure."""


def read_pin_from_adr(adr_text: str) -> str:
    match = PIN_RE.search(adr_text)
    if not match:
        raise CheckFailed(f"could not find a 'pin:  <40-hex-char-sha>' line in {ADR_PATH}")
    return match.group(1)


def read_pin_from_tokens_header(tokens_css: str) -> str:
    match = HEADER_PIN_RE.search(tokens_css)
    if not match:
        raise CheckFailed("tokens.css header comment is missing a 'Pinned commit:  <sha>' line")
    return match.group(1)


def split_header_and_body(tokens_css: str) -> tuple[str, str]:
    """Split tokens.css into (header comment, verbatim body).

    The header is the leading `/* ... */` comment block; the body is
    everything after it, with a single leading blank line stripped (the
    header/body separator).
    """
    if not tokens_css.startswith("/*"):
        raise CheckFailed("tokens.css does not start with a header comment block")
    end = tokens_css.find("*/")
    if end == -1:
        raise CheckFailed("tokens.css header comment block is never closed with */")
    header = tokens_css[: end + 2]
    rest = tokens_css[end + 2 :]
    # Strip exactly one separating blank line after the header comment.
    if rest.startswith("\n\n"):
        body = rest[2:]
    elif rest.startswith("\n"):
        body = rest[1:]
    else:
        body = rest
    return header, body


def read_org_file_at_pin(pin: str) -> str:
    if not ORG_REPO.is_dir():
        raise CheckSkipped(f"org checkout not present at {ORG_REPO}")
    try:
        result = subprocess.run(
            ["git", "-C", str(ORG_REPO), "show", f"{pin}:{ORG_TOKENS_PATH}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CheckSkipped(f"git binary unavailable: {exc}") from exc
    if result.returncode != 0:
        raise CheckSkipped(
            f"pinned commit {pin} (or {ORG_TOKENS_PATH} within it) is not available "
            f"in {ORG_REPO}: {result.stderr.strip()}"
        )
    return result.stdout


def check_tokens_match_org(tokens_css: str, adr_text: str) -> None:
    adr_pin = read_pin_from_adr(adr_text)
    header_pin = read_pin_from_tokens_header(tokens_css)
    if adr_pin != header_pin:
        raise CheckFailed(
            f"pin mismatch: ADR records {adr_pin!r} but tokens.css header says {header_pin!r}"
        )

    _, body = split_header_and_body(tokens_css)
    org_body = read_org_file_at_pin(adr_pin)

    if body != org_body:
        raise CheckFailed(
            "tokens.css body no longer byte-matches org's "
            f"{ORG_TOKENS_PATH} at pin {adr_pin} — re-copy per the ADR's re-pin procedure"
        )


def main() -> int:
    if not TOKENS_CSS_PATH.is_file():
        print(f"FAIL - {TOKENS_CSS_PATH} does not exist", file=sys.stderr)
        return 1
    if not ADR_PATH.is_file():
        print(f"FAIL - {ADR_PATH} does not exist", file=sys.stderr)
        return 1

    tokens_css = TOKENS_CSS_PATH.read_text(encoding="utf-8")
    adr_text = ADR_PATH.read_text(encoding="utf-8")

    try:
        check_tokens_match_org(tokens_css, adr_text)
    except CheckSkipped as exc:
        print(f"skipped - {exc}")
        return 0
    except CheckFailed as exc:
        print(f"FAIL - {exc}", file=sys.stderr)
        return 1

    print("ok - tokens.css matches org's pinned site-astro/src/styles/global.css")
    return 0


if __name__ == "__main__":
    sys.exit(main())
