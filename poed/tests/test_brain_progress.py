import json
import socket
import threading

from poed.brain import Brain


def _serve(path: str, lines: list[dict]) -> threading.Thread:
    """One-shot stub brain: accept one connection, read one request line,
    write `lines` as JSON-lines, close."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        f.readline()  # consume the request; content irrelevant to the stub
        for line in lines:
            conn.sendall((json.dumps(line) + "\n").encode())
        conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_request_collects_progress_in_order(tmp_path):
    sock = str(tmp_path / "b.sock")
    t = _serve(sock, [
        {"id": 1, "progress": "exchange"},
        {"id": 1, "progress": "listings"},
        {"id": 1, "ok": True, "result": {"kind": "price"}},
    ])
    stages = []
    result = Brain("unused", sock).request(
        {"cmd": "price"}, timeout=5.0, on_progress=stages.append
    )
    t.join(2)
    assert stages == ["exchange", "listings"]
    assert result == {"kind": "price"}


def test_request_without_callback_skips_progress(tmp_path):
    sock = str(tmp_path / "b.sock")
    t = _serve(sock, [
        {"id": 1, "progress": "exchange"},
        {"id": 1, "ok": True, "result": "r"},
    ])
    assert Brain("unused", sock).request({"cmd": "price"}, timeout=5.0) == "r"
    t.join(2)


def test_request_survives_raising_progress_callback(tmp_path):
    sock = str(tmp_path / "b.sock")
    t = _serve(sock, [
        {"id": 1, "progress": "exchange"},
        {"id": 1, "ok": True, "result": "r"},
    ])

    def boom(_stage):
        raise RuntimeError("ui bug")

    assert Brain("unused", sock).request(
        {"cmd": "price"}, timeout=5.0, on_progress=boom
    ) == "r"
    t.join(2)
