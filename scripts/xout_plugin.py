#!/usr/bin/env python3
"""Run the bundled xout package without changing the caller's cwd."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from xout.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
