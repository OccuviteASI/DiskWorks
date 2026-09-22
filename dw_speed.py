"""Disk speed test (Blackmagic Disk Speed Test style): sequential write then read of a
temporary file on a mounted volume with the OS cache bypassed, repeated until stopped,
reporting MB/s live.  Runs in the window process — no elevation, no raw devices.
"""
from __future__ import annotations

import mmap
import os
import sys
import threading
import time

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MB = 1_000_000            # decimal megabytes, like the tools people compare against
TEST_NAME = ".diskworks-speedtest.tmp"


# ----------------------------------------------------------------------------
# Unbuffered file I/O per platform
# ----------------------------------------------------------------------------
class UnbufferedFile:
    """A file opened so that reads and writes go to the device, not the OS cache
    (Windows FILE_FLAG_NO_BUFFERING|WRITE_THROUGH, Linux O_DIRECT, macOS F_NOCACHE).
    Buffers must be sector-aligned: callers pass mmap buffers (page aligned)."""

    def __init__(self, path: str, write: bool):
        self.path = path
        self.h = None
        self.fd = None
        if IS_WIN:
            import ctypes
            import ctypes.wintypes as wt
            self.k32 = ctypes.windll.kernel32
            self.k32.CreateFileW.restype = wt.HANDLE
            self.k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
            self.k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
            self.k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
            self.k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
            self.k32.CloseHandle.argtypes = [wt.HANDLE]
            GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
            CREATE_ALWAYS, OPEN_EXISTING = 2, 3
            flags = 0x20000000 | 0x80000000 | 0x80   # NO_BUFFERING | WRITE_THROUGH | ATTRIBUTE_NORMAL
            # the read pass must open the file that was just written, not truncate it
            self.h = self.k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, CREATE_ALWAYS if write else OPEN_EXISTING, flags, None)
            if not self.h or self.h == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.GetLastError(), ctypes.FormatError(ctypes.GetLastError()).strip())
            self._wt = wt
            self._ctypes = ctypes
        else:
            flags = (os.O_RDWR | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
            if not IS_MAC and hasattr(os, "O_DIRECT"):
                flags |= os.O_DIRECT
            self.fd = os.open(path, flags, 0o600)
            if IS_MAC:
                import fcntl
                F_NOCACHE = 48
                fcntl.fcntl(self.fd, F_NOCACHE, 1)

    def seek0(self) -> None:
        if IS_WIN:
            self.k32.SetFilePointerEx(self.h, 0, None, 0)
        else:
            os.lseek(self.fd, 0, os.SEEK_SET)

    def write(self, mv) -> int:
        if IS_WIN:
            got = self._wt.DWORD(0)
            addr = self._ctypes.addressof(self._ctypes.c_char.from_buffer(mv))
            if not self.k32.WriteFile(self.h, self._ctypes.c_void_p(addr), len(mv), self._ctypes.byref(got), None):
                raise OSError(self._ctypes.GetLastError(), self._ctypes.FormatError(self._ctypes.GetLastError()).strip())
            return got.value
        return os.write(self.fd, mv)

    def read(self, mv) -> int:
        if IS_WIN:
            got = self._wt.DWORD(0)
            addr = self._ctypes.addressof(self._ctypes.c_char.from_buffer(mv))
            if not self.k32.ReadFile(self.h, self._ctypes.c_void_p(addr), len(mv), self._ctypes.byref(got), None):
                raise OSError(self._ctypes.GetLastError(), self._ctypes.FormatError(self._ctypes.GetLastError()).strip())
            return got.value
        return os.readv(self.fd, [mv])

    def flush(self) -> None:
        if IS_WIN:
            self.k32.FlushFileBuffers(self.h)
        else:
            try:
                if IS_MAC:
                    import fcntl
                    fcntl.fcntl(self.fd, 51, 0)   # F_FULLFSYNC: really on the platter / flash
                else:
                    os.fsync(self.fd)
            except OSError:
                pass

    def close(self) -> None:
        if IS_WIN and self.h:
            self.k32.CloseHandle(self.h)
            self.h = None
        elif self.fd is not None:
            os.close(self.fd)
            self.fd = None


def same_volume(a: str, b: str) -> bool:
    """True when two directories live on the same volume (drive letter on Windows, st_dev elsewhere)."""
    try:
        if IS_WIN:
            da, db = os.path.splitdrive(os.path.abspath(a))[0], os.path.splitdrive(os.path.abspath(b))[0]
            if da and db:
                return da.lower() == db.lower()
            return os.path.normcase(os.path.abspath(a)).startswith(os.path.normcase(os.path.abspath(b)))
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def writable_folder_on(root: str) -> str:
    """A folder on the chosen volume where this user may create the test file.  The root of
    the Windows system drive refuses a normal user (Access is denied), so the user's temp and
    home folders are tried first when they live on that volume, then the root itself, then a
    DiskWorks folder under the root."""
    import tempfile
    cands = []
    for c in (tempfile.gettempdir(), os.path.expanduser("~"), root, os.path.join(root, "DiskWorks-speedtest")):
        if c and os.path.isabs(c) and same_volume(c, root) and c not in cands:
            cands.append(c)
    errors = []
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, TEST_NAME)
            f = UnbufferedFile(probe, write=True)
            f.close()
            os.remove(probe)
            return c
        except OSError as e:
            errors.append(f"{c}: {getattr(e, 'strerror', None) or e}")
    raise RuntimeError(f"No folder on {root} lets this user create the test file"
                       + (" (the root of the Windows drive needs administrator rights)" if IS_WIN else "")
                       + ". Tried " + "; ".join(errors))


# ----------------------------------------------------------------------------
# The test
# ----------------------------------------------------------------------------
class SpeedTest:
    def __init__(self, app):
        self.app = app
        self.events = app.log.__class__(4000)
        self.state: dict = {"running": False, "target": None, "path": None, "phase": None, "sizeMB": 0, "block": 0,
                            "runs": [], "current": None, "error": None, "started": None, "finished": None}
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    # -- routes ------------------------------------------------------------------
    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/speed/targets":
            h._json({"targets": self.targets()})
            return True
        if path == "/api/speed/events":
            since = int(q.get("since", ["0"])[0])
            h._json({"events": self.events.since(since), "seq": self.events.seq, "state": self.public_state()})
            return True
        if path == "/api/speed/state":
            h._json(self.public_state())
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/speed/start":
            h._json(self.start(body))
            return True
        if path == "/api/speed/stop":
            self.stop_flag.set()
            h._json({"ok": True})
            return True
        return False

    def public_state(self) -> dict:
        s = dict(self.state)
        s["runs"] = s["runs"][-30:]
        return s

    def targets(self) -> list[dict]:
        """Mounted volumes with enough free space for a test file."""
        inv = self.app.current_inventory()
        out = []
        for d in inv.get("disks", []):
            for p in d.get("partitions", []):
                root = None
                if p.get("letter"):
                    root = f"{p['letter']}:\\"
                elif p.get("mountpoints"):
                    root = p["mountpoints"][0]
                if not root or p.get("free") is None:
                    continue
                out.append({"id": p["id"], "root": root, "title": (p.get("label") or "") + (f" ({p['letter']}:)" if p.get("letter") else ""),
                            "disk": d["name"], "model": d.get("model"), "bus": d.get("bus"), "fs": p.get("fs"), "free": p.get("free"),
                            "size": p.get("volSize") or p.get("size"), "system": bool(d.get("system") or d.get("boot"))})
        out.sort(key=lambda t: (t["system"], t["root"]))
        return out

    # -- control -----------------------------------------------------------------
    def start(self, body: dict) -> dict:
        if self.thread and self.thread.is_alive():
            raise RuntimeError("A speed test is already running.")
        root = str(body.get("root") or "")
        if not root or not os.path.isdir(root):
            raise RuntimeError("Pick a drive to test.")
        size_mb = max(100, min(int(body.get("sizeMB") or 1000), 20000))
        block = int(body.get("block") or 1 << 20)
        if block not in (64 << 10, 256 << 10, 1 << 20, 4 << 20, 8 << 20):
            block = 1 << 20
        loop = bool(body.get("loop", True))
        try:
            st = os.statvfs(root) if hasattr(os, "statvfs") else None
            free = st.f_bavail * st.f_frsize if st else __import__("shutil").disk_usage(root).free
        except OSError as e:
            raise RuntimeError(f"Cannot read the free space of {root}: {e}")
        if free < size_mb * MB + 64 * MB:
            raise RuntimeError(f"Not enough free space on {root} for a {size_mb} MB test file.")
        folder = writable_folder_on(root)
        path = os.path.join(folder, TEST_NAME)
        self.stop_flag.clear()
        self.events.clear()
        self.state = {"running": True, "target": root, "path": path, "folder": folder, "phase": "write", "sizeMB": size_mb, "block": block,
                      "runs": [], "current": None, "error": None, "started": time.time(), "finished": None, "loop": loop}
        self.thread = threading.Thread(target=self._run, args=(root, path, size_mb, block, loop), daemon=True, name="speedtest")
        self.thread.start()
        self.app.info(f"Speed test started on {root} ({size_mb} MB, {block // 1024} KiB blocks, file in {folder})")
        return {"ok": True}

    def _run(self, root: str, path: str, size_mb: int, block: int, loop: bool) -> None:
        total = (size_mb * MB // block) * block
        buf = mmap.mmap(-1, block)
        mv = memoryview(buf)
        # incompressible-ish pattern so drives with transparent compression report real speed
        seed = os.urandom(4096)
        for off in range(0, block, 4096):
            mv[off:off + 4096] = seed
        try:
            while not self.stop_flag.is_set():
                run = {"write": None, "read": None, "ts": time.time()}
                for phase in ("write", "read"):
                    self.state["phase"] = phase
                    f = UnbufferedFile(path, write=(phase == "write"))
                    try:
                        done = 0
                        t0 = time.time()
                        last_t, last_b = t0, 0
                        f.seek0()
                        while done < total and not self.stop_flag.is_set():
                            n = f.write(mv) if phase == "write" else f.read(mv)
                            if n <= 0:
                                raise RuntimeError("The drive returned no data.")
                            done += n
                            now = time.time()
                            if now - last_t >= 0.2:
                                cur = (done - last_b) / (now - last_t) / MB
                                self.state["current"] = {"phase": phase, "mbps": round(cur, 1), "pct": round(100 * done / total, 1)}
                                self.events.push({"type": "tick", "phase": phase, "mbps": round(cur, 1), "pct": round(100 * done / total, 1)})
                                last_t, last_b = now, done
                        if phase == "write":
                            f.flush()
                        el = time.time() - t0
                        if self.stop_flag.is_set():
                            break
                        run[phase] = round(done / el / MB, 1) if el > 0 else None
                        self.events.push({"type": "phase", "phase": phase, "mbps": run[phase], "seconds": round(el, 2)})
                    finally:
                        f.close()
                if run["write"] is not None and run["read"] is not None:
                    self.state["runs"].append(run)
                    self.events.push({"type": "run", "run": run, "n": len(self.state["runs"])})
                if not loop:
                    break
        except Exception as e:
            self.state["error"] = str(e)
            self.events.push({"type": "error", "message": str(e)})
            self.app.info(f"Speed test stopped: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            self.state.update({"running": False, "phase": None, "finished": time.time(), "current": None})
            self.events.push({"type": "done", "runs": len(self.state["runs"])})
