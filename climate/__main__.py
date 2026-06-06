"""Entry point for ``python -m climate``."""

from __future__ import annotations

import sys

from climate.cli import main

if __name__ == "__main__":
    sys.exit(main())
