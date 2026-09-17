"""Neutral test point shared by the weather tests.

Location is private data in this project, so tests never write a real place.
Use these instead of inline coordinates (the repo hygiene test rejects
``latitude=<number>`` style literals outside ``tests/fixtures/``)::

    from tests.weather.neutral import NEUTRAL_LABEL, NEUTRAL_POINT
    location = Location(NEUTRAL_LABEL, *NEUTRAL_POINT)
"""

from __future__ import annotations

# Royal Observatory Greenwich, rounded to the default 2-decimal precision.
NEUTRAL_POINT: tuple[float, float] = (51.48, -0.0)
NEUTRAL_LABEL = "home"


def fake_secret(label: str) -> str:
    """A visibly fake credential for redaction tests, built at call time.

    Never type a key-shaped literal into a test: secret scanners (GitHub push
    protection, GitGuardian) match on shape and entropy, not on whether the
    value is live, and a flagged literal stays in git history after it is
    edited away. Words joined at runtime are not flagged and still prove the
    redaction logic.
    """
    return "-".join(["fake", label, "value", "for", "tests"])
