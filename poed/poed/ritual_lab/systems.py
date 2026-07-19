"""Registry of candidate systems available to the lab CLI."""

from __future__ import annotations

from .stages import RitualSystem


def available_systems() -> dict[str, type]:
    from .s0_baseline import BaselineSystem

    registry: dict[str, type] = {"s0": BaselineSystem}
    try:
        from .s1_lattice import LatticeSystem

        registry["s1"] = LatticeSystem
    except ImportError:
        pass
    try:
        from .s2_chrome import ChromeSystem

        registry["s2"] = ChromeSystem
    except ImportError:
        pass
    try:
        from .s3_items import ItemFirstSystem

        registry["s3"] = ItemFirstSystem
    except ImportError:
        pass
    try:
        from .s4_voting import VotingSystem

        registry["s4"] = VotingSystem
    except ImportError:
        pass
    try:
        from .s5_generative import GenerativeSystem

        registry["s5"] = GenerativeSystem
    except ImportError:
        pass
    return registry


def build_systems(names: list[str]) -> list[RitualSystem]:
    registry = available_systems()
    systems = []
    for name in names:
        if name not in registry:
            known = ", ".join(sorted(registry))
            raise ValueError(f"unknown system {name!r}; available: {known}")
        systems.append(registry[name]())
    return systems
