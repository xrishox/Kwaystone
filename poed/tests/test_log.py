import logging
import os

from poed import log


def _reset():
    lg = logging.getLogger("waystone")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()


def test_setup_writes_to_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    try:
        lg = log.setup()
        lg.info("hello waystone")
        logfile = tmp_path / "waystone/waystone.log"
        assert logfile.exists()
        assert "hello waystone" in logfile.read_text()
    finally:
        _reset()


def test_default_level_skips_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    try:
        lg = log.setup()
        lg.debug("quiet")
        lg.info("loud")
        text = (tmp_path / "waystone/waystone.log").read_text()
        assert "quiet" not in text
        assert "loud" in text
    finally:
        _reset()


def test_debug_flag_enables_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    try:
        lg = log.setup(debug=True)
        lg.debug("verbose")
        assert "verbose" in (tmp_path / "waystone/waystone.log").read_text()
    finally:
        _reset()


def test_pump_logs_child_lines(tmp_path, monkeypatch):
    """Brain child stderr lines land in the shared log, tagged as brain."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    try:
        log.setup()
        r, w = os.pipe()
        os.write(w, b"brain listening on /tmp/x.sock\nsome error\n")
        os.close(w)
        log.pump_pipe(os.fdopen(r, "rb"))  # runs to EOF synchronously
        text = (tmp_path / "waystone/waystone.log").read_text()
        assert "brain listening on /tmp/x.sock" in text
        assert "some error" in text
        assert "brain" in text
    finally:
        _reset()
