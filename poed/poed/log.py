"""Unified logging: poed and the brain child share one rotating file.

Everything logs under the "waystone" logger — stderr for the terminal,
plus ~/.local/state/waystone/waystone.log so errors are traceable after
the fact. Brain stderr is pumped line-by-line into the same file (see
pump_pipe), interleaved with poed's own records. The poesessid value is
never logged anywhere — keep it that way.
"""
import logging
import logging.handlers
import os
import threading
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    return state / "waystone" / "waystone.log"


def setup(debug: bool = False) -> logging.Logger:
    """Configure the waystone logger once; safe to call again (handlers reset)."""
    logger = logging.getLogger("waystone")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(stream)

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2
        )
        file.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(file)
    except OSError:
        logger.warning("cannot open %s; logging to stderr only", path)
    return logger


def pump_pipe(pipe) -> None:
    """Forward a child's stderr lines into the brain logger until EOF."""
    brain_logger = logging.getLogger("waystone.brain")
    with pipe:
        for raw in pipe:
            line = raw.decode(errors="replace").rstrip()
            if line:
                brain_logger.info("%s", line)


def pump_in_thread(pipe) -> threading.Thread:
    t = threading.Thread(target=pump_pipe, args=(pipe,), daemon=True)
    t.start()
    return t
