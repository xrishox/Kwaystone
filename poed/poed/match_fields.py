"""The one place that copies price-row fields into scan match dicts.

Every scanner emits match dicts carrying the same market fields from a
brain price row (or a template built from one).  Before this module each
scanner hand-copied the ~15 fields, and they drifted: the runeshape
scanner was missing quoteLiquidity/quoteMaxStock.  Add new row fields
here and every scanner picks them up.
"""

from __future__ import annotations


def match_row_fields(row: dict | None, *, kind_default: str = "tagged") -> dict:
    """Market fields shared by all scan matches, copied from ``row``.

    Callers add their scanner-specific keys (name, price/stack math,
    geometry, score, ocr text) and may override any default afterwards.
    """

    row = row or {}
    return {
        "kind": row.get("kind", kind_default),
        "priceAvailable": row.get("priceAvailable", True),
        "priceCurrency": row.get("priceCurrency", "exalted"),
        "quantity": row.get("quantity", 0),
        "trend": row.get("trend"),
        "quoteAmount": row.get("quoteAmount"),
        "quoteCurrency": row.get("quoteCurrency"),
        "quoteCurrencyText": row.get("quoteCurrencyText"),
        "quoteLiquidity": row.get("quoteLiquidity"),
        "quoteMaxStock": row.get("quoteMaxStock"),
        "exaltedPerChaos": row.get("exaltedPerChaos"),
        "exaltedPerDivine": row.get("exaltedPerDivine"),
        "sourceTag": row.get("sourceTag"),
        "sourceCategory": row.get("sourceCategory"),
        "priceConfidence": row.get("priceConfidence", "ok"),
    }
