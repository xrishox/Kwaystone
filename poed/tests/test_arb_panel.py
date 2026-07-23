"""Arbitrage panel rendering tests (requires GTK4 layer-shell and a display)."""

import os

import pytest

if not os.environ.get("DISPLAY"):
    pytest.skip("needs a display (xvfb)", allow_module_level=True)

try:
    from poed.arb_panel import ArbPanel, Gtk, _market_ratio
except (ImportError, ValueError) as error:
    pytest.skip(f"arbitrage panel unavailable: {error}", allow_module_level=True)


def _item(api_id, name):
    return {"apiId": api_id, "name": name, "category": "currency"}


def _observation():
    return {
        "id": "omen->chaos",
        "want": _item("chaos", "Chaos Orb"),
        "have": _item("omen", "Omen of Whittling"),
        "wantAmount": 81,
        "haveAmount": 1,
        "rate": 81,
        "observedAt": 1,
    }


def _outcomes(
    *, nominal_final=1, execution_final=0, buffered_final=0, actionable=False, steps=None
):
    if steps is None:
        steps = [
            {
                "nominalInputUnits": 1,
                "nominalOutputUnits": 81,
                "executionInputUnits": 1,
                "executionOutputUnits": 76,
                "bufferedInputUnits": 1,
                "bufferedOutputUnits": 75,
                "boundaryHeadroomPercent": 0,
            },
            {
                "nominalInputUnits": 81,
                "nominalOutputUnits": 5,
                "executionInputUnits": 76,
                "executionOutputUnits": 4,
                "bufferedInputUnits": 75,
                "bufferedOutputUnits": 4,
                "boundaryHeadroomPercent": 7.4,
            },
            {
                "nominalInputUnits": 5,
                "nominalOutputUnits": nominal_final,
                "executionInputUnits": 4,
                "executionOutputUnits": execution_final,
                "bufferedInputUnits": 4,
                "bufferedOutputUnits": buffered_final,
                "boundaryHeadroomPercent": 0,
            },
        ]
    return [
        {
            "quantity": quantity,
            "nominalFinalUnits": nominal_final if quantity == 1 else quantity,
            "executionFinalUnits": execution_final if quantity == 1 else quantity - 1,
            "bufferedFinalUnits": buffered_final if quantity == 1 else quantity - 1,
            "nominalComplete": True,
            "executionComplete": quantity != 1,
            "bufferedComplete": quantity != 1,
            **(
                {
                    "executionBlockedStep": 2,
                    "executionBlockedUnits": 4,
                    "bufferedBlockedStep": 2,
                    "bufferedBlockedUnits": 4,
                }
                if quantity == 1
                else {}
            ),
            "nominalReturnPercent": 0,
            "executionReturnPercent": None if quantity == 1 else -100 / quantity,
            "bufferedReturnPercent": None if quantity == 1 else -100 / quantity,
            "steps": steps,
            "localScore": 0.5,
            "localPeak": quantity == 5,
            "budgetBest": quantity in {5, 10, 25, 50, 100},
            "actionable": actionable,
        }
        for quantity in range(1, 101)
    ]


def _analysis():
    target = _item("omen", "Omen of Whittling")
    chaos = _item("chaos", "Chaos Orb")
    exalted = _item("exalted", "Exalted Orb")
    loop = {
        "id": "sell|buy",
        "path": [target, chaos, exalted, target],
        "percent": 8.0,
        "multiplier": 1.08,
        "nominalPercent": 8.0,
        "executionPercent": -7.4,
        "bufferedPercent": -12.0,
        "status": "verified",
        "stale": False,
        "actionable": True,
        "legs": [
            {
                "from": target,
                "to": chaos,
                "rate": 81,
                "executionRate": 76.95,
                "source": "capture",
                "observedAt": 1,
            },
            {
                "from": chaos,
                "to": exalted,
                "rate": 1 / 15,
                "executionRate": 0.95 / 15,
                "source": "capture-bridge",
                "observedAt": 1,
            },
            {
                "from": exalted,
                "to": target,
                "rate": 0.2,
                "executionRate": 0.19,
                "source": "capture",
                "observedAt": 1,
            },
        ],
        "quantityOutcomes": _outcomes(),
    }
    other = {
        **loop,
        "id": "other",
        "path": [target, exalted, chaos, target],
        "percent": 3.0,
        "multiplier": 1.03,
        "nominalPercent": 3.0,
        "executionPercent": -11.7,
        "bufferedPercent": -16.1,
        "status": "estimate",
        "estimateConfidence": "reliable",
        "actionable": False,
        "legs": [
            {
                "from": target,
                "to": exalted,
                "rate": 5,
                "executionRate": 4.75,
                "source": "capture",
                "observedAt": 1,
            },
            {
                "from": exalted,
                "to": chaos,
                "rate": 20,
                "executionRate": 19,
                "source": "poe2scout",
            },
            {
                "from": chaos,
                "to": target,
                "rate": 0.0103,
                "executionRate": 0.009785,
                "source": "capture",
                "observedAt": 1,
            },
        ],
        "quantityOutcomes": _outcomes(),
    }
    capture = {**_observation(), "role": "sell", "quote": chaos, "stale": False}
    bridge = {
        "id": "chaos->exalted",
        "want": exalted,
        "have": chaos,
        "wantAmount": 1,
        "haveAmount": 15,
        "rate": 1 / 15,
        "observedAt": 1,
        "stale": False,
    }
    return {
        "target": target,
        "captures": [capture],
        "bridges": [bridge],
        "loops": [loop, other],
        "bestVerifiedLoop": loop,
        "bestCandidateLoop": other,
        "loopsEvaluated": 2,
        "capturedCurrencyCount": 2,
        "unavailable": [],
        "verificationNeeded": [],
        "ratesEpoch": "2026-07-21T20:00:00Z",
        "ratesAgeMs": 60_000,
        "ratesStatus": "fresh",
        "safetyBufferBps": 500,
        "perLegSafetyBufferBps": 169.52,
        "executionConcessionBps": 500,
        "executionConcessionLoopPercent": 14.2625,
        "analyzedAt": 0,
    }


def _labels(widget, out=None):
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
        _labels(child, out)
        child = child.get_next_sibling()
    return out


def _buttons(widget, out=None):
    if out is None:
        out = []
    if isinstance(widget, Gtk.Button):
        out.append(widget)
    child = widget.get_first_child()
    while child is not None:
        _buttons(child, out)
        child = child.get_next_sibling()
    return out


def test_market_ratio_matches_game_normalization():
    assert _market_ratio(0.2439) == "4.10 : 1"
    assert _market_ratio(4.1) == "4.10 : 1"
    assert _market_ratio(1 / 15) == "15 : 1"


def test_target_chooser_lists_both_exchange_sides():
    panel = ArbPanel(None)
    selected = []
    panel.show_choice({"observation": _observation()}, selected.append, "right")
    text = "\n".join(_labels(panel._win))
    assert "Choose arbitrage target" in text
    assert "Chaos Orb" in text
    assert "Omen of Whittling" in text
    assert "81 : 1" in text
    panel.hide()


def test_target_chooser_offers_latest_session_restore():
    panel = ArbPanel(None)
    restored = []
    panel.show_choice(
        {"observation": _observation()},
        lambda _api_id: None,
        "right",
        on_restore=lambda: restored.append(True),
        restore_target_name="Kulemak's Invitation",
    )
    text = "\n".join(_labels(panel._win))
    assert "RESTORE LATEST ARB" in text
    assert "Kulemak's Invitation" in text
    restore = next(
        button
        for button in _buttons(panel._win)
        if "RESTORE LATEST ARB" in "\n".join(_labels(button))
    )
    restore.emit("clicked")
    assert restored == [True]
    panel.hide()


def test_analysis_renders_percentage_repeat_path_and_sources():
    recalculations = []
    panel = ArbPanel(
        None,
        on_recalculate=lambda: recalculations.append(True),
        show_losing_candidates=True,
    )
    panel.show_analysis(_analysis(), "right")
    text = "\n".join(_labels(panel._win))
    assert "Buffered faster fill: cannot complete · 4 Exalted Orb retained" in text
    assert "Fractional model  -12.0% buffered · -7.4% faster fill · +8.0% market" in text
    assert "1 → incomplete · -16.1% fractional est." in text
    assert "2 loops evaluated · 2 currencies" in text
    assert "BEST BUFFERED LOOP AT 1" in text
    assert "Omen of Whittling → Chaos Orb → Exalted Orb → Omen of Whittling ↻" in text
    assert "WHOLE-UNIT OUTCOME" in text
    assert "Market: 1 Omen of Whittling → 1 Omen of Whittling (+0.0%)" in text
    assert "Faster fill: cannot complete · 4 Exalted Orb retained" in text
    assert "5.0% fewer output units accepted at every market" in text
    assert "5.0% total adverse loop move · 1.7% modeled per leg" in text
    assert "81 Chaos Orb → 5 Exalted Orb market · 4 faster fill · 4 buffered" in text
    assert "1 Omen of Whittling = 81 Chaos Orb (81 : 1)" in text
    assert "Faster fill  1 Omen of Whittling = 76.95 Chaos Orb" in text
    assert "1 Chaos Orb = 0.0667 Exalted Orb (15 : 1)" in text
    assert "1 Exalted Orb = 0.2 Omen of Whittling (5 : 1)" in text
    assert "captured ratio" in text
    assert "live currency ratio" in text
    assert "CANDIDATES TO VERIFY" in text
    assert "LIVE CURRENCY PRICES" in text
    assert "Chaos Orb → Exalted Orb" in text
    assert "1 Chaos Orb = 0.0667 Exalted Orb (15 : 1)" in text
    assert panel._recalculate.get_sensitive()
    panel._recalculate.emit("clicked")
    assert recalculations == [True]
    panel.hide()


def test_clicking_another_loop_promotes_it_and_keeps_the_slider():
    panel = ArbPanel(None, show_losing_candidates=True)
    panel.show_analysis(_analysis(), "right")
    candidate = next(
        button
        for button in _buttons(panel._win)
        if "Omen of Whittling → Exalted Orb → Chaos Orb" in "\n".join(_labels(button))
    )

    candidate.emit("clicked")
    text = "\n".join(_labels(panel._win))

    assert "BUFFERED ESTIMATE · NOT VERIFIED PROFIT" in text
    assert "WHOLE-UNIT OUTCOME" in text
    assert "Do not treat it as executable profit" in text
    assert "Verify Exalted Orb → Chaos Orb" in text
    assert "press Alt+A" in text
    assert "press Alt+D" not in text
    panel.hide()


def test_automatic_loop_selection_uses_fractional_signal_then_tracks_quantity():
    data = _analysis()
    first, second = data["loops"]
    second["status"] = "verified"
    second.pop("estimateConfidence", None)
    first["bufferedPercent"] = 30.0
    second["bufferedPercent"] = 10.0
    first["quantityOutcomes"][1].update(
        {"bufferedComplete": True, "bufferedReturnPercent": -50.0}
    )
    second["quantityOutcomes"][1].update(
        {"bufferedComplete": True, "bufferedReturnPercent": 20.0}
    )

    panel = ArbPanel(None, show_losing_candidates=True)
    panel.show_analysis(data, "right")
    assert panel.selected_loop()["id"] == first["id"]

    panel._quantity = 2
    panel.show_analysis(data, "right")
    assert panel.selected_loop()["id"] == second["id"]

    panel._select_loop(first["id"])
    panel._quantity = 3
    panel.show_analysis(data, "right")
    assert panel.selected_loop()["id"] == first["id"]
    panel.hide()


def test_thin_history_is_visibly_separated_and_never_called_a_candidate():
    data = _analysis()
    thin = data["loops"][1]
    thin["estimateConfidence"] = "thin"
    for leg in thin["legs"]:
        if leg["source"] == "poe2scout":
            leg["scoutEvidence"] = {
                "confidence": "thin",
                "fromVolume": 40,
                "toVolume": 3,
                "liquidityExalted": 900,
            }
    another_thin = {**thin, "id": "another-thin"}
    data["loops"] = [thin, another_thin]
    data["bestCandidateLoop"] = None
    data["bestVerifiedLoop"] = None

    panel = ArbPanel(None, show_losing_candidates=True)
    panel.show_analysis(data, "right")
    text = "\n".join(_labels(panel._win))

    assert "THIN HISTORICAL SIGNAL · VERIFY FIRST" in text
    assert "THIN HISTORICAL SIGNALS" in text
    assert "Poe2Scout thin · 40/3 units · 900 ex" in text
    assert "not a ranked candidate" in text
    assert "CANDIDATES TO VERIFY" not in text
    panel.hide()


def test_losing_candidates_are_hidden_by_default_and_revealed_by_setting():
    changes = []
    panel = ArbPanel(None, on_show_losing=changes.append)
    panel.show_analysis(_analysis(), "right")

    hidden = "\n".join(_labels(panel._win))
    assert "No positive arbitrage candidates" in hidden
    assert "0 positive · 2 evaluated · 2 currencies" in hidden
    assert "Omen of Whittling → Chaos Orb → Exalted Orb" not in hidden

    panel._show_losing_toggle.set_active(True)
    shown = "\n".join(_labels(panel._win))
    assert changes == [True]
    assert "2 loops evaluated · 2 currencies" in shown
    assert "Omen of Whittling → Chaos Orb → Exalted Orb" in shown
    panel.hide()


def test_positive_fractional_candidate_stays_visible_despite_low_quantity_rounding():
    data = _analysis()
    data["loops"][0]["bufferedPercent"] = 1.0

    panel = ArbPanel(None)
    panel.show_analysis(data, "right")
    text = "\n".join(_labels(panel._win))

    assert panel.selected_loop()["id"] == "sell|buy"
    assert "1 positive · 2 evaluated · 2 currencies" in text
    assert "Buffered faster fill: cannot complete" in text
    assert "Omen of Whittling → Exalted Orb → Chaos Orb" not in text
    panel.hide()


def test_active_monitor_loop_stays_visible_when_it_turns_negative():
    data = _analysis()
    data["loops"][0]["bufferedPercent"] = 2.0
    panel = ArbPanel(None)
    panel.show_analysis(data, "right")
    assert panel.selected_loop()["id"] == "sell|buy"

    panel.set_monitor_state("tracking", "")
    data["loops"][0]["bufferedPercent"] = -2.0
    panel.show_analysis(data, "right")
    text = "\n".join(_labels(panel._win))

    assert panel.selected_loop()["id"] == "sell|buy"
    assert "Omen of Whittling → Chaos Orb → Exalted Orb" in text
    assert "Omen of Whittling → Exalted Orb → Chaos Orb" not in text
    panel.hide()


def test_missing_reverse_capture_never_presents_reciprocal_profit():
    data = _analysis()
    target = _item("invitation", "Kulemak's Invitation")
    annul = _item("annul", "Orb of Annulment")
    data.update(
        {
            "target": target,
            "loops": [],
            "bestCandidateLoop": None,
            "bestVerifiedLoop": None,
            "loopsEvaluated": 0,
            "unavailable": [f"{annul['name']} → {target['name']}"],
        }
    )

    panel = ArbPanel(None)
    panel.show_analysis(data, "right")
    text = "\n".join(_labels(panel._win))

    assert "No complete directed loop" in text
    assert "reverse prices are never inferred" in text
    assert "Missing directed market: Orb of Annulment → Kulemak's Invitation" in text
    assert "300.2%" not in text
    panel.hide()
