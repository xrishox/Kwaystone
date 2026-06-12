import os
import sys

_LAYER_SHELL = "/usr/lib/libgtk4-layer-shell.so"
if "libgtk4-layer-shell" not in os.environ.get("LD_PRELOAD", "") and os.path.exists(_LAYER_SHELL):
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{_LAYER_SHELL} {env.get('LD_PRELOAD', '')}".strip()
    os.execve(sys.executable, [sys.executable, "-m", "poed", *sys.argv[1:]], env)

import signal
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gtk, Gio, GLib, GLibUnix  # noqa: E402

from poed import config
from poed import capture
from poed import sessid as sessid_mod
from poed.badges import BadgeLayer
from poed.brain import Brain
from poed import hyprbind
from poed import uniquescan
from poed.hyprbind import EscBind, MultiBindManager
from poed.loginbox import LoginBox
from poed.overlay import OverlayPanel
from poed.portal import GlobalShortcuts
from poed.positions import PositionStore


class App:
    def __init__(self, application, cfg, brain, positions, esc_bind):
        self.application = application
        self.cfg = cfg
        self.brain = brain
        self.positions = positions
        self.esc_bind = esc_bind
        self.panel = None
        self.loginbox = None
        self.badges = None
        self.bind_mgr = None
        self.in_flight = False
        self.last_clipboard = None
        self.gen = 0
        # References kept so GLib/portal objects aren't GC'd while alive.
        self._shortcuts = None
        self._hypr_sock = None

    def _deliver(self, gen, result):
        # Drop results from a superseded lookup: a new on_activated bumps gen,
        # so a slow in-flight requery can't clobber a fresh price view.
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        if result.get("kind") == "currency":
            self.panel.show_currency(result)
        else:
            self.panel.show_price(result)
        return GLib.SOURCE_REMOVE

    def _deliver_error(self, gen, message):
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        self.panel.show_error(message)
        return GLib.SOURCE_REMOVE

    def _progress(self, gen, stage):
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        self.panel.set_loading_stage(stage)
        return GLib.SOURCE_REMOVE

    def _deliver_uniques(self, gen, matches, output, t0):
        if gen != self.gen:
            return GLib.SOURCE_REMOVE
        self.panel.show_uniques(
            matches, self.cfg["unique_min_exalted"],
            elapsed=time.monotonic() - t0,
        )
        if self.badges is not None:
            self.badges.show(matches, self.cfg["unique_min_exalted"], output)
        return GLib.SOURCE_REMOVE

    def run_price(self):
        gen = self.gen
        try:
            text = capture.grab_item_text(self.cfg["game_window_class"])
            if text is None:
                GLib.idle_add(self._deliver_error, gen, "not an item")
                return
            self.last_clipboard = text
            result = self.brain.request(
                {"cmd": "price", "clipboard": text, "league": self.cfg["league"]},
                on_progress=lambda stage: GLib.idle_add(self._progress, gen, stage),
            )
            GLib.idle_add(self._deliver, gen, result)
        except (RuntimeError, OSError, TimeoutError) as e:
            GLib.idle_add(self._deliver_error, gen, str(e))
        finally:
            self.in_flight = False

    def on_requery(self, overrides):
        # Returns True when dispatched, False when dropped — the overlay
        # debounce re-arms on a False so the user's last edit isn't lost.
        if self.in_flight or not self.last_clipboard:
            return False
        self.in_flight = True
        gen = self.gen

        def run_requery():
            try:
                result = self.brain.request(
                    {
                        "cmd": "requery",
                        "clipboard": self.last_clipboard,
                        "league": self.cfg["league"],
                        "overrides": overrides,
                    },
                    on_progress=lambda stage: GLib.idle_add(self._progress, gen, stage),
                )
                GLib.idle_add(self._deliver, gen, result)
            except (RuntimeError, OSError, TimeoutError) as e:
                GLib.idle_add(self._deliver_error, gen, str(e))
            finally:
                self.in_flight = False

        threading.Thread(target=run_requery, daemon=True).start()
        return True

    def on_login(self):
        # Thread: config sid wins, else click-time Firefox auto-detect.
        # The sid VALUE is never printed or logged.
        box = self.loginbox

        def run_login():
            sid = self.cfg["poesessid"] or sessid_mod.autodetect()
            if not sid:
                # pathofexile.com's POESESSID is a browser-session cookie —
                # Firefox keeps it in memory only, never on disk, so
                # autodetect can't see it. Manual paste is the standard
                # workflow (same as EE2/Awakened).
                GLib.idle_add(
                    box.set_status,
                    "no session — paste POESESSID into config.toml "
                    "(F12 on pathofexile.com → Storage → Cookies)",
                )
                return
            GLib.idle_add(box.set_busy)
            try:
                result = self.brain.request({"cmd": "login", "sessionId": sid})
                GLib.idle_add(box.set_logged_in, result.get("name", ""))
            except (RuntimeError, OSError, TimeoutError) as e:
                GLib.idle_add(box.set_anonymous)
                GLib.idle_add(box.set_status, str(e))

        threading.Thread(target=run_login, daemon=True).start()

    def on_logout(self):
        box = self.loginbox

        def run_logout():
            try:
                self.brain.request({"cmd": "logout"})
                GLib.idle_add(box.set_anonymous)
            except (RuntimeError, OSError, TimeoutError) as e:
                GLib.idle_add(box.set_status, str(e))

        threading.Thread(target=run_logout, daemon=True).start()

    def on_visibility(self, visible):
        # Bind/unbind the consuming Esc only while the panel is shown.
        if visible:
            self.esc_bind.show()
        else:
            self.esc_bind.hide()
            # Badges live and die with the panel (Esc dismisses both).
            if self.badges is not None:
                self.badges.hide()

    def on_activated(self, shortcut_id):
        panel = self.panel
        if shortcut_id == "panel-close":
            # Esc: close the panel from any focus state, ahead of the
            # in-flight / focus guards (those gate price lookups, not close).
            if panel is not None:
                panel.hide()
            return
        if panel is None:
            return
        if self.in_flight:
            return
        # Focus guard applies to price-check only: it injects Ctrl+C into the
        # focused window. unique-scan just screenshots the monitor — requiring
        # game FOCUS silently ate presses whenever the panel held focus (the
        # timer instrumentation caught 3 presses -> 1 scan, 2026-06-11).
        # The dynamic Alt+X bind already guarantees a game window exists.
        if shortcut_id != "unique-scan" and not capture.is_game_focused(
            self.cfg["game_window_class"]
        ):
            return
        # New lookup supersedes any in-flight requery still in the brain.
        self.gen += 1
        self.in_flight = True
        panel.show_loading()
        if self.badges is not None:
            self.badges.hide()  # stale badges must not outlive their screen
        if shortcut_id == "unique-scan":
            panel.set_loading_stage("uniquescan")
            # t0 = hotkey arrival in poed: the user's perceived latency clock
            # (xdph -> portal -> us is upstream of this and unmeasurable here).
            threading.Thread(
                target=self.run_unique_scan, args=(time.monotonic(),), daemon=True
            ).start()
        else:
            threading.Thread(target=self.run_price, daemon=True).start()

    def run_unique_scan(self, t0):
        gen = self.gen
        try:
            # First call after a cold icon cache downloads ~1100 icons
            # brain-side — generous timeout; warm calls answer in <1s.
            rows = self.brain.request(
                {"cmd": "uniqueprices", "league": self.cfg["league"]},
                timeout=120.0,
            )
            t_rows = time.monotonic()
            rows = uniquescan.filter_rows(rows, self.cfg["unique_scan_min_price"])
            output = hyprbind.active_game_output()
            if output is None:
                GLib.idle_add(self._deliver_error, gen, "no active monitor found")
                return
            matches = uniquescan.scan_screen(output, rows)
            t_scan = time.monotonic()
            if matches is None:
                GLib.idle_add(self._deliver_error, gen, "screen capture failed")
                return
            print(
                f"uniquescan: rows={t_rows - t0:.2f}s "
                f"capture+match={t_scan - t_rows:.2f}s "
                f"worker-total={t_scan - t0:.2f}s",
                flush=True,
            )
            GLib.idle_add(self._deliver_uniques, gen, matches, output, t0)
        except (RuntimeError, OSError, TimeoutError) as e:
            GLib.idle_add(self._deliver_error, gen, str(e))
        finally:
            self.in_flight = False

    def on_portal_error(self, message):
        print(f"exit: portal error — {message}", flush=True)
        self.application.quit()

    def on_activate(self, application):
        application.hold()
        self.panel = OverlayPanel(
            application, self.cfg, on_requery=self.on_requery,
            on_visibility=self.on_visibility, positions=self.positions,
        )
        loginbox = LoginBox(
            application, self.on_login, self.on_logout, positions=self.positions
        )
        self.loginbox = loginbox
        self.panel.attach_loginbox(loginbox)
        self.badges = BadgeLayer(application)

        def on_shortcuts_bound():
            if self.bind_mgr is not None:
                self.bind_mgr.notify_registered()

        shortcuts = GlobalShortcuts(
            "io.github.kriskruse.waystone", self.on_activated, self.on_portal_error,
            on_bound=on_shortcuts_bound,
        )
        self._shortcuts = shortcuts
        shortcuts.bind([
            ("price-check", "PoE2 price check", self.cfg["hotkey_price"]),
            ("unique-scan", "Scan screen for uniques", "ALT+x"),
            ("panel-close", "Close PoE2 overlay panel", "ESC"),
        ])

        try:
            bind_mgr = MultiBindManager.create(
                self.cfg["game_window_class"],
                [("ALT", "Z", "price-check"), ("ALT", "X", "unique-scan")],
            )
            self.bind_mgr = bind_mgr
            bind_mgr.prime()
            sock = bind_mgr.connect_events()
            self._hypr_sock = sock  # keep a reference so it isn't GC'd

            buf = b""

            def on_hypr_event(fd, _cond, _data):
                nonlocal buf
                try:
                    chunk = sock.recv(4096)
                except BlockingIOError:
                    return GLib.SOURCE_CONTINUE
                if not chunk:
                    print(
                        "hyprbind: event socket closed; dynamic bind disabled until restart",
                        flush=True,
                    )
                    bind_mgr.stop()
                    return GLib.SOURCE_REMOVE
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    bind_mgr.handle_line(line.decode("utf-8", "replace"))
                return GLib.SOURCE_CONTINUE

            GLibUnix.fd_add_full(
                GLib.PRIORITY_DEFAULT,
                sock.fileno(),
                GLib.IOCondition.IN,
                on_hypr_event,
                None,
            )
        except KeyError:
            print(
                "hyprbind: not a Hyprland session, dynamic bind disabled",
                flush=True,
            )

        def on_sigint():
            return self._shutdown("Ctrl+C received")

        def on_sigterm():
            return self._shutdown("SIGTERM received")

        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_sigterm)

        # Warm the unique-scan corpus in the background (brain snapshot +
        # icons + poed templates) so the first Alt+X is as fast as the rest.
        threading.Thread(
            target=uniquescan.warm, args=(self.brain, self.cfg), daemon=True
        ).start()

    def _shutdown(self, label):
        print(f"exit: {label}, quitting.", flush=True)
        if self.bind_mgr is not None:
            self.bind_mgr.stop()
        if self.esc_bind is not None:
            self.esc_bind.stop()
        self.application.quit()
        return GLib.SOURCE_REMOVE


def main():
    cfg = config.load()
    # Click-only login (iteration 5): no startup auto-detect. The configured
    # poesessid is still honored as the initial brain env (explicit opt-in);
    # Firefox cookie auto-detection now happens only on a Login button click.
    sessid = cfg["poesessid"] or ""
    sock = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "poe2-overlay-brain.sock"
    )
    brain = Brain(
        brain_dir=os.path.join(os.path.dirname(__file__), "../../brain"),
        socket_path=sock,
        env_extra={"POE2_SESSID": sessid} if sessid else None,
    )
    brain.start()
    esc_bind = EscBind("panel-close")
    positions = PositionStore()  # per-panel saved positions (XDG state)
    app_obj = None
    try:
        print("brain up:", brain.request({"cmd": "ping"}))

        app = Gtk.Application(
            application_id="io.github.kriskruse.waystone",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        app_obj = App(app, cfg, brain, positions, esc_bind)
        app.connect("activate", app_obj.on_activate)
        app.run(None)
    finally:
        brain.stop()
        if app_obj is not None and app_obj.bind_mgr is not None:
            app_obj.bind_mgr.stop()
        esc_bind.stop()


if __name__ == "__main__":
    main()
