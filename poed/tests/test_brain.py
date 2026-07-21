import os

import pytest

from poed import brain as brain_mod
from poed.brain import Brain


def test_resolve_brain_dir_env_override(monkeypatch):
    monkeypatch.setenv("WAYSTONE_BRAIN_DIR", "/usr/lib/waystone/brain")

    assert brain_mod.resolve_brain_dir() == "/usr/lib/waystone/brain"


def test_resolve_brain_dir_defaults_to_checkout(monkeypatch):
    monkeypatch.delenv("WAYSTONE_BRAIN_DIR", raising=False)

    expected = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "../../brain")
    )
    assert brain_mod.resolve_brain_dir() == expected


def test_launch_command_prefers_bundle(tmp_path, monkeypatch):
    """Release installs ship dist/server.mjs — run it with plain node."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/server.mjs").touch()
    monkeypatch.setattr(brain_mod.shutil, "which", lambda n: f"/usr/bin/{n}")

    assert brain_mod.launch_command(str(tmp_path)) == ["/usr/bin/node", "dist/server.mjs"]


def test_launch_command_dev_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(brain_mod.shutil, "which", lambda n: f"/usr/bin/{n}")

    assert brain_mod.launch_command(str(tmp_path)) == ["/usr/bin/npx", "tsx", "src/server.ts"]


def test_launch_command_bundle_needs_node(tmp_path, monkeypatch):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/server.mjs").touch()
    monkeypatch.setattr(brain_mod.shutil, "which", lambda n: None)

    with pytest.raises(RuntimeError, match="node"):
        brain_mod.launch_command(str(tmp_path))


def test_launch_command_dev_needs_npx(tmp_path, monkeypatch):
    monkeypatch.setattr(brain_mod.shutil, "which", lambda n: None)

    with pytest.raises(RuntimeError, match="npx"):
        brain_mod.launch_command(str(tmp_path))


def test_ping_roundtrip(tmp_path):
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        assert b.request({"cmd": "ping"}) == "pong"
    finally:
        b.stop()
    assert not os.path.exists(sock)


def test_error_roundtrip_is_clean(tmp_path):
    """Socket protocol test: after a good ping, a brain-side error surfaces
    as a clean RuntimeError (not a hang or a broken connection)."""
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        assert b.request({"cmd": "ping"}) == "pong"
        with pytest.raises(RuntimeError, match="unknown cmd"):
            b.request({"cmd": "no-such-cmd"})
    finally:
        b.stop()


def test_stop_without_start_never_unlinks_socket(tmp_path):
    """Duplicate/early-exit instances must not pull the socket path from
    under a live peer's brain."""
    sock = tmp_path / "b.sock"
    sock.touch()
    b = Brain(brain_dir="unused", socket_path=str(sock))

    b.stop()

    assert sock.exists()


def test_request_with_missing_socket_is_actionable(tmp_path):
    b = Brain(brain_dir="unused", socket_path=str(tmp_path / "gone.sock"))

    with pytest.raises(RuntimeError, match="brain socket missing"):
        b.request({"cmd": "ping"})
