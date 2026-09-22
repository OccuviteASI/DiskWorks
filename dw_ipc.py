"""The channel between the unprivileged window process and the elevated helper.

The window process listens (named pipe on Windows, Unix socket on Linux) with a
random 32-byte HMAC key; the helper - the same binary started once per session with
`--helper` through UAC (ShellExecute "runas") or pkexec - connects back, proves it
holds the key, and then serves JSON requests one job at a time.  Frames are JSON
objects sent with `send_bytes`.  See ARCHITECTURE.md §1.

Request:  {"id": n, "verb": "...", "args": {...}}
Replies:  {"id": n, "event": "progress"|"log"|"done"|"error", ...}   (any number, last one done/error)
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Client, Listener

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
ACCEPT_TIMEOUT = 180.0        # the user may take a while to answer the UAC / polkit prompt


def helper_command() -> list[str]:
    """How to start this program again: the frozen exe alone, or the interpreter + script."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0].endswith(".py")
                                            else os.path.join(os.path.dirname(os.path.abspath(__file__)), "diskworks.py"))]


def make_address(state_dir: str) -> tuple[str, str]:
    """(address, family) for the listener."""
    token = uuid.uuid4().hex
    if IS_WIN:
        return rf"\\.\pipe\DiskWorks-{token}", "AF_PIPE"
    import tempfile
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = os.path.join(base, f"diskworks-{token}")
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return os.path.join(d, "sock"), "AF_UNIX"


def send(conn, obj: dict) -> None:
    conn.send_bytes(json.dumps(obj).encode("utf-8"))


def recv(conn) -> dict:
    raw = conn.recv_bytes()
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("bad frame")
    return obj


# ----------------------------------------------------------------------------
# Elevation launchers
# ----------------------------------------------------------------------------
def launch_windows(params: list[str]) -> int:
    """Start this program elevated via ShellExecuteExW("runas"); returns the new pid."""
    import ctypes
    import ctypes.wintypes as wt

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR),
                    ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                    ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE)]

    cmd = helper_command()
    file = cmd[0]
    args = [subprocess.list2cmdline([a]) for a in cmd[1:] + params]
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00008000 | 0x00000400   # NOCLOSEPROCESS | NO_CONSOLE | FLAG_NO_UI(errors shown by us)
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = " ".join(args)
    info.lpDirectory = os.path.dirname(file)
    info.nShow = 0  # SW_HIDE
    ctypes.windll.kernel32.GetProcessId.argtypes = [wt.HANDLE]
    ctypes.windll.kernel32.GetProcessId.restype = wt.DWORD
    ctypes.windll.kernel32.CloseHandle.argtypes = [wt.HANDLE]
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.GetLastError()
        if err == 1223:
            raise RuntimeError("The administrator prompt was cancelled.")
        raise RuntimeError(f"Windows refused to start the helper (error {err}).")
    pid = ctypes.windll.kernel32.GetProcessId(info.hProcess)
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return int(pid)


def launch_mac(params: list[str]) -> subprocess.Popen:
    """macOS: run the helper as root through the standard administrator password dialog.
    The helper is backgrounded inside the shell line so osascript returns as soon as the
    prompt is answered; the helper connects back over the socket. TN2065: the auth cache is
    keyed on the exact script text, so the string is built once per session."""
    import shlex
    inner = " ".join(shlex.quote(a) for a in helper_command() + params) + " >/dev/null 2>&1 &"
    esc = inner.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{esc}" with administrator privileges'
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DYLD_", "LD_"))}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)


def launch_linux(params: list[str], key_hex: str) -> subprocess.Popen:
    pk = shutil.which("pkexec")
    if not pk:
        raise RuntimeError("pkexec (polkit) is not installed, so DiskWorks cannot ask for permission. "
                           "Run it by hand as root: sudo " + " ".join(helper_command() + params))
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("No graphical session for the permission prompt. Run as root: sudo " + " ".join(helper_command() + params))
    proc = subprocess.Popen([pk] + helper_command() + params, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    try:
        proc.stdin.write(key_hex + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    return proc


# ----------------------------------------------------------------------------
# Window-process side
# ----------------------------------------------------------------------------
class HelperClient:
    def __init__(self, app):
        self.app = app
        self.state = "absent"
        self.message = "View only. Changing disks needs administrator rights (Unlock)."
        self.hello: dict = {}
        self.conn = None
        self.listener = None
        self.pid: int | None = None
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()          # one request in flight at a time on the wire
        self.send_lock = threading.Lock()
        self.pending: dict[int, queue.Queue] = {}
        self.next_id = 1
        self.key_file: str | None = None

    def status(self) -> dict:
        return {"state": self.state, "message": self.message, "pid": self.pid, "version": self.hello.get("version"),
                "admin": self.hello.get("admin")}

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if self.state in ("starting", "ready"):
            return
        key = secrets.token_bytes(32)
        address, family = make_address(self.app.sd)
        self.listener = Listener(address, family=family, authkey=key)
        params = ["--helper", address, "--parent", str(os.getpid()), "--state", self.app.sd]
        self.state = "starting"
        self.message = "Waiting for the administrator prompt…" if IS_WIN else "Waiting for the password prompt…"
        try:
            if os.environ.get("DISKWORKS_NO_ELEVATE"):
                # Development: run the helper as the same user (raw disk access will fail on Windows).
                self.proc = subprocess.Popen(helper_command() + params, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, text=True)
                self.proc.stdin.write(key.hex() + "\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
                self.pid = self.proc.pid
            elif IS_WIN:
                hd = os.path.join(self.app.sd, "helper")
                os.makedirs(hd, exist_ok=True)
                self.key_file = os.path.join(hd, uuid.uuid4().hex + ".key")
                with open(self.key_file, "w", encoding="ascii") as f:
                    f.write(key.hex())
                params += ["--key-file", self.key_file]
                self.pid = launch_windows(params)
            elif IS_MAC:
                hd = os.path.join(self.app.sd, "helper")
                os.makedirs(hd, mode=0o700, exist_ok=True)
                self.key_file = os.path.join(hd, uuid.uuid4().hex + ".key")
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    f.write(key.hex())
                params += ["--key-file", self.key_file, "--uid", str(os.getuid())]
                self.proc = launch_mac(params)   # osascript; exits once the prompt is answered
                self.pid = None
            else:
                params += ["--uid", str(os.getuid())]
                self.proc = launch_linux(params, key.hex())
                self.pid = self.proc.pid
        except Exception as e:
            self._fail(str(e))
            raise
        self.app.info("Helper requested (%s)" % ("UAC" if IS_WIN else "pkexec"))
        threading.Thread(target=self._accept, daemon=True, name="helper-accept").start()

    def _fail(self, msg: str) -> None:
        self.state = "failed"
        self.message = msg
        self.app.info("Helper failed: " + msg)
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            if self.listener:
                self.listener.close()
        except Exception:
            pass
        self.listener = None
        if self.key_file:
            try:
                os.remove(self.key_file)
            except OSError:
                pass
            self.key_file = None

    def _accept(self) -> None:
        # Listener.accept() has no timeout; watch the clock from a helper thread instead.
        result: dict = {}

        def do_accept():
            try:
                result["conn"] = self.listener.accept()
            except Exception as e:
                result["err"] = e
        t = threading.Thread(target=do_accept, daemon=True)
        t.start()
        deadline = time.time() + ACCEPT_TIMEOUT
        while t.is_alive() and time.time() < deadline:
            t.join(0.25)
            if IS_MAC and self.proc is not None and self.proc.poll() is not None and t.is_alive():
                code = self.proc.returncode
                if code != 0:
                    err = ""
                    try:
                        err = (self.proc.stderr.read() or "").strip()
                    except Exception:
                        pass
                    if "-128" in err or "cancel" in err.lower():
                        self._fail("The administrator prompt was cancelled.")
                    else:
                        self._fail(f"macOS did not start the helper. {err[-300:]}")
                    return
                self.proc = None   # prompt answered; the root helper is now connecting back
                continue
            if self.proc is not None and self.proc.poll() is not None and t.is_alive():
                code = self.proc.returncode
                err = ""
                try:
                    err = (self.proc.stderr.read() or "").strip()
                except Exception:
                    pass
                if code in (126, 127):
                    self._fail("The permission prompt was cancelled.")
                else:
                    self._fail(f"The helper exited with code {code}. {err[:300]}")
                return
        if t.is_alive():
            self._fail("No answer to the permission prompt (timed out). Press Unlock to try again.")
            return
        if "err" in result:
            self._fail(f"The helper could not connect: {result['err']}")
            return
        conn = result["conn"]
        try:
            hello = recv(conn)
        except Exception as e:
            self._fail(f"The helper did not introduce itself: {e}")
            return
        if hello.get("hello", {}).get("version") != self.app_version():
            send(conn, {"verb": "quit"})
            self._fail("The helper is a different DiskWorks version; please restart the app.")
            return
        self.hello = hello["hello"]
        self.conn = conn
        self.state = "ready"
        self.message = "Unlocked for this session (helper pid %s)" % self.hello.get("pid")
        self.app.info(self.message)
        self._cleanup()
        threading.Thread(target=self._reader, daemon=True, name="helper-reader").start()
        try:
            self.app.on_helper_ready()
        except Exception:
            pass

    def app_version(self) -> str:
        import diskworks
        return diskworks.VERSION

    def _reader(self) -> None:
        try:
            while True:
                msg = recv(self.conn)
                q = self.pending.get(msg.get("id"))
                if q is not None:
                    q.put(msg)
                elif msg.get("event") == "log":
                    self.app.info("[helper] " + str(msg.get("msg", "")))
        except Exception:
            pass
        was_ready = self.state == "ready"
        self.state = "absent"
        self.message = "The helper has exited. Press Unlock to start it again." if was_ready else self.message
        self.conn = None
        for q in list(self.pending.values()):
            q.put({"event": "error", "message": "The helper connection was lost."})
        if was_ready:
            self.app.info("Helper connection closed")

    # -- requests -----------------------------------------------------------------
    def request(self, verb: str, args: dict | None = None, on_event=None, timeout: float | None = None) -> dict:
        """Send one request, stream progress to on_event, return the 'done' frame or raise."""
        if self.state != "ready" or self.conn is None:
            raise RuntimeError("Administrator rights are needed for this. Press Unlock first.")
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            q: queue.Queue = queue.Queue()
            self.pending[rid] = q
            try:
                with self.send_lock:
                    send(self.conn, {"id": rid, "verb": verb, "args": args or {}})
                deadline = time.time() + timeout if timeout else None
                while True:
                    try:
                        msg = q.get(timeout=1.0)
                    except queue.Empty:
                        if deadline and time.time() > deadline:
                            raise RuntimeError(f"The helper did not finish '{verb}' in time.")
                        continue
                    ev = msg.get("event")
                    if ev == "done":
                        return msg
                    if ev == "error":
                        raise RuntimeError(msg.get("message") or "The helper reported an error.")
                    if on_event:
                        try:
                            on_event(msg)
                        except Exception:
                            pass
            finally:
                self.pending.pop(rid, None)

    def send_control(self, verb: str, args: dict | None = None) -> None:
        """Fire-and-forget frames that must not wait behind a running job (cancel, quit)."""
        if self.conn is None:
            return
        try:
            with self.send_lock:
                send(self.conn, {"id": 0, "verb": verb, "args": args or {}})
        except Exception:
            pass

    def cancel(self) -> None:
        self.send_control("cancel")

    def quit(self) -> None:
        self.send_control("quit")
        self._cleanup()
