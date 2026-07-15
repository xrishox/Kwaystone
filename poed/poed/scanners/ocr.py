"""Shared OCR service for scanner extensions."""

from __future__ import annotations

import numpy as np

from poed import ocr_worker

OcrLine = ocr_worker.OcrLine
OcrUnavailable = ocr_worker.OcrUnavailable


def read_lines(crop: np.ndarray) -> list[OcrLine]:
    return ocr_worker.read_lines(crop)


def warm() -> bool:
    return ocr_worker.warm()


def stop() -> None:
    ocr_worker.stop()
