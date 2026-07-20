"""Scoring-variant wrappers around the shipped S2 pipeline.

Each variant flips experimental flags in poed.ritual_scan.identify for the
duration of one analyze() call; defaults always reproduce shipped behavior.
"""

from __future__ import annotations

import numpy as np

from poed.ritual_scan import identify as idm

from .s2_chrome import ChromeSystem
from .stages_output import RitualScanOutput


class _Variant:
    id = "s2?"
    settings: dict = {}

    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        saved = {key: getattr(idm, key) for key in self.settings}
        for key, value in self.settings.items():
            setattr(idm, key, value)
        try:
            return ChromeSystem().analyze(frame, rows)
        finally:
            for key, value in saved.items():
                setattr(idm, key, value)


class BlendSystem(_Variant):
    id = "s2b"
    settings = {"SCORING_MODE": "blend"}


class OrientSystem(_Variant):
    id = "s2o"
    settings = {"ORIENTATION_GATE": True}


class BlendOrientSystem(_Variant):
    id = "s2bo"
    settings = {"SCORING_MODE": "blend", "ORIENTATION_GATE": True}


class SharpSystem(_Variant):
    id = "s2s"
    settings = {"SHARPEN_SMALL_PITCH": True}


class BlendSharpSystem(_Variant):
    id = "s2bs"
    settings = {"SCORING_MODE": "blend", "SHARPEN_SMALL_PITCH": True}


class OrientSharpSystem(_Variant):
    id = "s2os"
    settings = {"ORIENTATION_GATE": True, "SHARPEN_SMALL_PITCH": True}


class BlendOrientSharpSystem(_Variant):
    id = "s2bos"
    settings = {
        "SCORING_MODE": "blend",
        "ORIENTATION_GATE": True,
        "SHARPEN_SMALL_PITCH": True,
    }
