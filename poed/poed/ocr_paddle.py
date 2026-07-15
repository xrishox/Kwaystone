"""PaddleOCR helper process.

The OCR worker remains a subprocess so its large native runtime and models can
be started lazily, but it uses the same Python 3.13 environment as the GTK app.
Release builds point it at bundled inference models and never download models
at runtime.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from typing import Any

import numpy as np

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

_DEVICE: str | None = None
_MODEL_SIZES = {"tiny", "small", "medium"}
_MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml")
_LARGE_VRAM_BYTES = 16 * 1024 * 1024 * 1024
_CUDA_MEMORY_BYTES: int | None = None


def _resolve_device(
    requested: str | None,
    cuda_compiled: bool,
    cuda_memory_bytes: int | None = None,
) -> str:
    requested = (requested or "auto").strip().lower()
    if requested in {"", "auto"}:
        return (
            "gpu:0"
            if cuda_compiled and (cuda_memory_bytes or 0) >= _LARGE_VRAM_BYTES
            else "cpu"
        )
    if requested == "cpu":
        return "cpu"
    if requested in {"cuda", "gpu"}:
        requested = "gpu:0"
    elif requested.startswith("cuda:"):
        requested = "gpu:" + requested.split(":", 1)[1]

    if not re.fullmatch(r"gpu(?::\d+)?", requested):
        raise RuntimeError(
            "WAYSTONE_PADDLE_DEVICE must be auto, cpu, cuda, cuda:N, gpu, or gpu:N"
        )
    if not cuda_compiled:
        raise RuntimeError(
            f"WAYSTONE_PADDLE_DEVICE={requested} requested, but this Paddle "
            "install is not CUDA-enabled"
        )
    return requested


def _cuda_memory_from_paddle() -> int | None:
    try:
        import paddle

        props = paddle.device.cuda.get_device_properties(0)
    except Exception:
        return None
    for name in ("total_memory", "total_global_mem", "total_mem"):
        value = getattr(props, name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    if isinstance(props, dict):
        for name in ("total_memory", "total_global_mem", "total_mem"):
            value = props.get(name)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _cuda_memory_from_nvidia_smi() -> int | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
                "-i",
                "0",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = result.stdout.strip().splitlines()[0:1]
    if not first:
        return None
    try:
        mib = int(first[0].strip().split()[0])
    except (IndexError, TypeError, ValueError):
        return None
    return mib * 1024 * 1024


def _cuda_memory_bytes() -> int | None:
    global _CUDA_MEMORY_BYTES
    if _CUDA_MEMORY_BYTES is None:
        _CUDA_MEMORY_BYTES = (
            _cuda_memory_from_paddle()
            or _cuda_memory_from_nvidia_smi()
            or 0
        )
    return _CUDA_MEMORY_BYTES or None


def _configure_device() -> str:
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE

    import paddle

    try:
        cuda_compiled = bool(paddle.device.is_compiled_with_cuda())
    except Exception:
        cuda_compiled = False

    requested = os.environ.get("WAYSTONE_PADDLE_DEVICE", "auto")
    memory_bytes = _cuda_memory_bytes() if cuda_compiled else None
    device = _resolve_device(requested, cuda_compiled, memory_bytes)
    try:
        paddle.set_device(device)
    except Exception:
        if (requested or "auto").strip().lower() not in {"", "auto"}:
            raise
        device = "cpu"
        paddle.set_device(device)
    _DEVICE = device
    return device


def _box_bounds(box: Any) -> tuple[float, float, float, float] | None:
    arr = np.asarray(box, dtype=np.float32)
    if arr.size == 4 and arr.ndim == 1:
        x0, y0, x1, y1 = arr.tolist()
        if x1 < x0 or y1 < y0:
            return x0, y0, x0 + x1, y0 + y1
        return x0, y0, x1, y1
    if arr.ndim >= 2 and arr.shape[-1] >= 2:
        xs = arr[..., 0]
        ys = arr[..., 1]
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())
    return None


def _dict_lines(page: Any) -> list[dict]:
    if hasattr(page, "json"):
        page = page.json() if callable(page.json) else page.json
    if not isinstance(page, dict):
        return []
    if isinstance(page.get("res"), dict):
        page = page["res"]
    texts = page.get("rec_texts") or page.get("texts") or []
    scores = page.get("rec_scores") or page.get("scores") or [0.0] * len(texts)
    boxes = (
        page.get("rec_boxes")
        or page.get("rec_polys")
        or page.get("dt_polys")
        or page.get("boxes")
        or []
    )
    out = []
    for text, score, box in zip(texts, scores, boxes):
        bounds = _box_bounds(box)
        text = str(text).strip()
        if text and bounds is not None:
            out.append({"text": text, "score": float(score or 0), "box": bounds})
    return out


def _model_name(size: str, kind: str, default: str) -> str:
    normalized = size.strip().lower()
    if normalized not in _MODEL_SIZES:
        normalized = default
    return f"PP-OCRv6_{normalized}_{kind}"


def _model_size_setting(value: str | None, default: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized in {"", "auto"}:
        return default
    return normalized if normalized in _MODEL_SIZES else default


def _auto_quantity_model_size(device: str) -> str:
    if device.startswith("gpu") and (_cuda_memory_bytes() or 0) >= _LARGE_VRAM_BYTES:
        return "medium"
    return "small"


def _configured_model_names(device: str = "cpu") -> tuple[str, str, str]:
    legacy_size = os.environ.get("WAYSTONE_PADDLE_MODEL_SIZE")
    det_size = _model_size_setting(
        os.environ.get("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", legacy_size),
        "small",
    )
    general_setting = os.environ.get("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", legacy_size)
    general_size = _model_size_setting(general_setting, "small")
    quantity_setting = os.environ.get("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", "auto")
    quantity_normalized = (quantity_setting or "auto").strip().lower()
    general_normalized = (general_setting or "auto").strip().lower()
    if quantity_normalized in _MODEL_SIZES:
        quantity_size = quantity_normalized
    elif general_normalized in _MODEL_SIZES:
        quantity_size = general_size
    else:
        quantity_size = _auto_quantity_model_size(device)
    return (
        _model_name(det_size, "det", "small"),
        _model_name(general_size, "rec", "small"),
        _model_name(quantity_size, "rec", general_size),
    )


def _bundled_model_dir(model_name: str) -> str | None:
    root = os.environ.get("WAYSTONE_OCR_MODEL_ROOT")
    if not root:
        return None
    model_dir = os.path.abspath(os.path.join(root, model_name))
    missing = [name for name in _MODEL_FILES if not os.path.isfile(os.path.join(model_dir, name))]
    if missing:
        raise RuntimeError(
            f"bundled OCR model {model_name} is incomplete; missing {', '.join(missing)}"
        )
    return model_dir


def _make_ocr():
    from paddleocr import PaddleOCR

    device = _configure_device()
    det_name, rec_name, _quantity_rec_name = _configured_model_names(device)
    options = dict(
        device=device,
        text_detection_model_name=det_name,
        text_recognition_model_name=rec_name,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    det_dir = _bundled_model_dir(det_name)
    rec_dir = _bundled_model_dir(rec_name)
    if det_dir is not None:
        options["text_detection_model_dir"] = det_dir
        options["text_recognition_model_dir"] = rec_dir
    return PaddleOCR(**options)


def _make_text_recognition(model_name: str):
    from paddleocr import TextRecognition

    options = dict(device=_configure_device(), model_name=model_name)
    model_dir = _bundled_model_dir(model_name)
    if model_dir is not None:
        options["model_dir"] = model_dir
    return TextRecognition(**options)


def _recognizer_from_ocr(ocr):
    """Reuse the OCR pipeline's recognizer when the requested size matches it."""
    pipeline = getattr(ocr, "paddlex_pipeline", None)
    recognizer = getattr(pipeline, "text_rec_model", None)
    if recognizer is None:
        raise RuntimeError("PaddleOCR did not expose its text recognizer")
    return recognizer


def _ocr_path(ocr, path: str) -> list[dict]:
    result = ocr.predict(path)
    return [line for page in result for line in _dict_lines(page)]


def _dict_rec(result: Any) -> dict:
    if hasattr(result, "json"):
        result = result.json() if callable(result.json) else result.json
    if not isinstance(result, dict):
        return {"text": "", "score": 0.0}
    if isinstance(result.get("res"), dict):
        result = result["res"]
    return {
        "text": str(result.get("rec_text") or result.get("text") or "").strip(),
        "score": float(result.get("rec_score") or result.get("score") or 0),
    }


def _rec_paths(rec, paths: list[str]) -> list[dict]:
    if not paths:
        return []
    default_batch = "32" if (_DEVICE or "").startswith("gpu") else "8"
    batch_size = max(1, int(
        os.environ.get("WAYSTONE_PADDLE_RECOGNITION_BATCH_SIZE", default_batch)
    ))
    results = rec.predict(paths, batch_size=batch_size)
    return [_dict_rec(result) for result in results]


def _write(obj: dict | list) -> None:
    print(json.dumps(obj), flush=True)


def serve() -> int:
    device = _configure_device()
    _det_name, general_rec_name, quantity_rec_name = _configured_model_names(device)
    ocr = _make_ocr()
    general_rec = _recognizer_from_ocr(ocr)
    quantity_rec = (
        general_rec
        if quantity_rec_name == general_rec_name
        else _make_text_recognition(quantity_rec_name)
    )
    _write({
        "type": "ready",
        "device": device,
        "recognition_model": general_rec_name,
        "quantity_model": quantity_rec_name,
    })
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if "rec_paths" in req:
                reads = _rec_paths(
                    quantity_rec,
                    [str(path) for path in req.get("rec_paths") or []],
                )
                _write({"id": req.get("id"), "ok": True, "rec": reads})
            else:
                lines = _ocr_path(ocr, str(req.get("path", "")))
                _write({"id": req.get("id"), "ok": True, "lines": lines})
        except Exception as e:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            _write({"id": req.get("id") if isinstance(req, dict) else None,
                    "ok": False, "error": str(e)})
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--server":
        return serve()
    if len(argv) != 2:
        print("usage: python -m poed.ocr_paddle IMAGE|--server", file=sys.stderr)
        return 2

    lines = _ocr_path(_make_ocr(), argv[1])
    _write(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
