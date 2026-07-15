"""Shared helpers for poed tests."""

from __future__ import annotations

import os


def local_debug_tests_enabled() -> bool:
    """True when the developer opted into tests that read retained local debug scans."""
    return os.environ.get("WAYSTONE_RUN_LOCAL_DEBUG_TESTS") == "1"
