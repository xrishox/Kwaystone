import os

import pytest

from poed import ocr_paddle


def test_resolve_device_auto_prefers_cuda_when_available():
    assert ocr_paddle._resolve_device("auto", True, 16 * 1024**3) == "gpu:0"


def test_resolve_device_auto_uses_cpu_with_small_cuda_memory():
    assert ocr_paddle._resolve_device("auto", True, 12 * 1024**3) == "cpu"


def test_resolve_device_auto_uses_cpu_without_cuda():
    assert ocr_paddle._resolve_device("auto", False) == "cpu"


def test_resolve_device_accepts_cuda_aliases():
    assert ocr_paddle._resolve_device("cuda", True) == "gpu:0"
    assert ocr_paddle._resolve_device("cuda:1", True) == "gpu:1"
    assert ocr_paddle._resolve_device("gpu", True) == "gpu:0"
    assert ocr_paddle._resolve_device("gpu:2", True) == "gpu:2"


def test_resolve_device_rejects_forced_cuda_without_cuda_build():
    try:
        ocr_paddle._resolve_device("cuda", False)
    except RuntimeError as e:
        assert "not CUDA-enabled" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_device_rejects_unknown_device():
    try:
        ocr_paddle._resolve_device("vulkan", True)
    except RuntimeError as e:
        assert "WAYSTONE_PADDLE_DEVICE" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_model_name_falls_back_to_supported_default():
    assert ocr_paddle._model_name("small", "det", "tiny") == "PP-OCRv6_small_det"
    assert ocr_paddle._model_name("unknown", "rec", "medium") == "PP-OCRv6_medium_rec"


def test_configured_model_names_default_to_small_small_on_cpu(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", raising=False)

    assert ocr_paddle._configured_model_names("cpu") == (
        "PP-OCRv6_small_det",
        "PP-OCRv6_small_rec",
        "PP-OCRv6_small_rec",
    )


def test_configured_model_names_default_to_medium_quantity_on_large_cuda(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", raising=False)
    monkeypatch.setattr(ocr_paddle, "_CUDA_MEMORY_BYTES", 16 * 1024**3)

    assert ocr_paddle._configured_model_names("gpu:0") == (
        "PP-OCRv6_small_det",
        "PP-OCRv6_small_rec",
        "PP-OCRv6_medium_rec",
    )


def test_configured_model_names_allow_quantity_recognition_override(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", raising=False)
    monkeypatch.setenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", "small")

    assert ocr_paddle._configured_model_names("gpu:0") == (
        "PP-OCRv6_small_det",
        "PP-OCRv6_small_rec",
        "PP-OCRv6_small_rec",
    )


def test_configured_model_names_quantity_override_wins(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", raising=False)
    monkeypatch.setenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", "small")
    monkeypatch.setenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", "medium")

    assert ocr_paddle._configured_model_names("cpu") == (
        "PP-OCRv6_small_det",
        "PP-OCRv6_small_rec",
        "PP-OCRv6_medium_rec",
    )


def test_configured_model_names_general_recognition_wins_when_quantity_auto(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_DETECTION_MODEL_SIZE", raising=False)
    monkeypatch.setenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", "medium")
    monkeypatch.setenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", "auto")

    assert ocr_paddle._configured_model_names("cpu") == (
        "PP-OCRv6_small_det",
        "PP-OCRv6_medium_rec",
        "PP-OCRv6_medium_rec",
    )


def test_bundled_model_dir_requires_complete_model(tmp_path, monkeypatch):
    model = tmp_path / "PP-OCRv6_small_det"
    model.mkdir()
    (model / "inference.json").touch()
    monkeypatch.setenv("WAYSTONE_OCR_MODEL_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="inference.pdiparams"):
        ocr_paddle._bundled_model_dir("PP-OCRv6_small_det")


def test_bundled_model_dir_returns_verified_directory(tmp_path, monkeypatch):
    model = tmp_path / "PP-OCRv6_small_det"
    model.mkdir()
    for name in ocr_paddle._MODEL_FILES:
        (model / name).touch()
    monkeypatch.setenv("WAYSTONE_OCR_MODEL_ROOT", str(tmp_path))

    assert ocr_paddle._bundled_model_dir("PP-OCRv6_small_det") == os.path.abspath(model)


def test_rec_paths_batches_all_inputs(monkeypatch):
    class Result:
        json = {"res": {"rec_text": "12", "rec_score": 0.9}}

    class Recognizer:
        def __init__(self):
            self.calls = []

        def predict(self, paths, batch_size):
            self.calls.append((paths, batch_size))
            return [Result(), Result()]

    monkeypatch.setenv("WAYSTONE_PADDLE_RECOGNITION_BATCH_SIZE", "16")
    recognizer = Recognizer()

    assert ocr_paddle._rec_paths(recognizer, ["a.png", "b.png"]) == [
        {"text": "12", "score": 0.9},
        {"text": "12", "score": 0.9},
    ]
    assert recognizer.calls == [(["a.png", "b.png"], 16)]


def test_rec_paths_defaults_to_batched_recognition_on_cpu(monkeypatch):
    class Recognizer:
        def __init__(self):
            self.batch_size = None

        def predict(self, paths, batch_size):
            self.batch_size = batch_size
            return []

    monkeypatch.delenv("WAYSTONE_PADDLE_RECOGNITION_BATCH_SIZE", raising=False)
    monkeypatch.setattr(ocr_paddle, "_DEVICE", "cpu")
    recognizer = Recognizer()

    ocr_paddle._rec_paths(recognizer, ["a.png"])

    assert recognizer.batch_size == 8


def test_recognizer_is_reused_from_ocr_pipeline():
    recognizer = object()

    class Pipeline:
        text_rec_model = recognizer

    class OCR:
        paddlex_pipeline = Pipeline()

    assert ocr_paddle._recognizer_from_ocr(OCR()) is recognizer
