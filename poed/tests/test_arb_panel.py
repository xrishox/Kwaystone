"""ArbPanel layout and anchor behavior (requires a display)."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="needs a display (xvfb)"
)

from poed.arb_panel import ArbPanel  # noqa: E402


def _answer():
    return {
        "mode": "commodity",
        "league": "Standard",
        "itemName": "Divine Orb",
        "verdict": {
            "kind": "opportunity",
            "text": "buy with chaos — 5.2% cheaper than exalted",
            "buyWith": "chaos",
            "savingsPct": 5.2,
        },
        "liquidPair": {
            "currency": "chaos",
            "price": 5926.0,
            "priceExalted": 237.0,
            "liquidity": 90,
            "stock": 400,
        },
        "perCurrency": [
            {"currency": "exalted", "amount": 250.0, "exaltedPrice": 250.0, "direct": True},
            {"currency": "chaos", "amount": 5925.0, "exaltedPrice": 237.0, "direct": True},
            {"currency": "divine", "amount": 1.0, "exaltedPrice": 237.0, "direct": False},
        ],
        "exaltedPrices": {"exalted": 1, "chaos": 0.04, "divine": 237},
        "itemRows": [],
        "matrix": [
            {"key": "pair:exalted", "label": "Exalted Orb", "priceText": "1 ex", "source": "aggregate"},
            {"key": "pair:chaos", "label": "Chaos Orb", "priceText": "0.04 ex", "source": "aggregate"},
            {"key": "pair:divine", "label": "Divine Orb", "priceText": "237 ex", "source": "aggregate"},
            {"key": "cur:vaal", "label": "Vaal Orb", "priceText": "21.6 ex", "source": "aggregate"},
        ],
    }


def _panel():
    return ArbPanel(None, positions=None, desktop=None)


def _all_labels(widget, out=None):
    if out is None:
        out = []
    try:
        text = widget.get_label()
    except AttributeError:
        text = None
    if text:
        out.append(text)
    child = widget.get_first_child()
    while child is not None:
        _all_labels(child, out)
        child = child.get_next_sibling()
    return out


def test_renders_verdict_liquid_per_currency_and_matrix():
    panel = _panel()
    panel.show_answer(_answer(), "Standard")

    texts = _all_labels(panel._win)
    joined = "\n".join(texts)
    assert "buy with chaos" in joined
    assert "best market" in joined
    assert "price across currencies" in joined
    assert "exchange rates — click to anchor" in joined
    assert "Vaal Orb" in joined
    assert panel.anchor == "exalted"
    panel.hide()


def test_anchor_click_rebases_conversions():
    panel = _panel()
    answer = _answer()
    panel.show_answer(answer, "Standard")

    panel.set_anchor("chaos")
    assert panel.anchor == "chaos"

    texts = "\n".join(_all_labels(panel._win))
    # Chaos-anchored conversion of the 250-ex exalted row: 250/0.04 = 6250.
    assert "6,250 chaos" in texts

    # Clicking the same currency again releases back to exalted.
    panel.set_anchor("chaos")
    assert panel.anchor == "exalted"
    texts = "\n".join(_all_labels(panel._win))
    assert "≈ 250 exalted" in texts or "250.0 ex" in texts or "≈ 250 ex" in texts
    panel.hide()


def test_matrix_rows_are_clickable_and_select_anchor():
    panel = _panel()
    panel.show_answer(_answer(), "Standard")

    # Find the Divine Orb matrix button and click it.
    def find_buttons(widget, out=None):
        if out is None:
            out = []
        if isinstance(widget, __import__("gi").repository.Gtk.Button):
            out.append(widget)
        child = widget.get_first_child()
        while child is not None:
            find_buttons(child, out)
            child = child.get_next_sibling()
        return out

    buttons = find_buttons(panel._win)
    assert len(buttons) == 4  # one per matrix row
    buttons[2].emit("clicked")  # pair:divine
    assert panel.anchor == "divine"
    panel.hide()


def test_update_state_merges_refined_rows():
    panel = _panel()
    panel.show_answer(_answer(), "Standard")

    state = {
        "refreshId": 1,
        "done": True,
        "matrix": [
            {"key": "pair:divine", "label": "Divine Orb", "priceText": "250 ex", "source": "live"},
        ],
        "itemRows": [],
    }
    panel.update_state(state, "Standard")

    texts = "\n".join(_all_labels(panel._win))
    assert "250 ex" in texts
    # Stage-1 fields survive the merge.
    assert "buy with chaos" in texts
    panel.hide()
