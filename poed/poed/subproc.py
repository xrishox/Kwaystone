"""Shared subprocess hygiene for host tools (hyprctl, xdotool, wl-paste, grim).

Two concerns live here instead of being scattered across call sites:

- Environment scrubbing: the app re-execs with LD_PRELOAD (gtk4-layer-shell)
  and AppImage builds set LD_LIBRARY_PATH for bundled libs. Both poison HOST
  binaries spawned as children (they would load our GTK closure or bundled
  incompatible libs), so host-tool spawns run with a scrubbed environment.
- Missing-tool diagnostics: every call site degrades silently by design;
  the one place that must speak is a single startup probe that names the
  missing tools once.
"""

import logging
import os
import shutil

_LOG = logging.getLogger("waystone.process")


def scrubbed_env() -> dict:
    """os.environ without the LD_* overrides that poison host binaries."""
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def report_missing_tools(names) -> None:
    """Warn once per missing host tool at startup (call sites stay silent)."""
    for name in names:
        if shutil.which(name) is None:
            _LOG.warning(
                "host tool not found on PATH: %s — related features will degrade",
                name,
            )
