#!/usr/bin/env python3
"""CLI shim for the guidance linter.

The real implementation lives in the `scorched` package so it ships inside
the Docker image. This file exists only so the developer ergonomics stay
the same — `python3 scripts/guidance_lint.py` from the repo root still works.

For CI and production use, prefer `python3 -m scorched.services.guidance_lint`
or import `scorched.services.guidance_lint.lint()` directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when invoked from a checkout that hasn't been
# pip-installed (dev flow). In Docker, scorched is already on sys.path.
_HERE = Path(__file__).resolve().parent.parent
_SRC = _HERE / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scorched.services.guidance_lint import main

if __name__ == "__main__":
    raise SystemExit(main())
