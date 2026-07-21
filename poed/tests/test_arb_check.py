"""ArbCheckController flow tests with fake panel/brain (no display needed)."""

import threading

import pytest

from poed import arb_check


class _FakePanel:
    instances = []

    def __init__(self, *args, **kwargs):
        self.answers = []
        self.states = []
        self.visible = False
        _FakePanel.instances.append(self)

    def is_visible(self):
        return self.visible

    def show_answer(self, answer, league):
        self.answers.append((answer, league))
        self.visible = True

    def update_state(self, state, league):
        self.states.append((state, league))

    def hide(self):
        self.visible = False


class _Brain:
    def __init__(self, answer, states=None):
        self.answer = answer
        self.states = states or []
        self.calls = []

    def request(self, msg, **_kwargs):
        self.calls.append(msg["cmd"])
        if msg["cmd"] == "arbquote":
            return self.answer
        if msg["cmd"] == "arbstate":
            return self.states.pop(0) if self.states else {"done": True, "matrix": [], "itemRows": []}
        raise AssertionError(f"unexpected cmd {msg['cmd']}")


class _Desktop:
    def __init__(self, focused=True):
        self.focused = focused

    def is_game_focused(self):
        return self.focused[0] if isinstance(self.focused, list) else self.focused


@pytest.fixture
def fakes(monkeypatch):
    _FakePanel.instances = []
    monkeypatch.setattr(arb_check, "ArbPanel", _FakePanel)
    monkeypatch.setattr(
        arb_check.clipboard, "grab_item_text", lambda *_args: "Item Class: Currency\nDivine Orb"
    )
    return _FakePanel


def _controller(brain, desktop=None):
    return arb_check.ArbCheckController(
        application=object(),
        cfg={"league": "Standard", "account_name": "", "poesessid": ""},
        brain=brain,
        desktop=desktop or _Desktop(),
        on_visibility_changed=lambda: None,
    )


def _flush_idle():
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def test_run_shows_stage1_answer_and_starts_poll(fakes):
    answer = {"mode": "commodity", "refreshId": 7, "league": "Standard", "matrix": [], "itemRows": []}
    brain = _Brain(answer)
    controller = _controller(brain)

    controller.run(1, lambda gen: gen == 1)
    _flush_idle()

    panel = fakes.instances[0]
    assert panel.answers == [(answer, "Standard")]
    assert controller._refresh_id == 7
    assert controller.is_visible()
    controller.hide()


def test_stale_generation_does_not_show(fakes):
    answer = {"mode": "commodity", "refreshId": 7, "league": "Standard", "matrix": [], "itemRows": []}
    brain = _Brain(answer)
    controller = _controller(brain)

    controller.run(1, lambda gen: gen == 2)  # superseded mid-flight
    _flush_idle()

    assert fakes.instances == []
    assert not controller.is_visible()


def test_apply_state_updates_and_stops_on_done(fakes):
    answer = {"mode": "commodity", "refreshId": 7, "league": "Standard", "matrix": [], "itemRows": []}
    brain = _Brain(answer)
    controller = _controller(brain)
    controller.run(1, lambda gen: gen == 1)
    _flush_idle()

    stop = controller._poll_stop
    state = {"done": True, "matrix": [{"key": "pair:divine", "source": "live"}], "itemRows": []}
    controller._apply_state(1, lambda gen: gen == 1, state, stop)

    panel = fakes.instances[0]
    assert panel.states == [(state, "Standard")]
    assert stop.is_set()
    controller.hide()


def test_apply_state_hides_on_game_focus_return(fakes):
    focused = [False]
    answer = {"mode": "commodity", "refreshId": 7, "league": "Standard", "matrix": [], "itemRows": []}
    brain = _Brain(answer)
    controller = _controller(brain, desktop=_Desktop(focused))
    controller.run(1, lambda gen: gen == 1)
    _flush_idle()

    panel = fakes.instances[0]
    stop = threading.Event()
    controller._apply_state(1, lambda gen: gen == 1, {"done": False, "matrix": []}, stop)
    assert panel.visible  # seen unfocused, still shown

    focused[0] = True
    controller._apply_state(1, lambda gen: gen == 1, {"done": False, "matrix": []}, stop)
    assert not panel.visible  # focus returned -> auto-hide


def test_brain_failure_leaves_no_panel(fakes):
    class _BadBrain:
        def request(self, *_args, **_kwargs):
            raise RuntimeError("brain down")

    controller = _controller(_BadBrain())
    controller.run(1, lambda gen: gen == 1)
    _flush_idle()

    assert fakes.instances == []
    assert not controller.is_visible()


def test_hide_stops_poll_and_hides_panel(fakes):
    answer = {"mode": "commodity", "refreshId": 7, "league": "Standard", "matrix": [], "itemRows": []}
    brain = _Brain(answer)
    controller = _controller(brain)
    controller.run(1, lambda gen: gen == 1)
    _flush_idle()

    panel = fakes.instances[0]
    stop = controller._poll_stop
    controller.hide()

    assert stop.is_set()
    assert not panel.visible
    assert controller._poll_stop is None
