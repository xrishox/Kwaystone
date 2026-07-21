"""GlobalShortcuts portal session tests with a faked session bus."""

import pytest
from gi.repository import GLib

import poed.portal as portal_mod


class FakeBus:
    def __init__(self):
        self.calls = []

    def get_unique_name(self):
        return ":1.99"

    def signal_subscribe(self, *args):
        return 1

    def signal_unsubscribe(self, _subscription):
        pass

    def call_sync(self, *args):
        self.calls.append(args)
        return GLib.Variant(
            "(o)", ("/org/freedesktop/portal/desktop/request/1_99/t",)
        )


def _unboxed(value):
    return value.unpack() if hasattr(value, "unpack") else value


@pytest.fixture
def fake_portal(monkeypatch):
    bus = FakeBus()
    watch = {}
    monkeypatch.setattr(portal_mod.Gio, "bus_get_sync", lambda *a: bus)
    monkeypatch.setattr(
        portal_mod.Gio,
        "bus_watch_name_on_connection",
        lambda _bus, _name, _flags, appeared, _vanished: watch.setdefault(
            "appeared", appeared
        )
        or 1,
    )
    unwatch = []
    monkeypatch.setattr(
        portal_mod.Gio, "bus_unwatch_name", lambda w: unwatch.append(w)
    )
    return bus, watch, unwatch


def _create_session_options(bus):
    creates = [c for c in bus.calls if c[3] == "CreateSession"]
    assert creates, "no CreateSession call issued"
    options = creates[-1][4].unpack()[0]
    return {key: _unboxed(value) for key, value in options.items()}


def test_stable_session_token_is_used(fake_portal):
    bus, _watch, _unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts(
        "app", lambda _sid: None, session_token="kwaystone"
    )

    gs.bind([("price-check", "desc", "ALT+z")])

    assert _create_session_options(bus)["session_handle_token"] == "kwaystone"


def test_default_session_token_is_random(fake_portal):
    bus, _watch, _unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts("app", lambda _sid: None)

    gs.bind([("price-check", "desc", "ALT+z")])

    token = _create_session_options(bus)["session_handle_token"]
    assert token.startswith("poed_")


def test_portal_restart_rebinds_with_same_shortcuts(fake_portal):
    bus, watch, _unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts(
        "app", lambda _sid: None, session_token="kwaystone"
    )
    gs.bind([("price-check", "desc", "ALT+z")])
    gs.session_handle = "/org/freedesktop/portal/desktop/session/1_99/kwaystone"
    before = len([c for c in bus.calls if c[3] == "CreateSession"])

    appeared = watch["appeared"]
    appeared(None, portal_mod.PORTAL_BUS, ":1.5")  # initial fire: no rebind
    appeared(None, portal_mod.PORTAL_BUS, ":1.5")  # same owner: still nothing
    assert len([c for c in bus.calls if c[3] == "CreateSession"]) == before

    appeared(None, portal_mod.PORTAL_BUS, ":1.6")  # restart: owner changed
    assert len([c for c in bus.calls if c[3] == "CreateSession"]) == before + 1
    assert _create_session_options(bus)["session_handle_token"] == "kwaystone"


def test_portal_appearing_after_failed_start_retries_bind(fake_portal):
    bus, watch, _unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts(
        "app", lambda _sid: None, session_token="kwaystone"
    )
    # Simulate a bind that already failed (portal was down at startup).
    gs._pending_shortcuts = [("price-check", "desc", "ALT+z")]
    gs._bind_failed = True
    before = len(bus.calls)

    watch["appeared"](None, portal_mod.PORTAL_BUS, ":1.5")

    assert [c for c in bus.calls if c[3] == "CreateSession"]
    assert len(bus.calls) > before


def test_initial_watch_fire_does_not_duplicate_in_flight_bind(fake_portal):
    bus, watch, _unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts(
        "app", lambda _sid: None, session_token="kwaystone"
    )
    gs.bind([("price-check", "desc", "ALT+z")])
    before = len([c for c in bus.calls if c[3] == "CreateSession"])

    # The watch's first fire arrives while the bind is still in flight (no
    # failure recorded): it must not issue a second CreateSession.
    watch["appeared"](None, portal_mod.PORTAL_BUS, ":1.5")

    assert len([c for c in bus.calls if c[3] == "CreateSession"]) == before


def test_stop_unwatches_portal_restart_monitor(fake_portal):
    _bus, _watch, unwatch = fake_portal
    gs = portal_mod.GlobalShortcuts("app", lambda _sid: None)

    gs.stop()
    gs.stop()  # idempotent

    assert len(unwatch) == 1
