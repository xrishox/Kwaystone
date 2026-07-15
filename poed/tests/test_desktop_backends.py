import os

import pytest
from gi.repository import GLib

from poed import config
from poed.desktop import create_backend
from poed.desktop.hyprland import HyprlandBackend
from poed.desktop.kwin import KWinBackend, _build_script
from poed.desktop.base import Shortcut
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
    cfg = {**config.DEFAULTS, "hotkey_price": "CTRL+d"}
    backend = HyprlandBackend(cfg)

    assert backend.portal_shortcuts()[0] == (
        "price-check",
        "PoE2 price check",
        "CTRL+d",
    )
    ids = [shortcut[0] for shortcut in backend.portal_shortcuts()]
    assert ids == ["price-check", "unique-scan", "panel-close"]


def test_kwin_shortcut_sync_scopes_game_and_panel_keys():
    backend = KWinBackend(config.DEFAULTS)

    assert backend._desired_shortcut_ids() == ()
    backend._present = True
    assert backend._desired_shortcut_ids() == (
        "price-check",
        "unique-scan",
    )
    backend._panel_visible = True
    assert backend._desired_shortcut_ids() == (
        "price-check",
        "unique-scan",
        "panel-close",
    )
    backend._present = False
    assert backend._desired_shortcut_ids() == ("panel-close",)


def test_kwin_script_contains_tracker_and_registered_shortcuts():
    script = _build_script(
        "steam_app_2694490",
        [Shortcut("price-check", "PoE2 price check", "Alt+Z")],
        3,
    )

    assert "steam_app_2694490" in script
    assert "registerShortcut" in script
    assert "waystone-\" + GENERATION" in script
    assert '"Kwaystone: " + shortcut.description' in script
    assert '"sid": "price-check"' in script
    assert "if (!w) return;" in script
    assert '"Geometry"' in script


def test_kwin_reload_runs_loaded_script_at_kwin_script_path(tmp_path):
    class FakeBus:
        def __init__(self):
            self.calls = []

        def call_sync(self, bus, path, iface, method, params, *rest):
            self.calls.append((bus, path, iface, method))
            if method == "shortcutNames":
                return GLib.Variant("(as)", ([],))
            if method == "loadScript":
                return GLib.Variant("(i)", (7,))
            if method == "unloadScript":
                return GLib.Variant("(b)", (True,))
            return None

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()
    backend._script_path = tmp_path / "waystone-kwin.js"

    backend._reload_script(("price-check",))

    assert (
        "org.kde.KWin",
        "/Scripting/Script7",
        "org.kde.kwin.Script",
        "run",
    ) in backend._bus.calls


def test_kwin_reload_clears_stale_shortcuts(tmp_path):
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
            if method == "loadScript":
                return GLib.Variant("(i)", (7,))
            return None

    backend = KWinBackend(config.DEFAULTS)
    backend._bus = FakeBus()
    backend._script_path = tmp_path / "waystone-kwin.js"

    backend._reload_script(("price-check",))

    assert backend._bus.unregistered == [("kwin", "waystone-1-price-check-0")]


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


def test_kwin_portal_capture_crops_full_workspace_to_active_output():
    np = pytest.importorskip("numpy")
    image = np.arange(5 * 10 * 3, dtype=np.uint8).reshape((5, 10, 3))
    backend = KWinBackend(config.DEFAULTS)
    backend._game_rect_output = "DP-2"
    backend._output_rect_global = Rect(4, 1, 6, 4)
    backend._workspace_rect_global = lambda: Rect(0, 0, 10, 5)

    cropped = backend._crop_portal_output(image, "DP-2")

    assert cropped.shape == (4, 6, 3)
    assert cropped.tolist() == image[1:5, 4:10].tolist()


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
