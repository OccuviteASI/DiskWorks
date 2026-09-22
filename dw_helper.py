"""The privileged helper: `DiskWorks --helper <address> [--key-file f] --parent pid --state dir`.

Started once per session by the window process through UAC / pkexec.  Connects back
over dw_ipc, proves it holds the session key, then serves allow-listed verbs one job
at a time, streaming progress frames.  Exits when the connection drops or the window
process disappears.  No UI, no HTTP.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
from multiprocessing.connection import Client

import dw_fs
import dw_inventory as di
import dw_ipc

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MAX_RAW_READ = 16 * 2**20


def _arg(argv: list[str], name: str, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def parent_alive(pid: int) -> bool:
    try:
        if IS_WIN:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wt.HANDLE
            k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
            k32.CloseHandle.argtypes = [wt.HANDLE]
            h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not h:
                return False
            try:
                return k32.WaitForSingleObject(h, 0) != 0  # 0 = WAIT_OBJECT_0 = exited
            finally:
                k32.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True


class Helper:
    def __init__(self, conn, state_dir: str, parent_pid: int, uid: int | None):
        self.conn = conn
        self.state_dir = state_dir
        self.parent_pid = parent_pid
        self.uid = uid
        self.send_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.job_thread: threading.Thread | None = None
        self.job_id: int | None = None
        self.cancel_event = threading.Event()
        self.inventory_cache: dict | None = None
        self.wsl_cache: tuple[float, dict] | None = None
        self.locked_devices: dict[str, object] = {}
        self.verbs = {
            "ping": self.v_ping, "inventory": self.v_inventory, "raw_read": self.v_raw_read,
            "identify": self.v_identify, "wsl_state": self.v_wsl_state,
        }
        try:
            import dw_ops_exec  # steps / imaging / access verbs (later phases)
            self.verbs.update(dw_ops_exec.verbs(self))
        except ImportError:
            pass

    # -- wire -----------------------------------------------------------------
    def emit(self, rid: int, event: str, **kw) -> None:
        msg = {"id": rid, "event": event}
        msg.update(kw)
        with self.send_lock:
            dw_ipc.send(self.conn, msg)

    def log_cmd(self, rid: int):
        """A logger callable (cmd, code, ms, output, title) for the tool runners."""
        def _log(cmd, code, ms, output, title=""):
            self.emit(rid, "log", cmd=cmd, code=code, ms=round(ms), output=(output or "")[-20000:], title=title)
        return _log

    def note(self, rid: int, msg: str) -> None:
        self.emit(rid, "log", msg=msg)

    def progress(self, rid: int, **kw) -> None:
        self.emit(rid, "progress", **kw)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RuntimeError("Cancelled.")

    # -- main loop ------------------------------------------------------------
    def serve(self) -> int:
        threading.Thread(target=self._watch_parent, daemon=True).start()
        while True:
            try:
                msg = dw_ipc.recv(self.conn)
            except (EOFError, OSError, ConnectionError):
                return 0
            except Exception:
                return 0
            verb = msg.get("verb")
            rid = int(msg.get("id") or 0)
            args = msg.get("args") or {}
            if verb == "quit":
                return 0
            if verb == "cancel":
                self.cancel_event.set()
                continue
            if verb == "ping":
                self.emit(rid, "done", pong=True, ts=time.time())
                continue
            fn = self.verbs.get(verb)
            if fn is None:
                self.emit(rid, "error", message=f"Unknown helper verb '{verb}'.")
                continue
            with self.job_lock:
                if self.job_thread and self.job_thread.is_alive():
                    self.emit(rid, "error", message="The helper is busy with another job.")
                    continue
                self.cancel_event.clear()
                self.job_id = rid
                self.job_thread = threading.Thread(target=self._run, args=(fn, rid, args), daemon=True)
                self.job_thread.start()

    def _run(self, fn, rid: int, args: dict) -> None:
        try:
            result = fn(rid, args) or {}
            self.emit(rid, "done", **result)
        except RuntimeError as e:
            self.emit(rid, "error", message=str(e))
        except Exception as e:
            self.emit(rid, "error", message=f"Unexpected problem in the helper: {e}", trace=traceback.format_exc()[-4000:])

    def _watch_parent(self) -> None:
        while True:
            time.sleep(2.0)
            if self.parent_pid and not parent_alive(self.parent_pid):
                os._exit(0)

    # -- verbs ----------------------------------------------------------------
    def v_ping(self, rid: int, args: dict) -> dict:
        return {"pong": True}

    def v_inventory(self, rid: int, args: dict) -> dict:
        inv = di.inventory()
        self.refine(inv, rid)
        self.inventory_cache = inv
        return {"inventory": inv}

    def refine(self, inv: dict, rid: int) -> None:
        """Add what only root/admin can see: filesystem signatures of RAW/unknown partitions,
        BitLocker state, table type of unknown disks, WSL availability."""
        bl = {}
        if IS_WIN:
            import dw_win
            try:
                bl = dw_win.bitlocker_status()
            except Exception:
                bl = {}
        if IS_MAC:
            import dw_mac
            dw_mac.refine(inv, log=self.log_cmd(rid))   # exact starts + free space from gpt / fdisk
        for d in inv.get("disks", []):
            dev = None
            try:
                dev = self.open_ro(d)
                if d.get("table") in ("unknown", "none") and dev is not None:
                    d["table"] = dw_fs.identify_table(dev.read(0, 512), dev.read(d.get("logicalSector") or 512, 512))
                for p in d.get("partitions", []):
                    if bl and p.get("letter") and p["letter"] in bl:
                        p["bitlocker"] = bl[p["letter"]]
                        if bl[p["letter"]].get("lock") == "Locked":
                            p["fs"] = "bitlocker"
                            p["fsSource"] = "signature"
                    if p.get("fs") is None and p.get("size") and dev is not None:
                        start = p["start"]
                        fs, detail = dw_fs.identify(lambda off, n: dev.read(start + off, n))
                        if fs:
                            p["fs"] = fs
                            p["fsSource"] = "signature"
                            if detail:
                                p["fsDetail"] = detail
                            p.setdefault("mountpoints", [])
            except Exception as e:
                d["refineError"] = str(e)
            finally:
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
            di.apply_protection(d)
        inv["hash"] = di.layout_hash(inv["disks"])
        inv["elevated"] = True
        inv["refined"] = True
        if IS_WIN:
            inv["wsl"] = self.wsl_state()

    def open_ro(self, disk: dict):
        if IS_WIN:
            import dw_win
            return dw_win.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)
        if IS_MAC:
            import dw_mac
            return dw_mac.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)
        import dw_linux
        return dw_linux.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)

    def wsl_state(self) -> dict:
        if self.wsl_cache and time.time() - self.wsl_cache[0] < 30:
            return self.wsl_cache[1]
        import dw_win
        try:
            st = dw_win.wsl_state()
        except Exception as e:
            st = {"installed": False, "distros": [], "mountable": False, "why": str(e)}
        self.wsl_cache = (time.time(), st)
        return st

    def v_wsl_state(self, rid: int, args: dict) -> dict:
        if not IS_WIN:
            return {"wsl": None}
        self.wsl_cache = None
        return {"wsl": self.wsl_state()}

    def find_disk(self, disk_id: str) -> dict:
        inv = self.inventory_cache or di.inventory()
        for d in inv.get("disks", []):
            if d["id"] == disk_id:
                return d
        raise RuntimeError(f"Disk {disk_id} is not present any more.")

    def v_raw_read(self, rid: int, args: dict) -> dict:
        disk = self.find_disk(str(args.get("disk")))
        offset = int(args.get("offset") or 0)
        length = int(args.get("length") or 512)
        if length <= 0 or length > MAX_RAW_READ or offset < 0:
            raise RuntimeError("Read size out of range.")
        dev = self.open_ro(disk)
        try:
            data = dev.read(offset, length)
        finally:
            dev.close()
        return {"data": base64.b64encode(data).decode("ascii"), "length": len(data)}

    def v_identify(self, rid: int, args: dict) -> dict:
        disk = self.find_disk(str(args.get("disk")))
        start = int(args.get("start") or 0)
        dev = self.open_ro(disk)
        try:
            fs, detail = dw_fs.identify(lambda off, n: dev.read(start + off, n))
        finally:
            dev.close()
        return {"fs": fs, "detail": detail, "label": dw_fs.fs_label(fs)}


def helper_main(argv: list[str]) -> int:
    address = _arg(argv, "--helper")
    key_file = _arg(argv, "--key-file")
    parent = int(_arg(argv, "--parent", "0") or 0)
    state = _arg(argv, "--state") or os.getcwd()
    uid = _arg(argv, "--uid")
    if not address:
        print("usage: --helper <address> [--key-file f] --parent pid --state dir", file=sys.stderr)
        return 2
    if key_file:
        with open(key_file, "r", encoding="ascii") as f:
            key_hex = f.read().strip()
        try:
            os.remove(key_file)
        except OSError:
            pass
    else:
        key_hex = sys.stdin.readline().strip()
    if not key_hex:
        return 3
    family = "AF_PIPE" if IS_WIN else "AF_UNIX"
    last = None
    for _ in range(40):  # the listener is up before we are launched, but be tolerant
        try:
            conn = Client(address, family=family, authkey=bytes.fromhex(key_hex))
            break
        except Exception as e:
            last = e
            time.sleep(0.25)
    else:
        print(f"helper: could not connect to {address}: {last}", file=sys.stderr)
        return 4
    import diskworks
    dw_ipc.send(conn, {"hello": {"version": diskworks.VERSION, "pid": os.getpid(), "platform": di.PLATFORM,
                                 "admin": diskworks.is_admin(), "uid": int(uid) if uid else None}})
    h = Helper(conn, state, parent, int(uid) if uid else None)
    try:
        return h.serve()
    finally:
        try:
            conn.close()
        except Exception:
            pass
