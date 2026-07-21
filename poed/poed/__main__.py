import os
import sys
import ctypes.util


def _layer_shell_library() -> str | None:
    configured = os.environ.get("WAYSTONE_LAYER_SHELL")
    if configured:
        return configured
    found = ctypes.util.find_library("gtk4-layer-shell")
    if found:
        return found
    for candidate in (
        "/usr/lib/libgtk4-layer-shell.so.0",
        "/usr/lib64/libgtk4-layer-shell.so.0",
        "/usr/lib/libgtk4-layer-shell.so",
        "/usr/lib64/libgtk4-layer-shell.so",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


_LAYER_SHELL = _layer_shell_library()
if (
    _LAYER_SHELL
    and "libgtk4-layer-shell" not in os.environ.get("LD_PRELOAD", "")
):
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{_LAYER_SHELL} {env.get('LD_PRELOAD', '')}".strip()
    os.execve(sys.executable, [sys.executable, "-m", "poed", *sys.argv[1:]], env)

import logging
import signal
import threading
import time
import tempfile
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gtk, Gio, GLib, GLibUnix  # noqa: E402

from poed import config
from poed import leagues
from poed import log as log_mod
from poed import brain as brain_module
from poed.brain import Brain
from poed import scanners as screen_scan
from poed.scanners import debug_io
from poed.desktop import create_backend
from poed.portal import GlobalShortcuts
from poed.positions import PositionStore
from poed.price_check import PriceCheckController
from poed.scan_ui import ScanResultController


@dataclass(frozen=True)
class _Choice:
    label: str
    value: str


class App:
    def __init__(self, application, cfg, brain, positions, desktop):
        self.application = application
        self.cfg = cfg
        self.brain = brain
        self.positions = positions
        self.desktop = desktop
        self.price_check = PriceCheckController(
            application, cfg, brain, desktop, self._refresh_panel_visible
        )
        self.scan_ui = None
        self.control_window = None
        self._hotkey_status = ""
        self._hotkey_status_label = None
        self._league_dd = None
        self._league_status = None
        self._league_names = [cfg["league"]]
        self._league_guard = False
        self._press_lock = threading.Lock()
        self.in_flight_gen = None
        self.in_flight_since = 0.0
        self.pending_press = None
        self._rewarm_running = False
        self.gen = 0
        self.dismissed_gen = None
        self._shutting_down = False
        # References kept so GLib/portal objects aren't GC'd while alive.
        self._shortcuts = None

    def _is_current_gen(self, gen: int) -> bool:
        return gen == self.gen

    def _deliver_scan_error(self, gen, message):
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        if gen == self.dismissed_gen:
            _LOG.info("error dropped: generation %s was dismissed", gen)
            return GLib.SOURCE_REMOVE
        if self.scan_ui is not None:
            self.scan_ui.show_scan_error(message)
        return GLib.SOURCE_REMOVE

    def _start_screen_scan(self, gen, t0):
        if gen != self.gen or gen == self.dismissed_gen:
            _LOG.info("screenscan start dropped: stale generation current=%s got=%s", self.gen, gen)
            self._end_in_flight(gen)
            return GLib.SOURCE_REMOVE
        _LOG.info("screenscan start dispatched")
        threading.Thread(target=self.run_screen_scan, args=(gen, t0), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _deliver_screen_scan(self, gen, result, output, t0, timings=None, debug_dir=None,
                             frame_size=None):
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        if gen == self.dismissed_gen:
            # Esc landed while the scan was in flight: the panel must stay
            # dismissed instead of popping back open with the late result.
            _LOG.info("result dropped: generation %s was dismissed", gen)
            return GLib.SOURCE_REMOVE
        if self.scan_ui is not None:
            self.scan_ui.show_result(result, output, t0, timings, frame_size=frame_size)
            press_to_paint_ms = round((time.monotonic() - t0) * 1000.0, 1)
            _LOG.info("screenscan press-to-paint=%.0fms", press_to_paint_ms)
            if debug_dir is not None:
                screen_scan.update_debug_manifest(
                    debug_dir, press_to_paint_ms=press_to_paint_ms
                )
        return GLib.SOURCE_REMOVE

    def _refresh_panel_visible(self) -> None:
        scan_visible = bool(self.scan_ui is not None and self.scan_ui.is_visible())
        price_visible = self.price_check.is_visible()
        self.desktop.set_panel_visible(scan_visible or price_visible)

    def _begin_in_flight(self) -> None:
        with self._press_lock:
            self.in_flight_gen = self.gen
            self.in_flight_since = time.monotonic()

    def _end_in_flight(self, gen: int) -> None:
        # Only the owning generation may clear its in-flight state; a stale
        # worker must not clobber a newer request's flag or replay queue.
        with self._press_lock:
            if self.in_flight_gen != gen:
                return
            self.in_flight_gen = None
            self.in_flight_since = 0.0
            pending = self.pending_press
            self.pending_press = None
        if pending is not None:
            shortcut_id, pressed_at = pending
            if time.monotonic() - pressed_at < 12.0:
                _LOG.info("replaying queued press: %s", shortcut_id)
                GLib.idle_add(self._replay_press, shortcut_id)

    def _replay_press(self, shortcut_id):
        self.on_activated(shortcut_id)
        return GLib.SOURCE_REMOVE

    def on_activated(self, shortcut_id):
        _LOG.info(
            "shortcut received: %s in_flight=%s panel_visible=%s",
            shortcut_id,
            self.in_flight_gen is not None,
            bool(self.scan_ui is not None and self.scan_ui.is_panel_visible()),
        )
        if shortcut_id == "panel-close":
            # Esc: close the panel from any focus state, ahead of the
            # in-flight / focus guards (those gate price lookups, not close).
            self.dismissed_gen = self.gen
            if self.scan_ui is not None:
                self.scan_ui.dismiss()
            self.price_check.hide()
            return
        if self.scan_ui is None:
            return
        if self.in_flight_gen is not None:
            age = time.monotonic() - self.in_flight_since if self.in_flight_since else 0.0
            if age < 15.0:
                # Never silently eat a press: remember the latest one and
                # replay it when the in-flight request finishes.
                with self._press_lock:
                    self.pending_press = (shortcut_id, time.monotonic())
                _LOG.info("shortcut queued: %s (in flight %.1fs)", shortcut_id, age)
                return
            _LOG.warning("stale in-flight request reset after %.1fs", age)
            self._end_in_flight(self.in_flight_gen)
        # Focus guard applies to price-check only: it injects Ctrl+C into the
        # focused window. screen-scan just screenshots the monitor — requiring
        # game FOCUS silently ate presses whenever the panel held focus (the
        # timer instrumentation caught 3 presses -> 1 scan, 2026-06-11).
        # The dynamic Alt+X bind already guarantees a game window exists.
        if (
            shortcut_id != "unique-scan"
            and not self.desktop.is_game_focused()
        ):
            return
        # New lookup supersedes any in-flight requery still in the brain.
        self.gen += 1
        self.dismissed_gen = None
        self._begin_in_flight()
        # Snapshot our overlay visibility before hiding anything: the capture
        # settle delay below is needed exactly when something of ours was on
        # screen at press time.
        overlay_was_visible = (
            self.scan_ui.is_visible() or self.price_check.is_visible()
        )
        self.scan_ui.hide_badges()  # stale badges must not outlive their screen
        if shortcut_id == "unique-scan":
            # Screen capture sees compositor contents. Hide our own panel first
            # so repeat scans don't match stale Kwaystone item cards. The
            # settle delay is only needed when one of our windows was actually
            # visible; a fresh press starts capturing immediately.
            if self.scan_ui.is_panel_visible():
                self.scan_ui.dismiss()
            if self.price_check.is_visible():
                self.price_check.hide()
            # t0 = hotkey arrival in poed: the user's perceived latency clock
            # (xdph -> portal -> us is upstream of this and unmeasurable here).
            if overlay_was_visible:
                GLib.timeout_add(120, self._start_screen_scan, self.gen, time.monotonic())
            else:
                self._start_screen_scan(self.gen, time.monotonic())
        else:
            threading.Thread(
                target=self._run_price_check,
                args=(self.gen,),
                daemon=True,
            ).start()

    def _run_price_check(self, gen: int) -> None:
        try:
            self.price_check.run(gen, self._is_current_gen)
        finally:
            self._end_in_flight(gen)

    def run_screen_scan(self, gen, t0):
        try:
            self.price_check.sync_config()
            engine = screen_scan.run(self.brain, self.desktop, self.cfg)
            press_to_result_ms = round((time.monotonic() - t0) * 1000.0, 1)
            _LOG.info(
                "screenscan: scanner=%s rows=%.2fs capture+scan=%.2fs press-to-result=%.0fms",
                engine.result.scanner_id,
                engine.rows_elapsed,
                engine.scan_elapsed,
                press_to_result_ms,
            )
            if engine.debug_dir is not None:
                screen_scan.update_debug_manifest(
                    engine.debug_dir, press_to_result_ms=press_to_result_ms
                )
            GLib.idle_add(
                self._deliver_screen_scan,
                gen,
                engine.result,
                engine.output,
                t0,
                engine.timings,
                engine.debug_dir,
                engine.frame_size,
            )
        except Exception as e:
            # Never let a press vanish silently: any scanner exception type
            # (cv2.error, KeyError, ...) becomes a visible error panel.
            GLib.idle_add(self._deliver_scan_error, gen, f"{type(e).__name__}: {e}")
        finally:
            self._end_in_flight(gen)

    def on_portal_error(self, message):
        if getattr(self.desktop, "portal_required", True):
            # Route through _shutdown so debug writes flush and backend
            # state (dynamic binds, scripts) is cleaned up on the fatal path.
            self._shutdown(f"portal error — {message}")
            return
        _LOG.error("portal error (hotkeys degraded): %s", message)
        self._set_hotkey_status(f"Global hotkeys unavailable: {message}")

    def _set_hotkey_status(self, text: str) -> None:
        self._hotkey_status = text
        if self._hotkey_status_label is not None:
            self._hotkey_status_label.set_text(text)
            self._hotkey_status_label.set_visible(bool(text))

    # --- league selector ---------------------------------------------------

    def _on_league_selected(self, dropdown, _pspec) -> None:
        if self._league_guard:
            return
        names = self._league_names or [self.cfg["league"]]
        index = int(dropdown.get_selected())
        if index < 0 or index >= len(names):
            return
        name = names[index]
        if name != self.cfg["league"]:
            self._set_league(name, f"Saved. Tracking {name}.")

    def _set_league(self, name: str, note: str) -> None:
        try:
            config.save_league(None, name)
        except OSError as e:
            if self._league_status is not None:
                self._league_status.set_text(f"Could not save league: {e}")
            return
        self.cfg = config.AppConfig.from_mapping(
            {**self.cfg.as_dict(), "league": name}
        )
        if self._league_status is not None:
            self._league_status.set_text(note)

    def _refresh_league_list(self) -> None:
        if self._league_status is not None:
            self._league_status.set_text("Refreshing leagues…")

        def fetch():
            try:
                result = self.brain.request({"cmd": "leagues"}, timeout=5.0)
                entries = result.get("leagues") or []
                names = [str(e.get("name") or "") for e in entries]
                current = {
                    str(e.get("name") or ""): bool(e.get("current"))
                    for e in entries
                }
                fetched_at = float(result.get("fetchedAt") or 0.0)
            except (RuntimeError, OSError, TimeoutError) as e:
                GLib.idle_add(self._fill_league_list, None, None, 0.0, str(e))
                return
            GLib.idle_add(self._fill_league_list, names, current, fetched_at, None)

        threading.Thread(target=fetch, daemon=True).start()

    def _fill_league_list(self, names, current, fetched_at, error):
        cfg_league = self.cfg["league"]
        note = None
        if error or not names:
            # Brain unreachable: keep the selector usable with just the
            # configured league instead of dying or emptying out.
            names = [cfg_league]
            current = {cfg_league: True}
            note = "league list unavailable (showing current)"
        elif cfg_league not in names:
            # The tracked league ended: follow the newest current league in
            # the same family (_set_league persists and writes the note).
            target = leagues.follow_target(names, current, cfg_league)
            self._set_league(target, f"'{cfg_league}' ended; now tracking '{target}'.")
            cfg_league = target
        elif fetched_at:
            age = max(0, int(time.time() - fetched_at / 1000))
            note = f"{len(names)} active leagues (updated {age}s ago)"
        else:
            note = f"{len(names)} active leagues"
        self._league_names = list(names)
        self._league_guard = True
        try:
            self._league_dd.set_model(Gtk.StringList.new(names))
            self._league_dd.set_selected(names.index(cfg_league))
        finally:
            self._league_guard = False
        if note is not None and self._league_status is not None:
            self._league_status.set_text(note)
        return GLib.SOURCE_REMOVE

    def _show_control_window(self) -> None:
        if self.control_window is not None:
            self.control_window.present()
            self._refresh_league_list()
            return
        win = Gtk.ApplicationWindow(application=self.application)
        win.set_title("Kwaystone")
        win.set_default_size(380, 280)
        win.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        status = Gtk.Label(label="Kwaystone is running", xalign=0.0)
        hotkey_status = Gtk.Label(label=self._hotkey_status, xalign=0.0, wrap=True)
        hotkey_status.add_css_class("warning")
        hotkey_status.set_visible(bool(self._hotkey_status))
        self._hotkey_status_label = hotkey_status

        league_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        league_text = Gtk.Label(label="League", xalign=0.0)
        league_text.set_hexpand(True)
        self._league_dd = Gtk.DropDown.new_from_strings([self.cfg["league"]])
        self._league_dd.set_selected(0)
        self._league_dd.connect("notify::selected", self._on_league_selected)
        league_row.append(league_text)
        league_row.append(self._league_dd)
        self._league_status = Gtk.Label(label="", xalign=0.0)
        self._league_status.add_css_class("dim-label")

        settings_label = Gtk.Label(label="OCR settings require restart", xalign=0.0)
        settings_label.add_css_class("dim-label")

        device_choices = (
            _Choice("Auto", "auto"),
            _Choice("CPU", "cpu"),
            _Choice("CUDA", "cuda"),
        )
        model_choices = (
            _Choice("Auto", "auto"),
            _Choice("Small", "small"),
            _Choice("Medium", "medium"),
        )
        device_dd = self._settings_dropdown(device_choices, self.cfg.ocr_device)
        model_dd = self._settings_dropdown(model_choices, self.cfg.ocr_model_size)
        quantity_dd = self._settings_dropdown(
            model_choices,
            self.cfg.ocr_quantity_model_size,
        )
        box.append(status)
        box.append(hotkey_status)
        box.append(league_row)
        box.append(self._league_status)
        box.append(settings_label)
        box.append(self._settings_row("OCR device", device_dd))
        box.append(self._settings_row("OCR model", model_dd))
        box.append(self._settings_row("Quantity model", quantity_dd))

        hint = Gtk.Label(
            label=(
                "Auto defaults: CUDA only with ≥16 GiB VRAM; OCR model auto "
                "means small; quantity auto follows OCR model, or medium on "
                "large-CUDA when both are auto."
            ),
            xalign=0.0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        box.append(hint)

        save_btn = Gtk.Button(label="Save OCR settings")
        saved = Gtk.Label(label="", xalign=0.0)
        saved.add_css_class("dim-label")

        def on_save(_btn):
            device = self._dropdown_value(device_choices, device_dd)
            model = self._dropdown_value(model_choices, model_dd)
            quantity = self._dropdown_value(model_choices, quantity_dd)
            try:
                config.save_ocr_settings(
                    None,
                    device=device,
                    model_size=model,
                    quantity_model_size=quantity,
                )
                self.cfg = config.AppConfig.from_mapping({
                    **self.cfg.as_dict(),
                    "ocr_device": device,
                    "ocr_model_size": model,
                    "ocr_quantity_model_size": quantity,
                })
            except (OSError, ValueError, SystemExit) as e:
                saved.set_text(f"Could not save settings: {e}")
                return
            saved.set_text("Saved. Restart Kwaystone to apply OCR changes.")

        save_btn.connect("clicked", on_save)

        quit_btn = Gtk.Button(label="Quit Kwaystone")
        quit_btn.connect("clicked", lambda _btn: self._shutdown("Quit button clicked"))

        box.append(save_btn)
        box.append(saved)
        box.append(quit_btn)
        win.set_child(box)
        win.connect("close-request", self._on_control_close)
        self.control_window = win
        win.present()
        GLib.timeout_add(350, self._minimize_control_window)
        self._refresh_league_list()

    def _settings_dropdown(
        self,
        choices: tuple[_Choice, ...],
        current: str,
    ):
        dropdown = Gtk.DropDown.new_from_strings([choice.label for choice in choices])
        values = [choice.value for choice in choices]
        try:
            index = values.index(current)
        except ValueError:
            index = 0
        dropdown.set_selected(index)
        return dropdown

    def _settings_row(self, label: str, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        text = Gtk.Label(label=label, xalign=0.0)
        text.set_hexpand(True)
        widget.set_hexpand(False)
        row.append(text)
        row.append(widget)
        return row

    def _dropdown_value(self, choices: tuple[_Choice, ...], dropdown) -> str:
        index = int(dropdown.get_selected())
        if index < 0 or index >= len(choices):
            return choices[0].value
        return choices[index].value

    def _minimize_control_window(self):
        if self.control_window is not None:
            self.control_window.minimize()
        return GLib.SOURCE_REMOVE

    def _on_control_close(self, *_args):
        self._shutdown("control window closed")
        return True

    def on_activate(self, application):
        if self.scan_ui is not None:
            self._show_control_window()
            return
        application.hold()
        # The brain starts here, not in main(): GApplication single-instance
        # guarantees only the primary instance ever runs this, so duplicate
        # launches can never spawn a competing brain that steals/unlinks the
        # shared socket path from under the running instance. It starts on a
        # worker: the spawn+ping wait (up to ~45s worst case) must not freeze
        # the main loop that SIGINT/portal/desktop setup depends on.
        def start_brain():
            try:
                self.brain.start()
                _LOG.info("brain up: %s", self.brain.request({"cmd": "ping"}))
            except (RuntimeError, OSError, TimeoutError) as e:
                GLib.idle_add(self._brain_start_failed, str(e))
                return
            # Warm scanner services only once the brain is actually up:
            # snapshot rows, icon templates, and the PaddleOCR helper.
            screen_scan.warm(self.brain, self.cfg)

        threading.Thread(target=start_brain, daemon=True).start()
        self._show_control_window()
        self.scan_ui = ScanResultController(
            application, self.cfg, self.positions, self.desktop,
            self._refresh_panel_visible,
        )

        def on_shortcuts_bound():
            self.desktop.on_shortcuts_bound()
            self._set_hotkey_status("")

        self.desktop.start(self.on_activated)
        _LOG.info("desktop backend: %s", self.desktop.name)
        # One startup probe names any missing host tools; call sites degrade
        # silently by design, so this is the only place that reports them.
        from poed.subproc import report_missing_tools

        _TOOLS = {
            "kwin": ("wl-paste", "xdotool"),
            "hyprland": ("hyprctl", "wl-paste", "xdotool", "grim"),
        }
        report_missing_tools(_TOOLS.get(self.desktop.name, ()))
        portal_error = getattr(self.desktop, "portal_error", None)
        if portal_error:
            self._set_hotkey_status(f"Global hotkeys unavailable: {portal_error}")

        def prewarm_price_overlay():
            self.price_check.prewarm()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(prewarm_price_overlay)

        if self.desktop.uses_portal_shortcuts:
            shortcuts = GlobalShortcuts(
                "io.github.kriskruse.waystone", self.on_activated, self.on_portal_error,
                on_bound=on_shortcuts_bound,
                session_token=self.desktop.portal_session_token(),
            )
            self._shortcuts = shortcuts
            shortcuts.bind(self.desktop.portal_shortcuts())

        def on_sigint():
            return self._shutdown("Ctrl+C received")

        def on_sigterm():
            return self._shutdown("SIGTERM received")

        # GLib moved these helpers between the GLib and GLibUnix
        # introspection namespaces. Debian 13's bundled typelib exposes only
        # the legacy GLib location, while newer hosts expose GLibUnix.
        signal_add = getattr(GLibUnix, "signal_add", GLib.unix_signal_add)
        signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint)
        signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_sigterm)

        # Re-warm periodically so the first press after the brain's ~12min
        # market refresh does not pay the template-corpus rebuild (~1-2s)
        # on the hot path. Skips overlapping runs; every rebuild happens on
        # this background thread instead of inside a scan. (The initial warm
        # runs in start_brain after the brain is confirmed up.)
        GLib.timeout_add_seconds(240, self._background_rewarm)

    def _brain_start_failed(self, message):
        _LOG.error("exit: brain failed to start — %s", message)
        self._shutdown(f"brain failed to start — {message}")
        return GLib.SOURCE_REMOVE

    def _background_rewarm(self):
        if not self._rewarm_running:
            def rewarm():
                try:
                    screen_scan.warm(self.brain, self.cfg)
                finally:
                    self._rewarm_running = False

            self._rewarm_running = True
            threading.Thread(target=rewarm, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _shutdown(self, label):
        if self._shutting_down:
            return GLib.SOURCE_REMOVE
        self._shutting_down = True
        _LOG.info("exit: %s, quitting.", label)
        debug_io.flush(timeout=5.0)
        screen_scan.stop()
        if self._shortcuts is not None:
            self._shortcuts.stop()
        self.desktop.stop()
        self.application.quit()
        return GLib.SOURCE_REMOVE


_LOG = logging.getLogger("waystone")


def _brain_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "waystone-brain.sock")
    base = tempfile.mkdtemp(prefix=f"waystone-{os.getuid()}-")
    return os.path.join(base, "brain.sock")


def main():
    GLib.set_application_name("Kwaystone")
    log_mod.setup(debug="--debug" in sys.argv or bool(os.environ.get("WAYSTONE_DEBUG")))
    cfg = config.load()
    config.apply_ocr_environment(cfg)
    # EE2 owns account/session-aware trade UI. A configured POESESSID is still
    # passed through for advanced authenticated trade API use.
    sessid = cfg["poesessid"] or ""
    sock = _brain_socket_path()
    brain = Brain(
        brain_dir=brain_module.resolve_brain_dir(),
        socket_path=sock,
        # POE2_LEAGUE steers the brain's startup warm (it otherwise warms
        # "Standard" regardless of config); per-request league still wins.
        env_extra={
            "POE2_LEAGUE": cfg["league"],
            **({"POE2_SESSID": sessid} if sessid else {}),
        },
    )
    desktop = create_backend(cfg)
    positions = PositionStore()  # per-panel saved positions (XDG state)
    app_obj = None
    try:
        app = Gtk.Application(
            application_id="io.github.kriskruse.waystone",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        app_obj = App(app, cfg, brain, positions, desktop)
        app.connect("activate", app_obj.on_activate)
        app.run(None)
    finally:
        # Every exit path flushes queued debug writes, not only _shutdown:
        # otherwise the final manifest updates are lost with the daemon writer.
        debug_io.flush(timeout=5.0)
        screen_scan.stop()
        brain.stop()
        desktop.stop()
        if not os.environ.get("XDG_RUNTIME_DIR"):
            try:
                os.rmdir(os.path.dirname(sock))
            except OSError:
                pass


if __name__ == "__main__":
    main()
