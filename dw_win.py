"""Windows backend: raw disk / volume access through ctypes, volume locking, the
Storage-cmdlet and diskpart runners, BitLocker status, WSL wrappers.
Everything here runs inside the elevated helper unless noted.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import mmap
import os
import re
import subprocess
import time

CREATE_NO_WINDOW = 0x08000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083
FSCTL_SET_SPARSE = 0x000900C4
FSCTL_SET_ZERO_DATA = 0x000980C8
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
k32.FlushFileBuffers.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.FindFirstVolumeW.restype = wt.HANDLE
k32.FindFirstVolumeW.argtypes = [wt.LPWSTR, wt.DWORD]
k32.FindNextVolumeW.restype = wt.BOOL
k32.FindNextVolumeW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD]
k32.FindVolumeClose.argtypes = [wt.HANDLE]
k32.GetLastError.restype = wt.DWORD


def _err(what: str) -> RuntimeError:
    code = ctypes.GetLastError()
    return RuntimeError(f"{what} failed: {ctypes.FormatError(code).strip()} (Windows error {code})")


class STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [("DeviceType", wt.DWORD), ("DeviceNumber", wt.DWORD), ("PartitionNumber", wt.DWORD)]


class RawDevice:
    """A physical drive (\\\\.\\PhysicalDriveN) or volume (\\\\.\\X:, \\\\?\\Volume{..}) opened for
    sector I/O.  Reads and writes are sector aligned; unbuffered + write-through when writing."""

    def __init__(self, path: str, write: bool = False, sector: int = 512, unbuffered: bool = True):
        self.path = path.rstrip("\\")
        self.sector = sector
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        flags = (FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH) if (write and unbuffered) else 0
        self.h = k32.CreateFileW(self.path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None)
        if self.h == INVALID_HANDLE_VALUE or not self.h:
            raise _err(f"Opening {path}")
        self.locked: list = []
        self._buf = mmap.mmap(-1, 1 << 20)  # page aligned scratch

    def close(self) -> None:
        for h in self.locked:
            try:
                out = wt.DWORD(0)
                k32.DeviceIoControl(h, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, ctypes.byref(out), None)
                k32.CloseHandle(h)
            except Exception:
                pass
        self.locked = []
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def length(self) -> int:
        out = ctypes.c_longlong(0)
        got = wt.DWORD(0)
        if not k32.DeviceIoControl(self.h, IOCTL_DISK_GET_LENGTH_INFO, None, 0, ctypes.byref(out), 8, ctypes.byref(got), None):
            raise _err("Reading the device size")
        return out.value

    def seek(self, offset: int) -> None:
        if not k32.SetFilePointerEx(self.h, offset, None, 0):
            raise _err("Seeking")

    def read(self, offset: int, length: int) -> bytes:
        """Read `length` bytes at `offset` (any alignment; handled internally)."""
        s = self.sector
        a_off = offset - (offset % s)
        a_len = ((offset + length - a_off + s - 1) // s) * s
        out = bytearray()
        pos = a_off
        while len(out) < a_len:
            chunk = min(a_len - len(out), len(self._buf))
            self.seek(pos)
            got = wt.DWORD(0)
            if not k32.ReadFile(self.h, ctypes.c_void_p(ctypes.addressof(ctypes.c_char.from_buffer(self._buf))), chunk, ctypes.byref(got), None):
                raise _err(f"Reading {self.path} at {pos}")
            if got.value == 0:
                break
            out += self._buf[: got.value]
            pos += got.value
        skip = offset - a_off
        return bytes(out[skip: skip + length])

    def write_aligned(self, offset: int, mv) -> int:
        """Write a sector-aligned buffer (memoryview/mmap) at a sector-aligned offset."""
        self.seek(offset)
        n = len(mv)
        got = wt.DWORD(0)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(mv))
        if not k32.WriteFile(self.h, ctypes.c_void_p(addr), n, ctypes.byref(got), None):
            raise _err(f"Writing {self.path} at {offset}")
        return got.value

    def flush(self) -> None:
        k32.FlushFileBuffers(self.h)

    def rescan(self) -> None:
        got = wt.DWORD(0)
        k32.DeviceIoControl(self.h, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0, ctypes.byref(got), None)

    # -- volumes on this physical drive ---------------------------------------
    def lock_volumes(self, disk_number: int, timeout: float = 15.0) -> list[str]:
        """Lock + dismount every volume that lives on physical drive `disk_number`; keep the
        handles open (unlocked at close()).  Returns the volume GUID paths locked."""
        locked_paths: list[str] = []
        for vol in volumes_on_disk(disk_number):
            path = vol.rstrip("\\")
            deadline = time.time() + timeout
            h = None
            while True:
                h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                    OPEN_EXISTING, 0, None)
                if h == INVALID_HANDLE_VALUE or not h:
                    raise _err(f"Opening volume {path}")
                got = wt.DWORD(0)
                if k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(got), None):
                    break
                err = ctypes.GetLastError()
                k32.CloseHandle(h)
                h = None
                if time.time() > deadline:
                    raise RuntimeError(f"Volume {path} is in use and could not be locked (Windows error {err}). "
                                       "Close programs using the drive and try again.")
                time.sleep(0.5)
            got = wt.DWORD(0)
            k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(got), None)
            self.locked.append(h)
            locked_paths.append(vol)
        return locked_paths


def volumes_on_disk(disk_number: int) -> list[str]:
    """\\\\?\\Volume{GUID}\\ paths whose device number is `disk_number`."""
    buf = ctypes.create_unicode_buffer(260)
    out: list[str] = []
    h = k32.FindFirstVolumeW(buf, 260)
    if h == INVALID_HANDLE_VALUE or not h:
        return out
    try:
        while True:
            vol = buf.value
            vh = k32.CreateFileW(vol.rstrip("\\"), 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
            if vh and vh != INVALID_HANDLE_VALUE:
                sdn = STORAGE_DEVICE_NUMBER()
                got = wt.DWORD(0)
                if k32.DeviceIoControl(vh, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0, ctypes.byref(sdn),
                                       ctypes.sizeof(sdn), ctypes.byref(got), None):
                    if sdn.DeviceNumber == disk_number and sdn.DeviceType == 7:  # FILE_DEVICE_DISK
                        out.append(vol)
                k32.CloseHandle(vh)
            if not k32.FindNextVolumeW(h, buf, 260):
                break
    finally:
        k32.FindVolumeClose(h)
    return out


# ----------------------------------------------------------------------------
# PowerShell / diskpart runners
# ----------------------------------------------------------------------------
def decode_clixml(text: str) -> str:
    """PowerShell writes its error stream as CLIXML when not attached to a console; turn it back into lines."""
    if "#< CLIXML" not in text:
        return text
    import html
    parts = re.findall(r'<S S="(?:Error|Warning|Verbose|Debug)">(.*?)</S>', text, flags=re.S)
    out = []
    for p in parts:
        p = p.replace("_x000D__x000A_", "\n").replace("_x000A_", "\n").replace("_x000D_", "")
        p = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), p)
        out.append(html.unescape(p))
    text2 = "".join(out).strip()
    return text2 or re.sub(r"<[^>]+>", "", text).replace("#< CLIXML", "").strip()


def run_powershell(script: str, timeout: float = 600.0, log=None, title: str = "") -> tuple[int, str, str]:
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = decode_clixml((r.stderr or b"").decode("utf-8", "replace"))
    if log:
        log(script.strip(), r.returncode, (time.time() - t0) * 1000, (out + ("\n" + err if err else "")).strip(), title)
    return r.returncode, out, err


def run_diskpart(lines: list[str], timeout: float = 3600.0, on_progress=None, log=None, title: str = "") -> tuple[int, str]:
    """Run a diskpart script; percent lines ('N percent completed') are reported through on_progress."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="diskworks-dp-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\nexit\n")
    t0 = time.time()
    proc = subprocess.Popen(["diskpart", "/s", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW)
    out = bytearray()
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            out += chunk
            if chunk in (b"\r", b"\n"):
                line = buf.decode("mbcs", "replace").strip()
                buf = b""
                m = re.search(r"(\d+)\s*percent", line)
                if m and on_progress:
                    on_progress(int(m.group(1)), line)
            else:
                buf += chunk
        proc.wait(timeout=timeout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    text = out.decode("mbcs", "replace")
    if log:
        log("diskpart /s <script>\n" + "\n".join(lines), proc.returncode, (time.time() - t0) * 1000, text.strip(), title)
    return proc.returncode, text


# ----------------------------------------------------------------------------
# BitLocker, WSL
# ----------------------------------------------------------------------------
def bitlocker_status() -> dict[str, dict]:
    """{ 'C': {'protection': 'On'|'Off', 'lock': 'Locked'|'Unlocked', 'percent': 100.0} } via manage-bde (admin)."""
    try:
        r = subprocess.run(["manage-bde", "-status"], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    text = (r.stdout or b"").decode("mbcs", "replace")
    out: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^Volume\s+([A-Z]):", line.strip())
        if m:
            cur = m.group(1).upper()
            out[cur] = {}
            continue
        if cur and ":" in line:
            k, _, v = line.strip().partition(":")
            k, v = k.strip().lower(), v.strip()
            if k.startswith("protection status"):
                out[cur]["protection"] = "On" if re.search(r"on", v.lower()) else "Off"
            elif k.startswith("lock status"):
                out[cur]["lock"] = "Locked" if "locked" in v.lower() and "unlocked" not in v.lower() else "Unlocked"
            elif k.startswith("percentage encrypted"):
                try:
                    out[cur]["percent"] = float(v.replace("%", "").replace(",", ".").strip())
                except ValueError:
                    pass
            elif k.startswith("conversion status"):
                out[cur]["conversion"] = v
    return out


def wsl(args: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """Run wsl.exe and decode its UTF-16LE output."""
    try:
        r = subprocess.run(["wsl.exe"] + args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return 127, "wsl.exe is not installed"
    except subprocess.TimeoutExpired:
        return 124, "wsl.exe did not answer"
    raw = (r.stdout or b"") + (r.stderr or b"")
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    return r.returncode, text.replace("\x00", "").strip()


def wsl_state() -> dict:
    """{'installed': bool, 'version': str, 'distros': [names], 'default': name, 'mountable': bool, 'why': str}"""
    code, text = wsl(["--status"], timeout=20)
    if code == 127:
        return {"installed": False, "distros": [], "mountable": False, "why": "WSL is not installed on this computer."}
    st = {"installed": True, "version": "", "distros": [], "default": None, "mountable": False, "why": ""}
    code2, ver = wsl(["--version"], timeout=20)
    m = re.search(r"WSL version:\s*([\d.]+)", ver)
    if m:
        st["version"] = m.group(1)
    code3, lst = wsl(["-l", "-v"], timeout=20)
    for line in lst.splitlines()[1:]:
        parts = line.replace("*", " ").split()
        if len(parts) >= 3 and parts[-1] in ("1", "2"):
            name = " ".join(parts[:-2])
            st["distros"].append({"name": name, "state": parts[-2], "version": int(parts[-1])})
            if line.strip().startswith("*"):
                st["default"] = name
    if code3 != 0 or not st["distros"]:
        st["why"] = "WSL is installed but no Linux distribution is; install one from the Microsoft Store (for example Ubuntu)."
        return st
    if not any(d["version"] == 2 for d in st["distros"]):
        st["why"] = "Only WSL 1 distributions are installed; wsl --mount needs WSL 2."
        return st
    st["mountable"] = True
    return st
