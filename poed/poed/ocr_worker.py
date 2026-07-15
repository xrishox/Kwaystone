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
    def __init__(self, helper: str):
        self.helper = helper
        self.proc: subprocess.Popen | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._out: queue.Queue[dict] = queue.Queue()

    def start(self, timeout: float = 180.0) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._start_locked(timeout)

    def request(self, path: str, timeout: float = 60.0) -> list[dict]:
        msg = self._request({"path": path}, timeout)
        return list(msg.get("lines") or [])

    def recognize(self, paths: list[str], timeout: float = 60.0) -> list[dict]:
        msg = self._request({"rec_paths": paths}, timeout)
        return list(msg.get("rec") or [])

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def _request(self, payload: dict, timeout: float) -> dict:
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start_locked(180.0)
            assert self.proc is not None and self.proc.stdin is not None
            self._seq += 1
            req_id = self._seq
            payload = dict(payload)
            payload["id"] = req_id
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OcrUnavailable("PaddleOCR helper timed out")
                msg = self._read_message(remaining)
                if msg.get("id") != req_id:
                    continue
                if not msg.get("ok"):
                    raise OcrUnavailable(str(msg.get("error") or "PaddleOCR helper failed"))
                return msg

    def _start_locked(self, timeout: float) -> None:
        if not Path(self.helper).exists():
            raise OcrUnavailable(f"PaddleOCR helper Python not found: {self.helper}")
        self._out = queue.Queue()
        try:
            self.proc = subprocess.Popen(
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
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrUnavailable("PaddleOCR helper startup timed out")
            msg = self._read_message(remaining)
            if msg.get("type") == "ready":
                device = msg.get("device")
                _LOG.info("PaddleOCR helper ready%s", f" on {device}" if device else "")
                return

    def _read_message(self, timeout: float) -> dict:
        """One message from the helper.

        Reads in short slices so a helper that crashes is reported with its
        real exit status immediately instead of after the full timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                # Drain any error the helper printed before dying.
                try:
                    msg = self._out.get_nowait()
                except queue.Empty:
                    raise OcrUnavailable(
                        f"PaddleOCR helper exited (code {proc.returncode})"
                    ) from None
                return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrUnavailable("PaddleOCR helper timed out")
            try:
                return self._out.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue

    def _pump_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
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
                self._out.put(msg)

    def _pump_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
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
