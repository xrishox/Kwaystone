"""Persistent XDG ScreenCast stream for low-latency screen observation."""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
import uuid
from collections.abc import Callable

import gi
import numpy as np

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from poed import config  # noqa: E402

_LOG = logging.getLogger("waystone.screencast")
_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
_REQUEST_IFACE = "org.freedesktop.portal.Request"
_SESSION_IFACE = "org.freedesktop.portal.Session"


def _token() -> str:
    return "waystone_" + uuid.uuid4().hex


def _restore_token_path() -> pathlib.Path:
    return config.state_home() / "waystone" / "screencast-token"


def _read_restore_token() -> str | None:
    try:
        token = _restore_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _write_restore_token(token: str) -> None:
    path = _restore_token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ScreenCast:
    """Own one monitor stream and deliver copied BGR frames at at most 4 Hz."""

    def __init__(
        self,
        on_frame: Callable[[np.ndarray, int, float], None],
        on_ready: Callable[[], None],
        on_error: Callable[[str], None],
        *,
        bus=None,
    ):
        self._on_frame = on_frame
        self._on_ready = on_ready
        self._on_error = on_error
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        unique = self._bus.get_unique_name()
        self._sender = unique.lstrip(":").replace(".", "_")
        self._session_handle: str | None = None
        self._pipeline = None
        self._pipewire_fd: int | None = None
        self._sequence = 0
        self._stopped = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._stopped:
                raise RuntimeError("ScreenCast cannot be restarted")
        self._create_session()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            pipeline = self._pipeline
            self._pipeline = None
            descriptor = self._pipewire_fd
            self._pipewire_fd = None
            session = self._session_handle
            self._session_handle = None
        if pipeline is not None:
            try:
                from gi.repository import Gst  # noqa: PLC0415

                pipeline.set_state(Gst.State.NULL)
            except (ImportError, GLib.Error, AttributeError):
                _LOG.exception("failed to stop ScreenCast pipeline")
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if session is not None:
            self._bus.call(
                _PORTAL_BUS,
                session,
                _SESSION_IFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
                None,
                None,
            )

    def _request_path(self, token: str) -> str:
        return f"{_PORTAL_PATH}/request/{self._sender}/{token}"

    def _call_request(self, method: str, params, token: str, callback) -> None:
        expected = self._request_path(token)
        subscription: dict[str, int] = {}

        def unsubscribe() -> None:
            identifier = subscription.pop("id", None)
            if identifier is not None:
                self._bus.signal_unsubscribe(identifier)

        def on_response(_conn, _sender, _path, _iface, _signal, variant, _data):
            unsubscribe()
            try:
                code, results = variant.unpack()
            except (TypeError, ValueError):
                code, results = 1, {}
            callback(int(code), results)

        def on_call(conn, result, _data):
            try:
                returned = conn.call_finish(result).unpack()[0]
            except GLib.Error as error:
                unsubscribe()
                self._fail(f"{method} call failed: {error.message}")
                return
            if returned != expected:
                unsubscribe()
                subscription["id"] = self._bus.signal_subscribe(
                    _PORTAL_BUS,
                    _REQUEST_IFACE,
                    "Response",
                    returned,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    on_response,
                    None,
                )

        subscription["id"] = self._bus.signal_subscribe(
            _PORTAL_BUS,
            _REQUEST_IFACE,
            "Response",
            expected,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        self._bus.call(
            _PORTAL_BUS,
            _PORTAL_PATH,
            _SCREENCAST_IFACE,
            method,
            params,
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            30000,
            None,
            on_call,
            None,
        )

    def _create_session(self) -> None:
        handle_token = _token()
        options = {
            "handle_token": GLib.Variant("s", handle_token),
            "session_handle_token": GLib.Variant("s", _token()),
        }
        self._call_request(
            "CreateSession",
            GLib.Variant("(a{sv})", (options,)),
            handle_token,
            self._session_created,
        )

    def _session_created(self, code: int, results: dict) -> None:
        if code != 0 or self._is_stopped():
            self._fail(f"ScreenCast session creation failed (code={code})")
            return
        session = results.get("session_handle")
        if not isinstance(session, str) or not session:
            self._fail("ScreenCast portal returned no session")
            return
        self._session_handle = session
        handle_token = _token()
        options = {
            "handle_token": GLib.Variant("s", handle_token),
            "types": GLib.Variant("u", 1),  # monitor
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", 1),  # hidden
            "persist_mode": GLib.Variant("u", 2),  # persist until revoked
        }
        restore_token = _read_restore_token()
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)
        self._call_request(
            "SelectSources",
            GLib.Variant("(oa{sv})", (session, options)),
            handle_token,
            self._sources_selected,
        )

    def _sources_selected(self, code: int, _results: dict) -> None:
        if code != 0 or self._is_stopped():
            self._fail(f"ScreenCast source selection failed (code={code})")
            return
        handle_token = _token()
        options = {"handle_token": GLib.Variant("s", handle_token)}
        self._call_request(
            "Start",
            GLib.Variant("(osa{sv})", (self._session_handle, "", options)),
            handle_token,
            self._started,
        )

    def _started(self, code: int, results: dict) -> None:
        if code != 0 or self._is_stopped():
            self._fail(f"ScreenCast start failed (code={code})")
            return
        streams = results.get("streams") or []
        if len(streams) != 1:
            self._fail("ScreenCast portal did not return exactly one monitor")
            return
        try:
            node_id = int(streams[0][0])
        except (IndexError, TypeError, ValueError):
            self._fail("ScreenCast portal returned an invalid PipeWire stream")
            return
        restore_token = results.get("restore_token")
        if isinstance(restore_token, str) and restore_token:
            try:
                _write_restore_token(restore_token)
            except OSError as error:
                _LOG.warning("could not persist ScreenCast restore token: %s", error)
        threading.Thread(
            target=self._open_pipewire,
            args=(node_id,),
            name="waystone-screencast-open",
            daemon=True,
        ).start()

    def _open_pipewire(self, node_id: int) -> None:
        try:
            result, descriptors = self._bus.call_with_unix_fd_list_sync(
                _PORTAL_BUS,
                _PORTAL_PATH,
                _SCREENCAST_IFACE,
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self._session_handle, {})),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                30000,
                None,
                None,
            )
            handle = int(result.unpack()[0])
            descriptor = descriptors.get(handle)
        except (GLib.Error, IndexError, TypeError, ValueError) as error:
            GLib.idle_add(self._fail, f"could not open PipeWire stream: {error}")
            return
        GLib.idle_add(self._start_pipeline, descriptor, node_id)

    def _start_pipeline(self, descriptor: int, node_id: int):
        if self._is_stopped():
            os.close(descriptor)
            return GLib.SOURCE_REMOVE
        try:
            gi.require_version("Gst", "1.0")
            gi.require_version("GstApp", "1.0")
            from gi.repository import Gst  # noqa: PLC0415

            Gst.init(None)
            pipeline = Gst.parse_launch(
                "pipewiresrc name=source do-timestamp=true ! "
                "queue max-size-buffers=1 leaky=downstream ! "
                "videorate drop-only=true max-rate=4 ! videoconvert ! "
                "video/x-raw,format=BGR ! "
                "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
            )
            source = pipeline.get_by_name("source")
            source.set_property("fd", descriptor)
            source.set_property("path", str(node_id))
            sink = pipeline.get_by_name("sink")
            sink.connect("new-sample", self._new_sample)
            change = pipeline.set_state(Gst.State.PLAYING)
            if change == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("GStreamer rejected the PipeWire stream")
        except (GLib.Error, RuntimeError, AttributeError) as error:
            os.close(descriptor)
            self._fail(f"could not start live screen stream: {error}")
            return GLib.SOURCE_REMOVE
        self._pipewire_fd = descriptor
        self._pipeline = pipeline
        self._on_ready()
        return GLib.SOURCE_REMOVE

    def _new_sample(self, sink):
        from gi.repository import Gst  # noqa: PLC0415

        sample = sink.emit("pull-sample")
        if sample is None or self._is_stopped():
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width = int(caps.get_value("width"))
        height = int(caps.get_value("height"))
        buffer = sample.get_buffer()
        success, mapped = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK
        try:
            row_stride = len(mapped.data) // max(1, height)
            if row_stride < width * 3:
                return Gst.FlowReturn.OK
            rows = np.frombuffer(mapped.data, dtype=np.uint8).reshape(height, row_stride)
            frame = rows[:, : width * 3].reshape(height, width, 3).copy()
        finally:
            buffer.unmap(mapped)
        with self._lock:
            if self._stopped:
                return Gst.FlowReturn.OK
            self._sequence += 1
            sequence = self._sequence
        try:
            self._on_frame(frame, sequence, time.monotonic())
        except Exception:  # noqa: BLE001 - never unwind into GStreamer's thread
            _LOG.exception("live screen frame callback failed")
        return Gst.FlowReturn.OK

    def _is_stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def _fail(self, message: str):
        if not self._is_stopped():
            _LOG.warning("%s", message)
            self._on_error(message)
        return GLib.SOURCE_REMOVE
