import os
from poed.brain import Brain


def test_ping_roundtrip(tmp_path):
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        assert b.request({"cmd": "ping"}) == "pong"
    finally:
        b.stop()


def test_parse_error_is_clean(tmp_path):
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        b.request({"cmd": "parse", "clipboard": "garbage"})
        assert False, "should raise"
    except RuntimeError as e:
        assert "not an item" in str(e)
    finally:
        b.stop()
