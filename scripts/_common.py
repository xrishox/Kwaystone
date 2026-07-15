"""Shared preamble for the extensionless Python scripts in this directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bootstrap() -> None:
    """Re-exec into the managed venv, then make ``poed`` importable.

    Preserves ``sys.argv`` across the re-exec so script arguments behave
    identically whether or not the managed interpreter was already running.
    """
    managed_python = ROOT / ".venv/bin/python"
    if managed_python.exists() and Path(sys.executable).resolve() != managed_python.resolve():
        os.execv(str(managed_python), [str(managed_python), *sys.argv])
    sys.path.insert(0, str(ROOT / "poed"))
