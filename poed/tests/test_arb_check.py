"""Arbitrage capture-session controller tests without a display."""

import pytest

from poed import arb_check


def _item(api_id, name, category=None):
    if category is None:
        category = "omens" if api_id == "omen" else "currency"
    return {
        "apiId": api_id,
        "name": name,
        "category": category,
        "isCurrency": category == "currency",
    }


def _observation(want="chaos", have="omen", observed_at=1000):
    names = {"chaos": "Chaos Orb", "exalted": "Exalted Orb", "omen": "Omen of Whittling"}
    return {
        "id": f"{have}->{want}",
        "want": _item(want, names[want]),
        "have": _item(have, names[have]),
        "wantAmount": 81,
        "haveAmount": 1,
        "rate": 81,
        "observedAt": observed_at,
    }


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **_options):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class _FakePanel:
    instances = []

    def __init__(self, *args, **kwargs):
        self.visible = False
        self.choice = None
        self.analysis = None
        self.error = None
        self.loading = None
        self.analysis_calls = 0
        self.kwargs = kwargs
        self.monitor_state = ("off", "")
        _FakePanel.instances.append(self)

    def is_visible(self):
        return self.visible

    def hide(self):
        self.visible = False

    def show_choice(self, pair, on_select, side, **kwargs):
        self.choice = (pair, on_select, side, kwargs)
        self.visible = True

    def show_loading(self, name, side=None):
        self.loading = (name, side)
        self.visible = True

    def show_analysis(self, answer, side=None):
        self.analysis = (answer, side)
        self.analysis_calls += 1
        self.visible = True

    def show_error(self, message, side=None, **_kwargs):
        self.error = (message, side)
        self.visible = True

    def set_monitor_state(self, state, detail=""):
        self.monitor_state = (state, detail)

    def selected_loop(self):
        return None

    def selected_quantity(self):
        return 1

    def alert(self):
        pass


class _Brain:
    def __init__(self, pairs):
        self.pairs = list(pairs)
        self.calls = []

    def request(self, message, **_kwargs):
        self.calls.append(message)
        if message["cmd"] == "arbpair":
            return {"observation": self.pairs.pop(0), "ratesFetchedAt": 1}
        if message["cmd"] == "arbanalyze":
            target = next(
                item
                for observation in message["observations"]
                for item in (observation["want"], observation["have"])
                if item["apiId"] == message["targetApiId"]
            )
            return {
                "target": target,
                "captures": [],
                "loops": [],
                "loopsEvaluated": 0,
                "capturedCurrencyCount": 0,
                "unavailable": [],
                "ratesFetchedAt": 1,
                "ratesAgeMs": 0,
            }
        raise AssertionError(message)


class _Desktop:
    pass


@pytest.fixture
def fakes(monkeypatch):
    _FakePanel.instances = []
    monkeypatch.setattr(arb_check, "ArbPanel", _FakePanel)
    monkeypatch.setattr(arb_check.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        arb_check.currency_exchange_scan,
        "capture",
        lambda _desktop: type(
            "Read",
            (),
            {
                "want_text": "Chaos Orb",
                "have_text": "Omen of Whittling",
                "want_amount": 81,
                "have_amount": 1,
                "observed_at": 1000,
                "panel_side": "right",
            },
        )(),
    )
    return _FakePanel


def _controller(brain):
    return arb_check.ArbCheckController(
        application=object(),
        cfg={
            "league": "Test",
            "arb_min_percent": 5.0,
            "arb_safety_buffer_percent": 5.0,
            "arb_execution_concession_percent": 5.0,
            "arb_show_losing_candidates": False,
        },
        brain=brain,
        desktop=_Desktop(),
        on_visibility_changed=lambda: None,
    )


def _flush_idle():
    from gi.repository import GLib

    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def test_alt_s_shows_both_target_choices_then_analyzes(fakes):
    brain = _Brain([_observation()])
    controller = _controller(brain)
    controller.start(1, lambda gen: gen == 1)
    _flush_idle()

    panel = fakes.instances[0]
    assert panel.choice[2] == "right"
    panel.choice[1]("omen")
    _flush_idle()

    assert controller._target["apiId"] == "omen"
    assert controller._observations[0]["id"] == "omen->chaos"
    assert panel.analysis is not None
    assert brain.calls[0]["forceRates"] is True
    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert len(analyze_calls) == 1
    assert [item["id"] for item in analyze_calls[0]["observations"]] == ["omen->chaos"]


def test_alt_s_allows_any_catalog_currency_as_the_quote(fakes):
    pair = {
        "id": "fracturing-orb->divine",
        "want": _item("divine", "Divine Orb"),
        "have": _item("fracturing-orb", "Fracturing Orb"),
        "wantAmount": 3,
        "haveAmount": 2,
        "rate": 1.5,
        "observedAt": 1000,
    }
    brain = _Brain([pair])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()

    panel = fakes.instances[0]
    panel.choice[1]("divine")
    _flush_idle()

    assert controller._target["apiId"] == "divine"
    assert controller._observations[0]["id"] == "fracturing-orb->divine"
    analyze = [call for call in brain.calls if call["cmd"] == "arbanalyze"][-1]
    assert analyze["observations"][0]["have"]["isCurrency"] is True


def test_alt_a_adds_and_replaces_same_direction(fakes):
    first = _observation(observed_at=1000)
    replacement = _observation(observed_at=2000)
    brain = _Brain([first, replacement])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")
    controller.add(2, lambda _gen: True)
    _flush_idle()

    assert len(controller._observations) == 1
    assert controller._observations[0]["observedAt"] == 2000
    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert analyze_calls[-1]["observations"][0]["observedAt"] == 2000
    pair_calls = [call for call in brain.calls if call["cmd"] == "arbpair"]
    assert [call["forceRates"] for call in pair_calls] == [True, False]
    assert pair_calls[0]["knownItems"] == []
    assert {item["apiId"] for item in pair_calls[1]["knownItems"]} == {"omen", "chaos"}


def test_recalculate_reuses_all_captured_pairs(fakes):
    first = _observation(observed_at=1000)
    second = _observation(want="exalted", observed_at=2000)
    brain = _Brain([first, second])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")
    controller.add(2, lambda _gen: True)
    _flush_idle()

    before = len([call for call in brain.calls if call["cmd"] == "arbanalyze"])
    fakes.instances[0].kwargs["on_recalculate"]()
    _flush_idle()

    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert len(analyze_calls) == before + 1
    assert {item["id"] for item in analyze_calls[-1]["observations"]} == {
        "omen->chaos",
        "omen->exalted",
    }


def test_live_update_preserves_opposite_direction_of_same_market(fakes):
    brain = _Brain([])
    controller = _controller(brain)
    controller._target = _item("omen", "Omen of Whittling")
    controller._monitor = object()
    controller._observations = [_observation(want="chaos", have="omen", observed_at=1)]
    reverse = _observation(want="omen", have="chaos", observed_at=2)

    controller._apply_monitor_observation(reverse)
    _flush_idle()

    assert [item["id"] for item in controller._observations] == [
        "omen->chaos",
        "chaos->omen",
    ]
    analyze = [call for call in brain.calls if call["cmd"] == "arbanalyze"][-1]
    assert [item["id"] for item in analyze["observations"]] == [
        "omen->chaos",
        "chaos->omen",
    ]


def test_buffer_change_reuses_rates_and_sends_basis_points(fakes, monkeypatch):
    monkeypatch.setattr(arb_check.config, "save_values", lambda *_args, **_kwargs: None)
    brain = _Brain([_observation()])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")

    fakes.instances[0].kwargs["on_buffer"](7.5)
    _flush_idle()

    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert analyze_calls[-1]["safetyBufferBps"] == 750
    assert analyze_calls[-1]["reuseRates"] is True
    assert controller._cfg["arb_safety_buffer_percent"] == 7.5


def test_execution_concession_change_reranks_notches_with_reused_rates(
    fakes, monkeypatch
):
    saved = []
    monkeypatch.setattr(
        arb_check.config,
        "save_values",
        lambda path, values: saved.append((path, values)),
    )
    brain = _Brain([_observation()])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")

    fakes.instances[0].kwargs["on_concession"](7.4)
    _flush_idle()

    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert analyze_calls[-1]["executionConcessionBps"] == 750
    assert analyze_calls[-1]["reuseRates"] is True
    assert controller._cfg["arb_execution_concession_percent"] == 7.5

    controller._persist_execution_concession()
    assert saved[-1] == (
        None,
        {"arb_execution_concession_percent": 7.5},
    )


def test_show_losing_candidate_setting_persists_without_refetching(
    fakes, monkeypatch
):
    saved = []
    monkeypatch.setattr(
        arb_check.config,
        "save_values",
        lambda path, values: saved.append((path, values)),
    )
    brain = _Brain([_observation()])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")
    analyze_count = len(
        [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    )

    fakes.instances[0].kwargs["on_show_losing"](True)
    _flush_idle()

    assert controller._cfg["arb_show_losing_candidates"] is True
    assert len([call for call in brain.calls if call["cmd"] == "arbanalyze"]) == analyze_count
    controller._persist_show_losing_candidates()
    assert saved[-1] == (None, {"arb_show_losing_candidates": True})


def test_alt_a_updates_an_added_currency_pair_and_recalculates(fakes):
    brain = _Brain(
        [
            _observation(want="chaos", have="omen", observed_at=1000),
            _observation(want="exalted", have="omen", observed_at=2000),
            _observation(want="chaos", have="exalted", observed_at=3000),
            _observation(want="exalted", have="chaos", observed_at=4000),
            _observation(want="chaos", have="exalted", observed_at=5000),
        ]
    )
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")
    controller.add(2, lambda _gen: True)
    _flush_idle()

    controller.add(3, lambda _gen: True)
    _flush_idle()
    assert [item["id"] for item in controller._bridges] == ["exalted->chaos"]

    controller.add(4, lambda _gen: True)
    _flush_idle()
    assert [item["id"] for item in controller._bridges] == [
        "exalted->chaos",
        "chaos->exalted",
    ]
    controller.add(5, lambda _gen: True)
    _flush_idle()
    assert [item["id"] for item in controller._bridges] == [
        "chaos->exalted",
        "exalted->chaos",
    ]
    assert controller._bridges[-1]["observedAt"] == 5000
    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert {item["id"] for item in analyze_calls[-1]["observations"]} == {
        "omen->chaos",
        "omen->exalted",
        "exalted->chaos",
        "chaos->exalted",
    }
    pair_calls = [call for call in brain.calls if call["cmd"] == "arbpair"]
    assert [call["forceRates"] for call in pair_calls] == [True, False, False, False, False]


def test_alt_a_rejects_currency_not_added_against_target(fakes):
    unadded = {
        "id": "exalted->chaos",
        "want": _item("chaos", "Chaos Orb"),
        "have": _item("exalted", "Exalted Orb"),
        "wantAmount": 15,
        "haveAmount": 1,
        "rate": 15,
        "observedAt": 2000,
    }
    brain = _Brain([_observation(), unadded])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")

    controller.add(2, lambda _gen: True)
    _flush_idle()

    assert "must already be part" in panel.error[0]
    assert controller._bridges == []


def test_alt_a_can_refine_a_target_return_leg(fakes):
    reverse_target = {
        "id": "exalted->omen",
        "want": _item("omen", "Omen of Whittling"),
        "have": _item("exalted", "Exalted Orb"),
        "wantAmount": 10,
        "haveAmount": 59,
        "rate": 10 / 59,
        "observedAt": 3000,
    }
    brain = _Brain(
        [
            _observation(want="chaos", have="omen", observed_at=1000),
            _observation(want="exalted", have="omen", observed_at=2000),
            reverse_target,
        ]
    )
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    fakes.instances[0].choice[1]("omen")
    controller.add(2, lambda _gen: True)
    _flush_idle()

    controller.add(3, lambda _gen: True)
    _flush_idle()

    assert [item["id"] for item in controller._observations] == [
        "omen->chaos",
        "omen->exalted",
        "exalted->omen",
    ]
    assert controller._bridges == []
    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert {item["id"] for item in analyze_calls[-1]["observations"]} == {
        "omen->chaos",
        "omen->exalted",
        "exalted->omen",
    }


def test_alt_a_rejects_pair_without_target_and_preserves_session(fakes):
    brain = _Brain([_observation(), _observation(want="chaos", have="exalted")])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")
    controller.add(2, lambda _gen: True)
    _flush_idle()

    assert "must already be part" in panel.error[0]
    assert controller._target["apiId"] == "omen"
    assert len(controller._observations) == 1


def test_alt_a_rejects_uncaptured_bridge_category(fakes):
    unsupported = {
        "id": "omen->splinter",
        "want": _item("splinter", "Breach Splinter", "fragments"),
        "have": _item("omen", "Omen of Whittling"),
        "wantAmount": 10,
        "haveAmount": 1,
        "rate": 10,
        "observedAt": 2000,
    }
    brain = _Brain([_observation(), unsupported])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")

    controller.add(2, lambda _gen: True)
    _flush_idle()

    assert "added side must be a currency" in panel.error[0]
    assert len(controller._observations) == 1


def test_alt_a_without_session_reports_error(fakes):
    controller = _controller(_Brain([]))
    controller.add(1, lambda _gen: True)
    _flush_idle()
    assert fakes.instances[0].error[0] == "No active arbitrage session"


def test_hide_archives_session_and_prepare_capture_does_not(fakes):
    controller = _controller(_Brain([_observation()]))
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")

    assert controller.prepare_capture() is True
    assert controller.has_session()
    controller.hide()
    assert not controller.has_session()
    assert controller.has_previous_session()
    assert controller._observations == []
    assert controller._bridges == []


def test_inactive_alt_s_offers_explicit_previous_session_restore(fakes):
    brain = _Brain([_observation(), _observation(observed_at=2000)])
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")
    _flush_idle()
    original_analysis_calls = panel.analysis_calls
    original_pair_calls = len([call for call in brain.calls if call["cmd"] == "arbpair"])

    controller.hide()
    controller.start(2, lambda _gen: True)
    _flush_idle()

    assert not controller.has_session()
    assert panel.choice[3]["restore_target_name"] == "Omen of Whittling"
    panel.choice[3]["on_restore"]()
    _flush_idle()

    assert controller.has_session()
    assert controller._target["apiId"] == "omen"
    assert [item["id"] for item in controller._observations] == ["omen->chaos"]
    assert panel.visible
    assert panel.analysis_calls == original_analysis_calls + 1
    assert len([call for call in brain.calls if call["cmd"] == "arbpair"]) == (
        original_pair_calls + 1
    )
    analyze_calls = [call for call in brain.calls if call["cmd"] == "arbanalyze"]
    assert analyze_calls[-1]["reuseRates"] is True


def test_alt_s_without_saved_session_has_no_restore_action(fakes):
    controller = _controller(_Brain([_observation()]))

    controller.start(1, lambda _gen: True)
    _flush_idle()

    assert fakes.instances[0].choice[3]["on_restore"] is None


def test_starting_new_target_archives_the_active_session(fakes):
    brain = _Brain(
        [_observation(observed_at=1000), _observation(want="exalted", observed_at=2000)]
    )
    controller = _controller(brain)
    controller.start(1, lambda _gen: True)
    _flush_idle()
    panel = fakes.instances[0]
    panel.choice[1]("omen")
    first_session_id = controller._session_id

    controller.start(2, lambda _gen: True)
    _flush_idle()
    assert panel.choice[3]["restore_target_name"] == "Omen of Whittling"
    panel.choice[1]("omen")
    _flush_idle()

    assert controller._previous_session["session_id"] == first_session_id
    assert controller._session_id != first_session_id


def test_capture_failure_is_visible(fakes, monkeypatch):
    monkeypatch.setattr(
        arb_check.currency_exchange_scan,
        "capture",
        lambda _desktop: (_ for _ in ()).throw(RuntimeError("Currency Exchange is not visible")),
    )
    controller = _controller(_Brain([]))
    controller.start(1, lambda _gen: True)
    _flush_idle()
    assert "not visible" in fakes.instances[0].error[0]


def test_sync_config_uses_newly_selected_league(fakes):
    brain = _Brain([_observation()])
    controller = _controller(brain)
    controller.sync_config({"league": "New League", "arb_min_percent": 5.0})

    controller.start(1, lambda _gen: True)
    _flush_idle()

    assert brain.calls[0]["league"] == "New League"


def test_analysis_diagnostics_preserve_rates_legs_and_useful_quantity_samples():
    target = _item("omen", "Omen of Whittling")
    chaos = _item("chaos", "Chaos Orb")
    exalted = _item("exalted", "Exalted Orb")
    outcomes = [
        {
            "quantity": quantity,
            "nominalFinalUnits": quantity,
            "executionFinalUnits": quantity - 1,
            "bufferedFinalUnits": quantity - 1,
            "nominalComplete": True,
            "executionComplete": True,
            "bufferedComplete": True,
            "nominalReturnPercent": 0,
            "executionReturnPercent": -100 / quantity,
            "bufferedReturnPercent": -100 / quantity,
            "budgetBest": quantity == 7,
            "localPeak": False,
            "actionable": False,
        }
        for quantity in range(1, 101)
    ]
    answer = {
        "target": target,
        "ratesEpoch": "epoch",
        "ratesSnapshotId": 42,
        "ratesFetchedAt": 1234,
        "ratesAgeMs": 50,
        "ratesStatus": "fresh",
        "safetyBufferBps": 500,
        "perLegSafetyBufferBps": 169.52,
        "executionConcessionBps": 500,
        "executionConcessionLoopPercent": 14.2625,
        "loopsEvaluated": 1,
        "capturedCurrencyCount": 2,
        "loops": [
            {
                "id": "omen->chaos->exalted->omen",
                "path": [target, chaos, exalted, target],
                "status": "estimate",
                "estimateConfidence": "reliable",
                "stale": False,
                "nominalPercent": 8,
                "executionPercent": -7.4,
                "bufferedPercent": 2.6,
                "actionable": False,
                "legs": [
                    {
                        "from": target,
                        "to": chaos,
                        "rate": 81,
                        "executionRate": 76.95,
                        "source": "capture",
                        "inputAmount": 1,
                        "outputAmount": 81,
                        "observedAt": 1000,
                    },
                    {
                        "from": chaos,
                        "to": exalted,
                        "rate": 1 / 15,
                        "source": "poe2scout",
                        "scoutEvidence": {"confidence": "reliable"},
                    },
                ],
                "quantityOutcomes": outcomes,
            }
        ],
    }

    concise = arb_check._analysis_log(answer, include_outcomes=False)
    detailed = arb_check._analysis_log(answer, include_outcomes=True)

    assert concise["rates"]["snapshotId"] == 42
    assert concise["loops"][0]["legs"][0]["inputAmount"] == 1
    assert concise["loops"][0]["legs"][1]["source"] == "poe2scout"
    assert concise["executionConcessionBps"] == 500
    assert concise["loops"][0]["legs"][0]["executionRate"] == 76.95
    quantities = {
        point["q"] for point in detailed["loops"][0]["quantityOutcomes"]
    }
    assert quantities == {1, 5, 7, 10, 25, 50, 100}
    assert 2 not in quantities
