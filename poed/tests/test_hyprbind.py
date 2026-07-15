import json

from poed.image_geometry import Rect
from poed.hyprbind import BindManager, EscBind, _norm, resolve_shortcut_name


class FakeCtl:
    def __init__(self):
        self.calls = []
        self.ok = True

    def __call__(self, *args):
        self.calls.append(args)
        return self.ok


def _resolved(sid):
    return f"xdg-terminal-exec:{sid}"


def test_game_open_event_binds_once():
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>5933a0c0,4,steam_app_2694490,Path of Exile 2")
    m.handle_line("openwindow>>5933a0c0,4,steam_app_2694490,Path of Exile 2")
    assert ctl.calls == [("keyword", "bind", "CTRL,D,global,xdg-terminal-exec:price-check")]


def test_close_event_unbinds():
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>5933a0c0,4,steam_app_2694490,Path of Exile 2")
    m.handle_line("closewindow>>5933a0c0")
    assert ctl.calls[-1] == ("keyword", "unbind", "CTRL,D")


def test_other_windows_ignored():
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>aabb,2,firefox,Mozilla Firefox")
    m.handle_line("closewindow>>aabb")
    assert ctl.calls == []


def test_unbind_on_stop_only_when_bound():
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.stop()
    assert ctl.calls == []          # never bound -> nothing to undo
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    m.stop()
    assert ctl.calls[-1] == ("keyword", "unbind", "CTRL,D")


# --- new RED tests -----------------------------------------------------------

def test_norm_strips_0x_prefix():
    assert _norm("0x5933a0c0") == "5933a0c0"
    assert _norm("5933a0c0") == "5933a0c0"
    assert _norm("0x560e297adf80") == "560e297adf80"
    assert _norm("  0x5933a0c0  ") == "5933a0c0"


def test_open_with_0x_addr_and_bare_close_match():
    """openwindow events may carry 0x prefix; closewindow uses bare hex.
    After normalization both must refer to the same window."""
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>0x5933a0c0,4,steam_app_2694490,Path of Exile 2")
    m.handle_line("closewindow>>5933a0c0")
    assert ctl.calls[-1] == ("keyword", "unbind", "CTRL,D")


def test_prime_address_normalized_against_socket_events(monkeypatch):
    """prime() reads 0x-prefixed addresses from hyprctl clients -j.
    A subsequent closewindow (bare hex) must still match."""
    import json
    import subprocess

    fake_clients = [{"class": "steam_app_2694490", "address": "0x5933a0c0"}]

    class FakeResult:
        stdout = json.dumps(fake_clients)
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())

    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.prime()
    # prime should have bound (game is running)
    assert ctl.calls[-1] == ("keyword", "bind", "CTRL,D,global,xdg-terminal-exec:price-check")
    # now close event arrives with bare hex — must unbind
    m.handle_line("closewindow>>5933a0c0")
    assert ctl.calls[-1] == ("keyword", "unbind", "CTRL,D")


def test_failed_bind_call_does_not_flip_state():
    """If _ctl returns False, _bound must stay False so the next open retries."""
    ctl = FakeCtl()
    ctl.ok = False
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    # bind failed -> _bound must still be False
    assert m._bound is False
    # now ctl works — second open (new addr) must retry bind
    ctl.ok = True
    m.handle_line("openwindow>>2,1,steam_app_2694490,t")
    assert ctl.calls[-1] == ("keyword", "bind", "CTRL,D,global,xdg-terminal-exec:price-check")


def test_failed_unbind_does_not_flip_state():
    """If unbind call fails, _bound must stay True so stop() retries."""
    ctl = FakeCtl()
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=_resolved)
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    assert m._bound is True
    ctl.ok = False
    m.handle_line("closewindow>>1")
    assert m._bound is True  # unbind failed; still consider ourselves bound
    # recovery: game window re-opens, closes again, ctl now works
    ctl.ok = True
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    m.handle_line("closewindow>>1")
    assert m._bound is False


# --- runtime appid resolution -----------------------------------------------

def test_resolve_shortcut_name_parses_hyprctl_json():
    raw = '[{"name": "xdg-terminal-exec:price-check", "description": "PoE2 price check"}]'
    assert resolve_shortcut_name("price-check", _raw=raw) == "xdg-terminal-exec:price-check"
    assert resolve_shortcut_name("missing", _raw=raw) is None
    assert resolve_shortcut_name("price-check", _raw="garbage") is None


def test_unresolved_shortcut_defers_bind_and_retries():
    ctl = FakeCtl()
    names = iter([None, "xdg-terminal-exec:price-check"])
    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=lambda sid: next(names))
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    assert ctl.calls == []          # not registered yet -> no bind, no state flip
    m.notify_registered()           # portal BindShortcuts completed
    assert ctl.calls[-1] == ("keyword", "bind", "CTRL,D,global,xdg-terminal-exec:price-check")


def test_resolution_cached_once_found():
    ctl = FakeCtl()
    calls = {"n": 0}

    def resolver(sid):
        calls["n"] += 1
        return "a:price-check"

    m = BindManager("steam_app_2694490", "CTRL", "D", "price-check",
                    _ctl=ctl, _resolve=resolver)
    m.handle_line("openwindow>>1,1,steam_app_2694490,t")
    m.handle_line("closewindow>>1")
    m.handle_line("openwindow>>2,1,steam_app_2694490,t")
    assert calls["n"] == 1


# --- EscBind: panel-scoped consuming Esc bind --------------------------------

def test_escbind_binds_once_on_double_show():
    ctl = FakeCtl()
    e = EscBind("panel-close", _ctl=ctl, _resolve=_resolved)
    e.show()
    e.show()
    assert ctl.calls == [("keyword", "bind", ",Escape,global,xdg-terminal-exec:panel-close")]


def test_escbind_unbinds_on_hide():
    ctl = FakeCtl()
    e = EscBind("panel-close", _ctl=ctl, _resolve=_resolved)
    e.show()
    e.hide()
    assert ctl.calls[-1] == ("keyword", "unbind", ",Escape")


def test_escbind_hide_when_never_bound_does_nothing():
    ctl = FakeCtl()
    e = EscBind("panel-close", _ctl=ctl, _resolve=_resolved)
    e.hide()
    assert ctl.calls == []


def test_escbind_deferred_when_unresolved_then_binds_next_show():
    ctl = FakeCtl()
    names = iter([None, "xdg-terminal-exec:panel-close"])
    e = EscBind("panel-close", _ctl=ctl, _resolve=lambda sid: next(names))
    e.show()
    assert ctl.calls == []  # not registered yet -> no bind, no state flip
    e.show()
    assert ctl.calls[-1] == ("keyword", "bind", ",Escape,global,xdg-terminal-exec:panel-close")


def test_escbind_failed_ctl_does_not_flip_state():
    ctl = FakeCtl()
    ctl.ok = False
    e = EscBind("panel-close", _ctl=ctl, _resolve=_resolved)
    e.show()
    assert e._bound is False
    ctl.ok = True
    e.show()
    assert ctl.calls[-1] == ("keyword", "bind", ",Escape,global,xdg-terminal-exec:panel-close")


def test_escbind_resolution_cached():
    ctl = FakeCtl()
    calls = {"n": 0}

    def resolver(sid):
        calls["n"] += 1
        return "a:panel-close"

    e = EscBind("panel-close", _ctl=ctl, _resolve=resolver)
    e.show()
    e.hide()
    e.show()
    assert calls["n"] == 1


def test_escbind_stop_is_hide():
    assert EscBind.stop is EscBind.hide


def test_multi_bind_manager_binds_and_unbinds_all():
    ctl = FakeCtl()
    from poed.hyprbind import MultiBindManager
    mgr = MultiBindManager.create(
        "steam_app_2694490",
        [("ALT", "Z", "price-check"), ("ALT", "X", "unique-scan")],
        _ctl=ctl, _resolve=_resolved,
    )
    mgr.handle_line("openwindow>>abc123,1,steam_app_2694490,PoE2")
    binds = [a for a in ctl.calls if a[1] == "bind"]
    assert len(binds) == 2
    assert any("xdg-terminal-exec:price-check" in a[2] for a in binds)
    assert any("xdg-terminal-exec:unique-scan" in a[2] for a in binds)
    ctl.calls.clear()
    mgr.handle_line("closewindow>>abc123")
    unbinds = [a for a in ctl.calls if a[1] == "unbind"]
    assert {a[2] for a in unbinds} == {"ALT,Z", "ALT,X"}
    ctl.calls.clear()
    mgr.handle_line("openwindow>>abc123,1,steam_app_2694490,PoE2")
    mgr.stop()
    assert {a[2] for a in ctl.calls if a[1] == "unbind"} == {"ALT,Z", "ALT,X"}


def test_active_game_rect_maps_hypr_geometry_to_output_pixels(monkeypatch):
    from poed import hyprbind

    def fake_hyprctl(*args):
        if args == ("activewindow", "-j"):
            return json.dumps(
                {
                    "class": "steam_app_2694490",
                    "monitor": 1,
                    "at": [2020, 100],
                    "size": [1000, 700],
                }
            )
        if args == ("monitors", "-j"):
            return json.dumps(
                [
                    {
                        "id": 0,
                        "name": "HDMI-A-1",
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                    },
                    {
                        "id": 1,
                        "name": "DP-2",
                        "x": 1920,
                        "y": 0,
                        "width": 2560,
                        "height": 1440,
                    },
                ]
            )
        if args == ("clients", "-j"):
            return "[]"
        raise AssertionError(args)

    monkeypatch.setattr(hyprbind, "_hyprctl_out", fake_hyprctl)

    assert hyprbind.active_game_output("steam_app_2694490") == "DP-2"
    assert hyprbind.active_game_rect("steam_app_2694490", "DP-2", (5120, 2880)) == Rect(
        200, 200, 2000, 1400
    )
