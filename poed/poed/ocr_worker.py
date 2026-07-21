"""Persistent PaddleOCR helper process used by screen scanners.

The main GTK process should not import PaddleOCR directly.  This module owns
the helper subprocess, request/response protocol, and crop preprocessing so
scanner modules can share one OCR runtime without depending on each other.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

_LOG = logging.getLogger("waystone.ocr")
OCR_SCALE = 1.0


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class OcrLine:
    text: str
    score: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in crop pixels


class PaddleOcrWorker:
    """Owns one helper process and its request/response pipe.

    Lock discipline (shutdown must never hang behind a slow OCR call):
      - _state_lock: tiny, guards proc/_out/_seq field swaps only.
      - _spawn_lock: serializes helper spawns (held across the readiness
        wait; contended only by threads that also want to spawn).
      - _request_lock: serializes request/response on the pipe (one
        outstanding request; the protocol would interleave otherwise).
    stop() takes none of the long-held locks: it swaps the proc reference
    under _state_lock and terminates outside any lock.
    """

    def __init__(self, helper: str):
        self.helper = helper
        self.proc: subprocess.Popen | None = None
        self._seq = 0
        self._state_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._out: queue.Queue[dict] = queue.Queue()

    def start(self, timeout: float = 180.0) -> None:
        self._spawn(timeout)

    def request(self, path: str, timeout: float = 60.0) -> list[dict]:
        msg = self._request({"path": path}, timeout)
        return list(msg.get("lines") or [])

    def recognize(self, paths: list[str], timeout: float = 60.0) -> list[dict]:
        msg = self._request({"rec_paths": paths}, timeout)
        return list(msg.get("rec") or [])

    def stop(self) -> None:
        with self._state_lock:
            proc, self.proc = self.proc, None
        self._terminate(proc)

    def _request(self, payload: dict, timeout: float) -> dict:
        with self._request_lock:
            proc = self._spawn(180.0)
            if proc.stdin is None:
                self._discard(proc)
                raise OcrUnavailable("PaddleOCR helper has no stdin")
            with self._state_lock:
                self._seq += 1
                req_id = self._seq
                out = self._out
            payload = dict(payload)
            payload["id"] = req_id
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                # Dead or wedged helper (broken pipe, closed stdin): drop it
                # so the next call respawns instead of failing forever.
                self._discard(proc)
                raise OcrUnavailable(f"PaddleOCR helper write failed: {e}") from e
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Wedged (hung, not dead) helper: kill it so the next
                    # call respawns rather than stacking 60s timeouts.
                    self._discard(proc)
                    raise OcrUnavailable("PaddleOCR helper timed out")
                try:
                    msg = self._read_message(proc, out, remaining)
                except OcrUnavailable:
                    # Any transport failure (timeout, helper exit): drop the
                    # helper so the next call respawns instead of reusing a
                    # wedged or dead process.
                    self._discard(proc)
                    raise
                if msg.get("id") != req_id:
                    continue
                if not msg.get("ok"):
                    raise OcrUnavailable(str(msg.get("error") or "PaddleOCR helper failed"))
                return msg

    def _spawn(self, timeout: float) -> subprocess.Popen:
        with self._state_lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            return proc
        with self._spawn_lock:
            # Re-check under the spawn lock: another thread may have spawned.
            with self._state_lock:
                proc = self.proc
            if proc is not None and proc.poll() is None:
                return proc
            if not Path(self.helper).exists():
                raise OcrUnavailable(f"PaddleOCR helper Python not found: {self.helper}")
            out: queue.Queue[dict] = queue.Queue()
            try:
                new_proc = subprocess.Popen(
                    [self.helper, "-m", "poed.ocr_paddle", "--server"],
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                raise OcrUnavailable(f"PaddleOCR helper failed: {e}") from e
            threading.Thread(
                target=self._pump_stdout, args=(new_proc, out), daemon=True
            ).start()
            threading.Thread(
                target=self._pump_stderr, args=(new_proc,), daemon=True
            ).start()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # A half-started helper must not linger and must never be
                    # mistaken for a ready one by the next start attempt.
                    self._terminate(new_proc)
                    raise OcrUnavailable("PaddleOCR helper startup timed out")
                try:
                    msg = self._read_message(new_proc, out, remaining)
                except OcrUnavailable as e:
                    self._terminate(new_proc)
                    raise OcrUnavailable(
                        f"PaddleOCR helper startup failed: {e}"
                    ) from e
                if msg.get("type") == "ready":
                    break
            with self._state_lock:
                self.proc = new_proc
                self._out = out
            device = msg.get("device")
            _LOG.info("PaddleOCR helper ready%s", f" on {device}" if device else "")
            return new_proc

    def _discard(self, proc: subprocess.Popen) -> None:
        """Forget and terminate a helper that can no longer be trusted."""
        with self._state_lock:
            if self.proc is proc:
                self.proc = None
        self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            # Reap after kill: no zombie left behind.
            try:
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

    def _read_message(
        self, proc: subprocess.Popen, out: "queue.Queue[dict]", timeout: float
    ) -> dict:
        """One message from a specific helper/queue generation.

        Reads in short slices so a helper that crashes is reported with its
        real exit status immediately instead of after the full timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            if proc.poll() is not None:
                # Drain any error the helper printed before dying.
                try:
                    msg = out.get_nowait()
                except queue.Empty:
                    raise OcrUnavailable(
                        f"PaddleOCR helper exited (code {proc.returncode})"
                    ) from None
                return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrUnavailable("PaddleOCR helper timed out")
            try:
                return out.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue

    def _pump_stdout(self, proc: subprocess.Popen, out: "queue.Queue[dict]") -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _LOG.debug("PaddleOCR helper stdout: %s", line)
                continue
            if isinstance(msg, dict):
                out.put(msg)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.strip()
            if line:
                _LOG.debug("PaddleOCR helper: %s", line)


_worker: PaddleOcrWorker | None = None
_worker_lock = threading.Lock()


def _helper_python() -> str:
    configured = os.environ.get("WAYSTONE_PADDLE_PYTHON")
    if configured:
        return configured
    return sys.executable


def _worker_for_helper() -> PaddleOcrWorker:
    global _worker
    helper = _helper_python()
    with _worker_lock:
        if _worker is None or _worker.helper != helper:
            if _worker is not None:
                _worker.stop()
            _worker = PaddleOcrWorker(helper)
        return _worker


def preprocess(crop: np.ndarray) -> np.ndarray:
    up = cv2.resize(crop, None, fx=OCR_SCALE, fy=OCR_SCALE, interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(up, (0, 0), 1.0)
    return cv2.addWeighted(up, 1.45, blur, -0.45, 0)


def read_lines(crop: np.ndarray) -> list[OcrLine]:
    if crop.size == 0:
        # Degenerate crops (extreme aspect ratios, off-frame geometry) would
        # make cv2.resize raise a raw cv2.error — decline cleanly instead.
        return []
    img = preprocess(crop)
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        if not cv2.imwrite(tmp.name, img):
            raise RuntimeError("could not write OCR crop")
        payload = _worker_for_helper().request(tmp.name)

    lines = [
        OcrLine(str(row["text"]), float(row.get("score") or 0), tuple(row["box"]))
        for row in payload
        if row.get("text") and row.get("box")
    ]
    scaled = []
    for line in lines:
        x0, y0, x1, y1 = line.box
        scaled.append(
            OcrLine(
                line.text,
                line.score,
                (x0 / OCR_SCALE, y0 / OCR_SCALE, x1 / OCR_SCALE, y1 / OCR_SCALE),
            )
        )
    return scaled


def recognize_images(paths: list[str], timeout: float = 60.0) -> list[dict]:
    return _worker_for_helper().recognize(paths, timeout)


def recognize_arrays(images: list, timeout: float = 60.0) -> list[dict]:
    """Recognition-only OCR over in-memory BGR arrays.

    Owns the temp-file protocol the helper needs; results align with
    ``images`` by index. Images that fail to encode yield ``{}`` so the
    alignment is preserved.
    """

    if not images:
        return []
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="waystone-ocr-rec-") as tmp:
        tmp_dir = Path(tmp)
        paths: list[str] = []
        indexes: list[int] = []
        for index, image in enumerate(images):
            path = tmp_dir / f"rec-{index:04d}.png"
            if cv2.imwrite(str(path), image):
                paths.append(str(path))
                indexes.append(index)
        reads = recognize_images(paths, timeout) if paths else []
    out: list[dict] = [{} for _ in images]
    for index, read in zip(indexes, reads):
        out[index] = read
    return out


def warm() -> bool:
    try:
        _worker_for_helper().start()
        return True
    except OcrUnavailable as e:
        _LOG.warning("PaddleOCR warm failed: %s", e)
        return False


def stop() -> None:
    global _worker
    with _worker_lock:
        worker = _worker
        _worker = None
    if worker is not None:
        worker.stop()


# Compatibility aliases for older private tests/callers.
_PaddleWorker = PaddleOcrWorker
_preprocess = preprocess
