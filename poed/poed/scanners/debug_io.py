"""Background executor for debug-scan persistence.

Every Alt+X scan records a full-monitor capture PNG, per-stage crops, a
summary JPEG, a manifest, and runs retention pruning.  Encoding and disk
I/O for those took 400-600ms on the hot path between "matches computed"
and "prices on screen".  This module serializes all of that work onto one
daemon thread: submission order is execution order, so manifest
read-modify-writes and retention pruning behave exactly as before — just
after the result has already been delivered.

Images handed to the queue must not be mutated afterwards; scan code
treats captures as immutable and stage overlays are freshly composed
copies.  ``flush()`` drains the queue (used by tests, tools, and app
shutdown).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

_LOG = logging.getLogger("waystone.scanners.debug")

# Image payloads keep full frames alive while queued; bound the bytes, not
# the queue depth — manifest/prune jobs are tiny and must never be dropped.
_MAX_PENDING_IMAGE_BYTES = 512 * 1024 * 1024

_queue: queue.Queue[Callable[[], None]] = queue.Queue()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_image_bytes = 0
_image_bytes_lock = threading.Lock()


def _run() -> None:
    while True:
        job = _queue.get()
        try:
            job()
        except Exception:  # noqa: BLE001 - debug persistence must never crash scans
            _LOG.exception("debug write failed")
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_run, name="waystone-debug-io", daemon=True
            )
            _worker.start()


def submit(job: Callable[[], None]) -> None:
    """Queue a persistence job; never blocks the scan hot path."""

    _ensure_worker()
    _queue.put_nowait(job)


def submit_image(job: Callable[[], None], size_bytes: int) -> None:
    """Queue an image write unless the outstanding image budget is spent."""

    global _image_bytes
    with _image_bytes_lock:
        if _image_bytes + size_bytes > _MAX_PENDING_IMAGE_BYTES:
            _LOG.warning("debug image backlog full; dropping one debug image")
            return
        _image_bytes += size_bytes

    def run_and_release() -> None:
        global _image_bytes
        try:
            job()
        finally:
            with _image_bytes_lock:
                _image_bytes -= size_bytes

    submit(run_and_release)


# key -> True when another request arrived while a job was already pending;
# the finishing job re-queues itself so the last request is always honored.
_pending_keys: dict[str, bool] = {}
_pending_lock = threading.Lock()


def submit_keyed(key: str, job: Callable[[], None]) -> None:
    """Coalesce jobs by key: at most one queued at a time, and a request
    arriving while one is pending guarantees one more run afterwards.

    Used for retention pruning: repeated per-scan prune requests collapse,
    but the final state always reflects the last request.
    """

    with _pending_lock:
        if key in _pending_keys:
            _pending_keys[key] = True
            return
        _pending_keys[key] = False

    def run_and_clear() -> None:
        # try/finally: if job() raises, the key MUST still be popped —
        # otherwise the flag stays set forever, every later submit_keyed
        # takes the "already pending" branch, and the job never runs again
        # (retention pruning silently stops; scan dirs grow unbounded).
        try:
            job()
        finally:
            with _pending_lock:
                rerun = _pending_keys.pop(key, False)
            if rerun:
                submit_keyed(key, job)

    submit(run_and_clear)


def flush(timeout: float | None = 10.0) -> bool:
    """Wait until every queued job has run — including keyed jobs that
    re-queue themselves. Returns False on timeout."""

    # Read under the lock: a lockless check can report "no worker" while a
    # first submit() is concurrently creating one with a job already queued.
    with _worker_lock:
        worker = _worker
    if worker is None:
        return True
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        done = threading.Event()
        submit(done.set)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not done.wait(remaining):
            return False
        with _pending_lock:
            drained = not _pending_keys and _queue.empty()
        if drained:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
