#!/usr/bin/env python3
"""Spike: portal GlobalShortcuts under xdg-desktop-portal-hyprland (xdph).

De-risks the riskiest assumption of POE2-Overlay: that a global hotkey
(e.g. SHIFT+space for price-check) can be registered on Wayland/Hyprland
via the org.freedesktop.portal.GlobalShortcuts portal interface.

Throwaway diagnostic script. Run with:
    timeout 15 python spikes/spike_portal.py

Headless-verifiable: CreateSession + BindShortcuts response codes,
ListShortcuts output. NOT verifiable headless: the actual Activated
signal (requires a human to physically press SHIFT+space).

See the ## Findings block at the bottom for observed behaviour.
"""

import signal
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gio, GLib, GLibUnix  # noqa: E402

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
GS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# Shortcut we want to bind for the price-check action.
SHORTCUT_ID = "price-check"
SHORTCUT_TRIGGER = "SHIFT+space"
SHORTCUT_DESCRIPTION = "POE2 price check"

# Unique-ish tokens for this run. Real code should make these random.
HANDLE_TOKEN = "poe2_spike_handle"
SESSION_TOKEN = "poe2_spike_session"


def log(step, msg):
    print(f"[{step}] {msg}", flush=True)


class PortalSpike:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Unique name like ":1.42" -> token sender fragment "1_42".
        unique = self.bus.get_unique_name()
        self.sender = unique.lstrip(":").replace(".", "_")
        log("bus", f"connected, unique name = {unique}, sender token = {self.sender}")
        self.session_handle = None
        self.loop = GLib.MainLoop()

    # --- Request/Response plumbing ----------------------------------------

    def request_path_for(self, token):
        """Construct the predicted Request object path for a token.

        Portal methods that take a handle_token return a Request object whose
        Response signal fires at /org/freedesktop/portal/desktop/request/
        <SENDER>/<TOKEN>. We subscribe to this path BEFORE issuing the call so
        we can never miss the (potentially immediate) Response signal.
        """
        return f"{PORTAL_PATH}/request/{self.sender}/{token}"

    def call_with_request(self, method, params, token, on_response):
        """Call a GlobalShortcuts method that follows the Request pattern.

        Subscribes to the predicted Request path first, then invokes the
        method, then verifies the returned object path matches the prediction.
        on_response(code, results_dict) is invoked when the Response arrives.
        """
        expected = self.request_path_for(token)
        log(method, f"subscribing to expected request path: {expected}")

        sub_id = {"id": None}

        def on_signal(conn, sender, path, iface, sig, params_variant, user_data):
            code, results = params_variant.unpack()
            log(method, f"Response signal: code={code} results={results}")
            if sub_id["id"] is not None:
                conn.signal_unsubscribe(sub_id["id"])
            on_response(code, results)

        sub_id["id"] = self.bus.signal_subscribe(
            PORTAL_BUS,
            REQUEST_IFACE,
            "Response",
            expected,
            None,
            Gio.DBusSignalFlags.NONE,
            on_signal,
            None,
        )

        log(method, f"calling with params: {params}")
        ret = self.bus.call_sync(
            PORTAL_BUS,
            PORTAL_PATH,
            GS_IFACE,
            method,
            params,
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        returned_path = ret.unpack()[0]
        log(method, f"returned request handle: {returned_path}")
        if returned_path != expected:
            log(
                method,
                f"WARNING: returned path != expected. "
                f"expected={expected} got={returned_path}. "
                "Re-subscribing on actual path.",
            )
            if sub_id["id"] is not None:
                self.bus.signal_unsubscribe(sub_id["id"])
            sub_id["id"] = self.bus.signal_subscribe(
                PORTAL_BUS,
                REQUEST_IFACE,
                "Response",
                returned_path,
                None,
                Gio.DBusSignalFlags.NONE,
                on_signal,
                None,
            )

    # --- Steps ------------------------------------------------------------

    def create_session(self):
        log("CreateSession", "starting")
        options = {
            "handle_token": GLib.Variant("s", HANDLE_TOKEN),
            "session_handle_token": GLib.Variant("s", SESSION_TOKEN),
        }
        params = GLib.Variant("(a{sv})", (options,))
        self.call_with_request(
            "CreateSession", params, HANDLE_TOKEN, self.on_session_created
        )

    def on_session_created(self, code, results):
        if code != 0:
            log("CreateSession", f"FAILED with response code {code}. Aborting.")
            self.loop.quit()
            sys.exit(1)
        self.session_handle = results.get("session_handle")
        log("CreateSession", f"OK. session_handle = {self.session_handle}")
        self.bind_shortcuts()

    def bind_shortcuts(self):
        log("BindShortcuts", "starting")
        shortcut_meta = {
            "description": GLib.Variant("s", SHORTCUT_DESCRIPTION),
            "preferred_trigger": GLib.Variant("s", SHORTCUT_TRIGGER),
        }
        shortcuts = [(SHORTCUT_ID, shortcut_meta)]
        options = {
            "handle_token": GLib.Variant("s", HANDLE_TOKEN + "_bind"),
        }
        params = GLib.Variant(
            "(oa(sa{sv})sa{sv})",
            (self.session_handle, shortcuts, "", options),
        )
        self.call_with_request(
            "BindShortcuts", params, HANDLE_TOKEN + "_bind", self.on_shortcuts_bound
        )

    def on_shortcuts_bound(self, code, results):
        if code != 0:
            log("BindShortcuts", f"FAILED with response code {code}. Aborting.")
            self.loop.quit()
            sys.exit(1)
        bound = results.get("shortcuts")
        log("BindShortcuts", f"OK. bound shortcuts = {bound}")
        self.list_shortcuts()

    def list_shortcuts(self):
        log("ListShortcuts", "starting")
        options = {"handle_token": GLib.Variant("s", HANDLE_TOKEN + "_list")}
        params = GLib.Variant("(oa{sv})", (self.session_handle, options))
        self.call_with_request(
            "ListShortcuts", params, HANDLE_TOKEN + "_list", self.on_shortcuts_listed
        )

    def on_shortcuts_listed(self, code, results):
        if code != 0:
            log("ListShortcuts", f"FAILED with response code {code}.")
        else:
            log("ListShortcuts", f"OK. shortcuts = {results.get('shortcuts')}")
        log(
            "ready",
            "Setup complete. Waiting for activations. "
            f"Press {SHORTCUT_TRIGGER} to trigger '{SHORTCUT_ID}'. Ctrl+C to exit.",
        )

    # --- Activation signal ------------------------------------------------

    def subscribe_activated(self):
        def on_activated(conn, sender, path, iface, sig, params_variant, user_data):
            data = params_variant.unpack()
            log("Activated", f"shortcut activated! params = {data}")

        def on_deactivated(conn, sender, path, iface, sig, params_variant, user_data):
            data = params_variant.unpack()
            log("Deactivated", f"shortcut deactivated. params = {data}")

        self.bus.signal_subscribe(
            PORTAL_BUS, GS_IFACE, "Activated", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE, on_activated, None,
        )
        self.bus.signal_subscribe(
            PORTAL_BUS, GS_IFACE, "Deactivated", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE, on_deactivated, None,
        )
        log("Activated", "subscribed to Activated/Deactivated signals")

    # --- Run --------------------------------------------------------------

    def run(self):
        # Report the portal version for diagnostics.
        try:
            ver = self.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (GS_IFACE, "version")),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE, -1, None,
            )
            log("version", f"GlobalShortcuts version = {ver.unpack()[0]}")
        except GLib.Error as exc:
            log("version", f"could not read version: {exc}")

        self.subscribe_activated()
        self.create_session()

        # Clean Ctrl+C handling.
        GLibUnix.signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_sigint
        )
        self.loop.run()

    def _on_sigint(self):
        log("exit", "Ctrl+C received, quitting cleanly.")
        self.loop.quit()
        return GLib.SOURCE_REMOVE


def main():
    try:
        spike = PortalSpike()
        spike.run()
    except GLib.Error as exc:
        log("FATAL", f"DBus error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()


# =============================================================================
# ## Findings
# =============================================================================
#
# VERDICT: WORKS. GlobalShortcuts is fully supported by xdph and the full
# CreateSession -> BindShortcuts -> ListShortcuts flow returns response code 0.
# This de-risks the project's riskiest assumption.
#
# Environment:
#   - Arch, Hyprland, xdg-desktop-portal-hyprland (xdph) 1.3.12
#   - Python 3.14.5, python-gobject (gi) 3.56.3
#   - GlobalShortcuts portal interface version = 1
#
# Confirmed the interface is exposed:
#   busctl --user introspect org.freedesktop.portal.Desktop \
#       /org/freedesktop/portal/desktop \
#       org.freedesktop.portal.GlobalShortcuts
#   -> methods CreateSession / BindShortcuts / ListShortcuts,
#      signals Activated / Deactivated / ShortcutsChanged, property version=1.
#   /usr/share/xdg-desktop-portal/portals/hyprland.portal lists
#   org.freedesktop.impl.portal.GlobalShortcuts under Interfaces=, so the
#   hyprland backend (not just the generic frontend) implements it.
#
# DBus call order that worked (each step is a portal Request -> Response):
#   1. Properties.Get(GlobalShortcuts, "version")  -> 1
#   2. signal_subscribe Activated/Deactivated on the GlobalShortcuts iface
#      at /org/freedesktop/portal/desktop
#   3. CreateSession({handle_token, session_handle_token})
#        returns request handle; Response signal:
#        code=0, results={'session_handle':
#          '/org/freedesktop/portal/desktop/session/<SENDER>/<SESSION_TOKEN>'}
#   4. BindShortcuts(session_handle,
#        [('price-check', {description, preferred_trigger='SHIFT+space'})],
#        parent_window='', {handle_token})
#        Response: code=0,
#        results={'shortcuts': [('price-check',
#          {'trigger_description': '', 'description': 'POE2 price check'})]}
#   5. ListShortcuts(session_handle, {handle_token})
#        Response: code=0. See "ListShortcuts quirk" below for the result.
#
# Response codes: all 0 (success) for CreateSession, BindShortcuts,
# ListShortcuts.
#
# ListShortcuts quirk (xdph 1.3.12):
#   - On the FIRST successful run, ListShortcuts returned the bound shortcut:
#       {'shortcuts': [('price-check',
#         {'trigger_description': '', 'description': 'POE2 price check'})]}
#   - On EVERY subsequent run (same boot/session), BindShortcuts still
#     reports code=0 and echoes the shortcut in ITS OWN Response, but
#     ListShortcuts consistently returns {'shortcuts': []}.
#   - So ListShortcuts is NOT a reliable confirmation of a binding under
#     xdph 1.3.12. Treat the BindShortcuts Response (code=0 + echoed
#     shortcuts) as the source of truth in Task 8; do not gate logic on
#     ListShortcuts being non-empty.
#
# Request/Response pattern:
#   - The predicted request path
#     /org/freedesktop/portal/desktop/request/<SENDER>/<TOKEN>
#     (SENDER = unique bus name with leading ':' stripped and '.' -> '_',
#      e.g. ':1.229' -> '1_229') matched the path returned by every method
#     call EXACTLY. The defensive re-subscribe branch never fired. Still worth
#     keeping the subscribe-before-call ordering to avoid the race in
#     production, but in practice xdph returns the predictable path.
#
# Approval dialog: NONE observed. All three calls completed within
#   milliseconds in a fully headless run (no GUI interaction, no human
#   present). xdph did NOT pop an approval/permission dialog for binding the
#   shortcut. (Contrast: GNOME's portal shows a "set shortcuts" dialog.) So on
#   Hyprland the bind is silent/automatic.
#
# Surprises / gotchas:
#   - Gio DBus signal callbacks receive 7 positional args, INCLUDING the
#     trailing user_data passed to signal_subscribe. Initial 6-arg callbacks
#     raised "takes 6 positional arguments but 7 were given". Fixed.
#   - 'trigger_description' comes back EMPTY ('') from xdph, even though we
#     requested preferred_trigger='SHIFT+space'. The bind still succeeds and
#     the shortcut id is registered. On Hyprland the actual key binding for a
#     portal global shortcut is configured by the COMPOSITOR/USER (e.g. via
#     hyprland.conf `bind` to the portal), not honoured from preferred_trigger
#     by xdph 1.3.12. preferred_trigger appears to be advisory only here.
#   - GLib.unix_signal_add is deprecated on gi 3.56; used GLibUnix.signal_add.
#   - timeout(1) exits 124 when it kills the idle MainLoop -- that is expected
#     for the headless run, NOT a failure.
#
# NOT verifiable headless (requires human):
#   - Actually pressing SHIFT+space and seeing an [Activated] line. Because
#     trigger_description is empty, the human likely must wire the trigger in
#     Hyprland config to point at the portal shortcut, OR investigate whether
#     xdph exposes a config UI. This needs physical verification before Task 8.
#
# Implications for Task 8 (poed/poed/portal.py):
#   - Reuse this exact call order and Request/Response plumbing.
#   - Make handle_token / session_handle_token random per run (this spike used
#     fixed tokens for readability).
#   - Do not rely on preferred_trigger being applied; plan for the user to bind
#     the trigger in Hyprland, and listen for Activated to fire the price check.
#   - Do not rely on ListShortcuts to confirm a binding (returns [] after the
#     first run). Use the BindShortcuts Response as confirmation.
#   - PITFALL: do NOT call sys.exit() inside GLib signal callbacks -- GLib's
#     dispatcher swallows SystemExit and the process exits 0. Set a flag, quit
#     the loop, exit from main().
#   - PITFALL: don't share a mutable subscription-id cell between subscribe and
#     re-subscribe paths -- late Response after re-subscribe unsubscribes the
#     wrong handler. Capture the id in a local at subscribe time. Also: portal
#     handle tokens must match [a-zA-Z0-9_]+ -- random tokens via uuid4 need
#     dashes replaced with underscores.
