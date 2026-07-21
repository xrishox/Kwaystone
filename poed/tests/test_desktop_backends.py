import os

import pytest
from gi.repository import GLib

from poed import config
from poed.desktop import create_backend
from poed.desktop.hyprland import HyprlandBackend
from poed.desktop.kwin import KWinBackend, _build_esc_script, _build_script, _KwinScript
from poed.image_geometry import Rect


def _clean_desktop_env(monkeypatch):
    for name in (
        "WAYSTONE_DESKTOP_BACKEND",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_DESKTOP",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "WAYLAND_DISPLAY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_create_backend_prefers_forced_kde(monkeypatch):
    _clean_desktop_env(monkeypatch)
    monkeypatch.setenv("WAYSTONE_DESKTOP_BACKEND", "kde")

    assert isinstance(create_backend(config.DEFAULTS), KWinBackend)


def test_create_backend_detects_hyprland(monkeypatch):
    _clean_desktop_env(monkeypatch)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")

    assert isinstance(create_backend(config.DEFAULTS), HyprlandBackend)


def test_create_backend_rejects_unknown_wayland(monkeypatch):
    _clean_desktop_env(monkeypatch)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    with pytest.raises(SystemExit):
        create_backend(config.DEFAULTS)


def test_hyprland_backend_uses_configured_price_hotkey():
    cfg = {**config.DEFAULTS, "hotkey_price": "CTRL+d", "hotkey_arb": "CTRL+s"}
    backend = HyprlandBackend(cfg)

    assert backend.portal_shortcuts()[0] == (
        "price-check",
        "PoE2 price check",
        "CTRL+d",
    )
    ids = [shortcut[0] for shortcut in backend.portal_shortcuts()]
    assert ids == ["price-check", "unique-scan", "arb-check", "panel-close"]


def test_kwin_portal_shortcuts_excludes_esc_and_uses_configured_price_key():
    cfg = {**config.DEFAULTS, "hotkey_price": "CTRL+d", "hotkey_arb": "CTRL+s"}
    backend = KWinBackend(cfg)

    shortcuts = backend.portal_shortcuts()

    # Esc is never portal-bound: a statically bound Esc would be consumed
    # globally and never reach the game. It is bound natively, only while a
    # panel is visible.
    assert shortcuts == [
        ("price-check", "PoE2 price check", "CTRL+d"),
        ("unique-scan", "Scan current PoE2 screen", "ALT+x"),
        ("arb-check", "Currency arbitrage", "CTRL+s"),
    ]


def test_kwin_portal_session_token_is_stable_for_persistent_bindings():
    # The KDE portal derives the kglobalaccel component from this token; a
    # stable token is what makes the first-run grant persist across launches.
    assert KWinBackend(config.DEFAULTS).portal_session_token() == "kwaystone"
    # Hyprland's portal names shortcuts from the systemd scope instead; a
    # random per-run token there is intentional.
    assert HyprlandBackend(config.DEFAULTS).portal_session_token() is None


def test_kwin_script_is_tracker_only():
    script = _build_script("steam_app_2694490")

    assert "steam_app_2694490" in script
    # Shortcut registration moved to KGlobalAccel; script reloads must never
    # create re-bind windows again.
    assert "registerShortcut" not in script
    assert "GENERATION" not in script
    assert "if (!w) return;" in script
    assert '"Geometry"' in script


def test_kwin_load_tracker_runs_loaded_script(tmp_path):
    class FakeBus:
        def __init__(self):
            self.calls = []

        def call_sync(self, bus, path, iface, method, params, *rest):
            self.calls.append((bus, path, iface, method))
            if method == "loadScript":
                return GLib.Variant("(i)", (7,))
            if method == "unloadScript":
                return GLib.Variant("(b)", (True,))
            return None

    bus = FakeBus()
    backend = KWinBackend(config.DEFAULTS)
    backend._tracker = _KwinScript(bus, tmp_path / "waystone-kwin.js")

    backend._load_tracker()

    assert (
        "org.kde.KWin",
        "/Scripting/Script7",
        "org.kde.kwin.Script",
        "run",
    ) in bus.calls


def test_kwin_startup_clears_legacy_script_shortcuts(tmp_path):
    class FakeBus:
        def __init__(self):
            self.unregistered = []

        def call_sync(self, bus, path, iface, method, params, *rest):
            if method == "shortcutNames":
                return GLib.Variant(
                    "(as)",
                    (["waystone-1-price-check-0", "other-app-shortcut"],),
                )
            if method == "unregister":
                self.unregistered.append(params.unpack())
                return None
            return None

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    backend._clear_prior_shortcuts()

    assert backend._bus.unregistered == [("kwin", "waystone-1-price-check-0")]


def test_kwin_portal_preflight_accepts_real_variant_shape():
    # Regression: PyGObject unpacks (v) replies already unboxed; assuming a
    # boxed Variant raised AttributeError and aborted desktop.start().
    class FakeBus:
        def call_sync(self, *args):
            return GLib.Variant("(v)", (GLib.Variant("u", 2),))

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    backend._check_portal()

    assert backend.portal_error is None


def test_kwin_portal_preflight_records_portal_failure():
    class FakeBus:
        def call_sync(self, *args):
            raise GLib.Error("portal missing")

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    backend._check_portal()

    assert "GlobalShortcuts portal unavailable" in backend.portal_error


def test_kwin_esc_script_binds_only_esc_to_panel_close():
    script = _build_esc_script()

    assert "registerShortcut" in script
    assert '"Esc"' in script
    assert '"panel-close"' in script
    assert "waystone-panel-close" in script
    # The tracker keeps owning game detection; the Esc script must not grow a
    # second copy of it.
    assert "GAME_CLASS" not in script


def test_kwin_esc_binding_follows_panel_visibility(tmp_path):
    class FakeBus:
        def __init__(self):
            self.calls = []

        def call_sync(self, bus, path, iface, method, params, *rest):
            self.calls.append(method)
            if method == "loadScript":
                return GLib.Variant("(i)", (7,))
            if method == "unloadScript":
                return GLib.Variant("(b)", (True,))
            return None

    backend = KWinBackend(config.DEFAULTS)
    backend._esc = _KwinScript(FakeBus(), tmp_path / "waystone-kwin-esc.js")

    backend.set_panel_visible(True)
    backend._esc_thread.join(timeout=5.0)
    methods = backend._esc._bus.calls
    assert "loadScript" in methods and "run" in methods

    # Repeating the same visibility must not churn script loads.
    backend.set_panel_visible(True)
    assert backend._esc._bus.calls == methods

    backend.set_panel_visible(False)
    backend._esc_thread.join(timeout=5.0)
    methods = backend._esc._bus.calls
    assert "stop" in methods and "unloadScript" in methods

    backend.set_panel_visible(False)
    assert backend._esc._bus.calls == methods


def test_kwin_esc_load_failure_degrades_without_raising(tmp_path):
    class FakeBus:
        def call_sync(self, bus, path, iface, method, params, *rest):
            if method == "loadScript":
                return GLib.Variant("(i)", (-1,))
            return None

    backend = KWinBackend(config.DEFAULTS)
    backend._esc = _KwinScript(FakeBus(), tmp_path / "waystone-kwin-esc.js")

    backend.set_panel_visible(True)  # KWin refusal is logged, not raised
    backend._esc_thread.join(timeout=5.0)
    assert backend._panel_visible is True
    assert backend._esc.loaded is False


def test_kwin_kwin_watch_ignores_initial_fire_and_reloads_on_restart(tmp_path):
    loads = []

    class FakeScript:
        def __init__(self):
            self.reset_calls = 0

        def reset(self):
            self.reset_calls += 1

        def load(self, _content):
            loads.append(True)

    backend = KWinBackend(config.DEFAULTS)
    backend._tracker = FakeScript()
    backend._esc = FakeScript()

    # First fire at watch registration: records the owner, no reload.
    backend._on_kwin_appeared(None, "org.kde.KWin", ":1.10")
    assert loads == []
    assert backend._tracker.reset_calls == 0

    # Same owner again: still nothing.
    backend._on_kwin_appeared(None, "org.kde.KWin", ":1.10")
    assert loads == []

    # Owner change = compositor restart: scripts reset and reloaded.
    backend._on_kwin_appeared(None, "org.kde.KWin", ":1.22")
    backend._reload_thread.join(timeout=5.0)
    assert loads == [True]
    assert backend._tracker.reset_calls == 1
    assert backend._esc.reset_calls == 1

    # Panel visible at restart time: the Esc binding is re-armed too.
    backend._panel_visible = True
    backend._on_kwin_appeared(None, "org.kde.KWin", ":1.33")
    backend._reload_thread.join(timeout=5.0)
    assert loads == [True, True, True]


def test_kwin_dispatch_gates_on_focus_but_not_registration():
    backend = KWinBackend(config.DEFAULTS)
    seen = []
    backend._on_activated = seen.append
    backend._focused = False
    backend._panel_visible = False
    backend._dispatch_hotkey("unique-scan")
    assert seen == []
    backend._focused = True
    backend._dispatch_hotkey("unique-scan")
    assert seen == ["unique-scan"]
    backend._focused = False
    backend._dispatch_hotkey("panel-close")
    assert seen == ["unique-scan", "panel-close"]


def test_kwin_capture_output_decodes_fd_bytes(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    img = np.full((8, 10, 3), 127, np.uint8)
    ok, encoded = cv2.imencode(".png", img)
    assert ok

    class FakeBus:
        def call_with_unix_fd_list_sync(self, *args):
            params = args[4]
            fd_list = args[8]
            output, _options, handle = params.unpack()
            assert output == "DP-2"
            os.write(fd_list.get(handle), encoded.tobytes())

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    out = backend.capture_output("DP-2")

    assert out.shape == img.shape


def test_kwin_capture_output_decodes_raw_fd_bytes():
    np = pytest.importorskip("numpy")
    raw = np.array(
        [
            [[1, 2, 3, 255], [4, 5, 6, 255]],
            [[7, 8, 9, 255], [10, 11, 12, 255]],
        ],
        dtype=np.uint8,
    )

    class FakeBus:
        def call_with_unix_fd_list_sync(self, *args):
            params = args[4]
            fd_list = args[8]
            output, _options, handle = params.unpack()
            assert output == "DP-2"
            os.write(fd_list.get(handle), raw.tobytes())
            return GLib.Variant(
                "(a{sv})",
                (
                    {
                        "type": GLib.Variant("s", "raw"),
                        "format": GLib.Variant("u", 6),
                        "width": GLib.Variant("u", 2),
                        "height": GLib.Variant("u", 2),
                        "stride": GLib.Variant("u", 8),
                    },
                ),
            )

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    out = backend.capture_output("DP-2")

    assert out.shape == (2, 2, 3)
    assert out.tolist() == raw[:, :, :3].tolist()


def test_kwin_capture_output_returns_none_on_failure():
    class FakeBus:
        def call_with_unix_fd_list_sync(self, *args):
            raise OSError("denied")

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    assert backend.capture_output("DP-2") is None


def test_kwin_capture_output_falls_back_to_screenshot_portal(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    img = np.full((6, 9, 3), 91, np.uint8)
    target = tmp_path / "portal.png"
    assert cv2.imwrite(str(target), img)

    class FakeBus:
        callback = None

        def call_with_unix_fd_list_sync(self, *args):
            raise OSError("restricted interface denied")

        def get_unique_name(self):
            return ":1.44"

        def signal_subscribe(self, *args):
            self.callback = args[6]
            return 7

        def signal_unsubscribe(self, _subscription):
            pass

        def call_sync(self, *args):
            params = args[4]
            _parent, options = params.unpack()
            assert options["interactive"] is False
            # Screenshot portal v3 only accepts target values advertised by
            # AvailableTargets. KDE portal v2 advertises none, so omitting the
            # option preserves its compatible full-screen behavior.
            assert "target" not in options
            token = options["handle_token"]
            path = f"/org/freedesktop/portal/desktop/request/1_44/{token}"
            response = GLib.Variant(
                "(ua{sv})",
                (
                    0,
                    {"uri": GLib.Variant("s", target.as_uri())},
                ),
            )
            self.callback(None, None, path, None, None, response, None)
            return GLib.Variant("(o)", (path,))

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()

    out = backend.capture_output("DP-2")

    assert out.shape == img.shape
    assert out.tolist() == img.tolist()
    assert not target.exists()


def test_kwin_portal_capture_crops_full_workspace_to_active_output(monkeypatch):
    np = pytest.importorskip("numpy")
    image = np.arange(5 * 10 * 3, dtype=np.uint8).reshape((5, 10, 3))
    backend = KWinBackend(config.DEFAULTS)
    backend._game_rect_output = "DP-2"
    backend._output_rect_global = Rect(4, 1, 6, 4)
    monkeypatch.setattr(
        "poed.desktop.kwin.workspace_rect_physical", lambda: Rect(0, 0, 10, 5)
    )
    monkeypatch.setattr(
        "poed.desktop.kwin.monitor_scale_factor", lambda _connector: 1.0
    )

    cropped = backend._crop_portal_output(image, "DP-2")

    assert cropped.shape == (4, 6, 3)
    assert cropped.tolist() == image[1:5, 4:10].tolist()


def test_kwin_portal_capture_scales_target_for_fractional_output(monkeypatch):
    np = pytest.importorskip("numpy")
    image = np.arange(5 * 10 * 3, dtype=np.uint8).reshape((5, 10, 3))
    backend = KWinBackend(config.DEFAULTS)
    backend._game_rect_output = "DP-2"
    # Logical output rect 2x2 at (2,0); at 2x scale that's physical 4x4 at (4,0).
    backend._output_rect_global = Rect(2, 0, 2, 2)
    monkeypatch.setattr(
        "poed.desktop.kwin.workspace_rect_physical", lambda: Rect(0, 0, 10, 5)
    )
    monkeypatch.setattr(
        "poed.desktop.kwin.monitor_scale_factor", lambda _connector: 2.0
    )

    cropped = backend._crop_portal_output(image, "DP-2")

    assert cropped.shape == (4, 4, 3)
    assert cropped.tolist() == image[0:4, 4:8].tolist()


def test_kwin_geometry_maps_window_to_captured_output_pixels():
    class Invocation:
        def return_value(self, _value):
            pass

    backend = KWinBackend(config.DEFAULTS)
    params = GLib.Variant(
        "(siiiiiiii)",
        ("DP-2", 2100, 100, 1600, 900, 1920, 0, 2560, 1440),
    )

    backend._on_method_call(None, None, None, None, "Geometry", params, Invocation())

    assert backend.active_game_rect("DP-2", (5120, 2880)) == Rect(360, 200, 3200, 1800)
    assert backend.active_game_rect("HDMI-A-1", (5120, 2880)) is None
