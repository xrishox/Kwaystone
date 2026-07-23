import numpy as np

from poed import arb_monitor
from poed.image_geometry import Rect


def _item(api_id):
    return {"apiId": api_id, "name": api_id.title()}


def _loop():
    target = _item("target")
    chaos = _item("chaos")
    exalted = _item("exalted")
    return {
        "path": [target, chaos, exalted, target],
        "legs": [
            {"from": target, "to": chaos, "rate": 10},
            {"from": chaos, "to": exalted, "rate": 10},
            {"from": exalted, "to": target, "rate": 1},
        ],
    }


class _Source:
    def __init__(self, _on_frame, on_ready, _on_error):
        self.on_ready = on_ready
        self.stopped = False

    def start(self):
        self.on_ready()

    def stop(self):
        self.stopped = True


class _Desktop:
    focused = True

    def is_game_focused(self):
        return self.focused

    def active_game_output(self):
        return "monitor"

    def active_game_rect(self, _output, frame_size):
        return Rect(0, 0, *frame_size)


class _Brain:
    def __init__(self):
        self.requests = []

    def request(self, request, **_kwargs):
        self.requests.append(request)
        want = request["wantText"]
        have = request["haveText"]
        return {
            "observation": {
                "id": f"{have}->{want}",
                "want": _item(want),
                "have": _item(have),
                "wantAmount": request["wantAmount"],
                "haveAmount": request["haveAmount"],
                "rate": request["wantAmount"] / request["haveAmount"],
                "observedAt": request["observedAt"],
            }
        }


def _read(want="chaos", have="target", visual=0):
    signature = bytes([visual]) * (96 * 24)
    return type(
        "Read",
        (),
        {
            "want_text": want,
            "have_text": have,
            "want_amount": 10,
            "have_amount": 1,
            "observed_at": 1000,
            "want_visual": signature,
            "have_visual": signature,
        },
    )()


def test_pair_change_requires_two_visually_consistent_frames(monkeypatch):
    reads = iter([_read(), _read(visual=1), _read("exalted", "chaos"), _read("exalted", "chaos")])
    monkeypatch.setattr(
        arb_monitor.currency_exchange_scan,
        "read_live_frame",
        lambda _frame: next(reads),
    )
    observations = []
    states = []
    brain = _Brain()
    monitor = arb_monitor.LiveArbMonitor(
        brain,
        _Desktop(),
        "Test",
        lambda state, detail: states.append((state, detail)),
        observations.append,
        source_factory=_Source,
    )
    monitor.start(_loop())
    frame = np.zeros((100, 200, 3), dtype=np.uint8)

    for sequence in range(1, 5):
        monitor._process(frame, sequence)

    assert [observation["id"] for observation in observations] == [
        "target->chaos",
        "chaos->exalted",
    ]
    assert states[-1][0] == "tracking"
    assert {item["apiId"] for item in brain.requests[0]["knownItems"]} == {
        "target",
        "chaos",
        "exalted",
    }


def test_focus_loss_discards_pair_evidence(monkeypatch):
    desktop = _Desktop()
    monkeypatch.setattr(
        arb_monitor.currency_exchange_scan,
        "read_live_frame",
        lambda _frame: _read(),
    )
    observations = []
    monitor = arb_monitor.LiveArbMonitor(
        _Brain(), desktop, "Test", lambda *_args: None, observations.append, source_factory=_Source
    )
    monitor.start(_loop())
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    monitor._process(frame, 1)
    desktop.focused = False
    monitor._process(frame, 2)
    desktop.focused = True
    monitor._process(frame, 3)

    assert observations == []
    monitor._process(frame, 4)
    assert len(observations) == 1


def test_large_favorable_jump_needs_three_confirmations():
    monitor = arb_monitor.LiveArbMonitor(
        _Brain(), _Desktop(), "Test", lambda *_args: None, lambda *_args: None, source_factory=_Source
    )
    monitor.start(_loop())
    signature = bytes(96 * 24)
    observation = {
        "have": _item("target"),
        "want": _item("chaos"),
        "rate": 20,
    }

    def candidate(sequence):
        return {
            "sequence": sequence,
            "pair": ("target", "chaos"),
            "wantVisual": signature,
            "haveVisual": signature,
            "observation": observation,
        }

    assert monitor._confirm(candidate(1)) is False
    assert monitor._confirm(candidate(2)) is False
    assert monitor._confirm(candidate(3)) is True
