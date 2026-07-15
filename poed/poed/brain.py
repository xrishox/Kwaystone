import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

from poed import log


def resolve_brain_dir() -> str:
    """WAYSTONE_BRAIN_DIR for installed packages (launcher sets it);
    repo-relative default for dev checkouts."""
    env = os.environ.get("WAYSTONE_BRAIN_DIR")
    if env:
        return env
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "brain"))


def launch_command(brain_dir: str) -> list[str]:
    """Bundled brain (release install) when dist/server.mjs exists, tsx dev mode otherwise."""
    if os.path.exists(os.path.join(brain_dir, "dist/server.mjs")):
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node not found on PATH; install nodejs")
        return [node, "dist/server.mjs"]
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError(
            "npx not found on PATH; ensure node/npx is available "
            "(e.g. mise: ~/.local/share/mise/installs/node/*/bin) and run "
            "poed from a shell where PATH includes it"
        )
    return [npx, "tsx", "src/server.ts"]


class Brain:
    """Spawns the Node brain as a child and speaks JSON-lines to it."""

    def __init__(self, brain_dir: str, socket_path: str, env_extra: dict | None = None):
        self.brain_dir, self.socket_path = brain_dir, socket_path
        # Extra env vars merged into the child's environment (e.g. POE2_SESSID).
        # Never logged.
        self.env_extra = env_extra or {}
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self, timeout=15.0):
        # npx -> npm exec -> node(tsx) is a process tree; terminating only the
        # npx wrapper reparents the node child and leaves an orphan brain. Run
        # the child as its own session/process-group leader so stop() can
        # signal the whole group.
        self.proc = subprocess.Popen(
            launch_command(self.brain_dir),
            cwd=self.brain_dir,
            env={**os.environ, "BRAIN_SOCKET": self.socket_path, **self.env_extra},
            start_new_session=True,
            # stderr flows into the shared waystone log (stacks survive there).
            stderr=subprocess.PIPE,
        )
        log.pump_in_thread(self.proc.stderr)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._connect().close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"brain exited rc={self.proc.returncode}")
                time.sleep(0.2)
        raise TimeoutError("brain did not come up")

    def _connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.socket_path)
        return s

    def request(self, msg: dict, timeout=30.0, on_progress=None):
        with self._lock:
            self._id += 1
            msg = {"id": self._id, **msg}
        s = self._connect()
        s.settimeout(timeout)
        try:
            s.sendall((json.dumps(msg) + "\n").encode())
            buf = b""
            while True:
                while b"\n" not in buf:
                    try:
                        chunk = s.recv(65536)
                    except TimeoutError:
                        raise RuntimeError("brain request timed out")
                    if not chunk:
                        raise RuntimeError("brain closed connection")
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                resp = json.loads(line)
                if "progress" in resp:
                    # Stage announcements before the final response. They also
                    # reset the inactivity timeout during slow exchange probes.
                    # A buggy UI callback must not kill the read loop.
                    if on_progress is not None:
                        try:
                            on_progress(resp["progress"])
                        except Exception:
                            pass
                    continue
                break
        finally:
            s.close()
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "unknown brain error"))
        return resp["result"]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
