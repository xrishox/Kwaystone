import threading

import pytest


try:
    from poed import price_check
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on GTK stack
    pytest.skip(f"price-check UI unavailable: {exc}", allow_module_level=True)


class _Brain:
    def request(self, *_args, **_kwargs):
        raise AssertionError("copy failure must not contact the EE2 host")


class _Desktop:
    def is_game_focused(self):
        return True


def test_copy_failure_does_not_show_native_ui(monkeypatch):
    monkeypatch.setattr(price_check.clipboard, "grab_item_text", lambda *_args: None)
    visibility_changes = []
    controller = price_check.PriceCheckController(
        application=object(),
        cfg={
            "hotkey_price": "ALT+z",
            "league": "Runes of Aldur",
            "account_name": "",
            "poesessid": "",
        },
        brain=_Brain(),
        desktop=_Desktop(),
        on_visibility_changed=lambda: visibility_changes.append(True),
    )

    controller.run(1, lambda gen: gen == 1)

    assert visibility_changes == []
    assert not controller.is_visible()


class _Overlay:
    def __init__(self):
        self.visible = True
        self.hides = 0

    def is_visible(self):
        return self.visible

    def hide(self):
        self.visible = False
        self.hides += 1


class _FocusDesktop:
    def __init__(self, focused):
        self.focused = focused

    def is_game_focused(self):
        return self.focused[0]


def _state_controller(focused):
    controller = price_check.PriceCheckController(
        application=object(),
        cfg={
            "hotkey_price": "ALT+z",
            "league": "Standard",
            "account_name": "",
            "poesessid": "",
        },
        brain=_Brain(),
        desktop=_FocusDesktop(focused),
        on_visibility_changed=lambda: None,
    )
    controller._overlay = _Overlay()
    return controller


def _armed_poll(controller):
    stop = threading.Event()
    controller._state_poll = stop
    return stop


def test_apply_state_hides_when_game_focus_returns():
    focused = [False]
    controller = _state_controller(focused)
    stop = _armed_poll(controller)

    controller._apply_state({"hideRequestSeq": 0}, stop)
    assert controller._overlay.hides == 0  # marked seen-unfocused, still shown

    focused[0] = True
    controller._apply_state({"hideRequestSeq": 0}, stop)
    assert controller._overlay.hides == 1  # focus returned -> auto-hide


def test_apply_state_hides_on_brain_hide_request():
    focused = [True]
    controller = _state_controller(focused)
    stop = _armed_poll(controller)
    controller._hide_seq = 1

    controller._apply_state(
        {"hideRequestSeq": 2, "hideReason": "price-check-hidden"}, stop
    )

    assert controller._overlay.hides == 1
    assert controller._hide_seq == 2


def test_apply_state_ignores_malformed_hide_seq():
    focused = [True]
    controller = _state_controller(focused)
    stop = _armed_poll(controller)

    controller._apply_state({"hideRequestSeq": "not-a-number"}, stop)

    assert controller._overlay.hides == 0


def test_apply_state_stops_when_panel_gone():
    focused = [True]
    controller = _state_controller(focused)
    stop = _armed_poll(controller)
    controller._overlay.visible = False

    controller._apply_state({"hideRequestSeq": 9}, stop)

    assert stop.is_set()
    assert controller._state_poll is None
    assert controller._overlay.hides == 0


def test_apply_state_drops_late_result_after_stop():
    focused = [True]
    controller = _state_controller(focused)
    stop = _armed_poll(controller)
    stop.set()  # panel hidden between request and idle delivery

    controller._apply_state({"hideRequestSeq": 9}, stop)

    assert controller._overlay.hides == 0
