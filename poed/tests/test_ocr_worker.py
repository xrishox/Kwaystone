"""PaddleOcrWorker protocol/lifecycle tests with stub helper processes."""

import subprocess
import sys
import threading
import time

import pytest

from poed import ocr_paddle, ocr_worker


READY_ECHO = """
import json, sys
print(json.dumps({"type": "ready", "device": "cpu"}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({"id": req.get("id"), "ok": True, "lines": []}), flush=True)
"""

HANG_ON_REQUEST = """
import json, sys, time
print(json.dumps({"type": "ready", "device": "cpu"}), flush=True)
for line in sys.stdin:
    time.sleep(30)
"""

NEVER_READY = """
import time
time.sleep(30)
"""

DIE_ON_REQUEST = """
import json, sys
print(json.dumps({"type": "ready", "device": "cpu"}), flush=True)
for line in sys.stdin:
    sys.exit(3)
"""

READY_THEN_EXIT = """
import json, sys, time
print(json.dumps({"type": "ready", "device": "cpu"}), flush=True)
time.sleep(0.2)
sys.exit(0)
"""


@pytest.fixture
def stub_factory(monkeypatch, tmp_path):
    """Point the worker's Popen at a stub script instead of poed.ocr_paddle."""
    real_popen = subprocess.Popen

    def install(body: str) -> None:
        script = tmp_path / "stub_helper.py"
        script.write_text(body)
        monkeypatch.setattr(
            ocr_worker.subprocess,
            "Popen",
            lambda _cmd, **kw: real_popen([sys.executable, str(script)], **kw),
        )

    return install


@pytest.fixture
def worker():
    w = ocr_worker.PaddleOcrWorker(helper=sys.executable)
    yield w
    w.stop()


def test_start_and_request_roundtrip(stub_factory, worker):
    stub_factory(READY_ECHO)
    worker.start(timeout=10.0)
    assert worker.request("anything.png", timeout=5.0) == []


def test_startup_timeout_terminates_and_allows_retry(stub_factory, worker):
    stub_factory(NEVER_READY)
    with pytest.raises(ocr_worker.OcrUnavailable, match="startup"):
        worker.start(timeout=0.5)
    # The half-started helper was terminated and is not mistaken for ready.
    assert worker.proc is None

    stub_factory(READY_ECHO)
    worker.start(timeout=10.0)
    assert worker.request("x", timeout=5.0) == []


def test_request_timeout_discards_wedged_helper_and_respawns(stub_factory, worker):
    stub_factory(HANG_ON_REQUEST)
    worker.start(timeout=10.0)
    with pytest.raises(ocr_worker.OcrUnavailable, match="timed out"):
        worker.request("x", timeout=0.5)
    # The wedged helper was discarded, not reused for the next call.
    assert worker.proc is None

    stub_factory(READY_ECHO)
    assert worker.request("x", timeout=10.0) == []


def test_helper_death_mid_request_reports_exit(stub_factory, worker):
    stub_factory(DIE_ON_REQUEST)
    worker.start(timeout=10.0)
    with pytest.raises(ocr_worker.OcrUnavailable, match="exited"):
        worker.request("x", timeout=10.0)
    assert worker.proc is None or worker.proc.poll() is not None


def test_dead_helper_request_raises_unavailable_not_broken_pipe(stub_factory, worker):
    stub_factory(READY_THEN_EXIT)
    worker.start(timeout=10.0)
    time.sleep(0.5)  # let the helper exit fully
    with pytest.raises(ocr_worker.OcrUnavailable):
        worker.request("x", timeout=5.0)


def test_stop_returns_promptly_with_wedged_request(stub_factory, worker):
    stub_factory(HANG_ON_REQUEST)
    worker.start(timeout=10.0)
    outcome = {}

    def do_request():
        try:
            worker.request("x", timeout=30.0)
        except ocr_worker.OcrUnavailable as e:
            outcome["error"] = str(e)

    thread = threading.Thread(target=do_request, daemon=True)
    thread.start()
    time.sleep(0.3)
    started = time.monotonic()
    worker.stop()
    elapsed = time.monotonic() - started
    thread.join(timeout=10.0)
    # stop() must not wait behind the wedged 30s request.
    assert elapsed < 5.0
    assert not thread.is_alive()


def test_rec_paths_rejects_misaligned_read_count():
    class FakeRec:
        def predict(self, paths, batch_size):
            return []  # silently skipped images

    with pytest.raises(RuntimeError, match="reads for 2 images"):
        ocr_paddle._rec_paths(FakeRec(), ["a.png", "b.png"])


def test_serve_survives_malformed_lines(monkeypatch, capsys):
    import io

    monkeypatch.setattr(ocr_paddle, "_configure_device", lambda: "cpu")
    monkeypatch.setattr(
        ocr_paddle, "_configured_model_names", lambda device: ("d", "r", "r")
    )
    monkeypatch.setattr(ocr_paddle, "_make_ocr", lambda: object())
    monkeypatch.setattr(ocr_paddle, "_recognizer_from_ocr", lambda ocr: object())
    monkeypatch.setattr(ocr_paddle, "_ocr_path", lambda ocr, path: [{"text": "x"}])
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{garbage\n{"id": 7, "path": "x"}\n')
    )

    assert ocr_paddle.serve() == 0

    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    import json

    ready, error, ok = (json.loads(line) for line in out)
    assert ready["type"] == "ready"
    # Malformed line: one error response with a null id, then normal service.
    assert error == {"id": None, "ok": False, "error": error["error"]}
    assert ok == {"id": 7, "ok": True, "lines": [{"text": "x"}]}
