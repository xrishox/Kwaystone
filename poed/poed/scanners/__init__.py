"""Screen scanner extensions used by the unified Alt+X scan path."""

from .common import update_debug_manifest
from .core import run, warm, stop
from .types import Detection, ScanContext, ScanResult, Scanner

__all__ = [
    "Detection",
    "ScanContext",
    "ScanResult",
    "Scanner",
    "run",
    "update_debug_manifest",
    "warm",
    "stop",
]
