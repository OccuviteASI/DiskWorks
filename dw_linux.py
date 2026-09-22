"""Linux backend: bundled-tool resolution, raw device access, mount / unmount, busy
checks, the kernel-driver ladder.  Everything here runs inside the root helper
unless noted."""
from __future__ import annotations

import fcntl
import mmap
import os
import shutil
import struct
import subprocess
import sys
import time

BLKRRPART = 0x125F
BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268


def resource_dir() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def tools_dir() -> str:
    return os.path.join(resource_dir(), "bin", "linux-x86_64", "tools")


def tool(name: str) -> str | None:
    """Path of a bundled tool (preferred) or the system one; None when neither exists."""
    p = os.path.join(tools_dir(), name)
    if os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    return shutil.which(name)


def tool_env() -> dict:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    lib = resource_dir()
    if os.path.isdir(tools_dir()):
        env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        env["PATH"] = tools_dir() + ":" + env.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    return env


def run(argv: list[str], timeout: float = 3600.0, on_line=None, log=None, title: str = "", input_text: str | None = None) -> tuple[int, str]:
    """Run a tool, streaming its output lines to on_line (for progress) and logging the whole run."""
    exe = tool(argv[0])
    if not exe:
        raise RuntimeError(f"The tool '{argv[0]}' is not available (not bundled and not installed).")
    cmd = [exe] + argv[1:]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                            env=tool_env(), text=False)
    if input_text is not None:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    out = bytearray()
    buf = b""
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        out += chunk
        if chunk in (b"\r", b"\n"):
            if on_line and buf.strip():
                on_line(buf.decode("utf-8", "replace").strip())
            buf = b""
        else:
            buf += chunk
    proc.wait(timeout=timeout)
    text = out.decode("utf-8", "replace")
    if log:
        log(" ".join(argv), proc.returncode, (time.time() - t0) * 1000, text.strip(), title)
    return proc.returncode, text


class RawDevice:
    def __init__(self, path: str, write: bool = False, sector: int = 512, direct: bool = False):
        self.path = path
        flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
        if write:
            flags |= os.O_EXCL
        if direct and hasattr(os, "O_DIRECT"):
            flags |= os.O_DIRECT
        self.fd = os.open(path, flags)
        try:
            buf = bytearray(4)
            fcntl.ioctl(self.fd, BLKSSZGET, buf)
            self.sector = struct.unpack("i", buf)[0] or sector
        except OSError:
            self.sector = sector
        self._buf = mmap.mmap(-1, 1 << 20)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.fsync(self.fd)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def length(self) -> int:
        try:
            buf = bytearray(8)
            fcntl.ioctl(self.fd, BLKGETSIZE64, buf)
            return struct.unpack("Q", buf)[0]
        except OSError:
            return os.fstat(self.fd).st_size

    def read(self, offset: int, length: int) -> bytes:
        return os.pread(self.fd, length, offset)

    def write_aligned(self, offset: int, mv) -> int:
        return os.pwrite(self.fd, mv, offset)

    def flush(self) -> None:
        os.fdatasync(self.fd)

    def rescan(self) -> None:
        try:
            fcntl.ioctl(self.fd, BLKRRPART)
        except OSError:
            pass


def reread_table(path: str, log=None) -> None:
    """Ask the kernel to re-read the partition table; fall back to blockdev, then make sure the
    kernel's partition list matches the table (partx / partprobe when the ioctl was a no-op)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, BLKRRPART)
        finally:
            os.close(fd)
    except OSError:
        if tool("blockdev"):
            run(["blockdev", "--rereadpt", path], timeout=30, log=log, title="Re-read partition table")
    settle()
    sync_partition_nodes(path, log=log)


def settle() -> None:
    if shutil.which("udevadm"):
        subprocess.run(["udevadm", "settle", "--timeout=10"], capture_output=True)


def kernel_partition_count(path: str) -> int | None:
    """How many partitions the kernel currently exposes for this disk (sda1, loop0p1, nvme0n1p1 ...)."""
    name = os.path.basename(os.path.realpath(path))
    sysdir = f"/sys/class/block/{name}"
    if not os.path.isdir(sysdir):
        return None
    try:
        return sum(1 for e in os.listdir(sysdir) if e.startswith(name) and os.path.isfile(os.path.join(sysdir, e, "partition")))
    except OSError:
        return None


def table_partition_count(path: str) -> int | None:
    """How many partitions the on-disk table holds, per sfdisk; None when unknown, 0 when there is no table."""
    if not tool("sfdisk"):
        return None
    try:
        code, out = run(["sfdisk", "-J", path], timeout=30)
    except RuntimeError:
        return None
    if code != 0:
        return 0 if "does not contain a recognized partition table" in out else None
    try:
        import json
        return len(json.loads(out).get("partitiontable", {}).get("partitions", []))
    except (ValueError, AttributeError):
        return None


def sync_partition_nodes(path: str, log=None) -> None:
    """BLKRRPART can succeed without changing anything (seen on loop devices under some kernels):
    the table on disk then differs from what the kernel exposes and /dev/<disk>p1 never appears.
    Compare the two and ask partx (BLKPG add / delete / resize per partition), then partprobe,
    to reconcile them. Nothing runs when they already agree."""
    want = table_partition_count(path)
    have = kernel_partition_count(path)
    if want is None or have is None or want == have:
        return
    if log:
        log(f"kernel: {have} partition(s), table: {want}", 0, 0, "", "Partition table not picked up by the kernel")
    for argv in (["partx", "-u", path], ["partprobe", path]):
        if not tool(argv[0]):
            continue
        run(argv, timeout=60, log=log, title="Register partitions with the kernel")
        settle()
        if kernel_partition_count(path) == want:
            return
    if log:
        log(f"kernel still shows {kernel_partition_count(path)} partition(s) on {path}", 1, 0, "", "Register partitions with the kernel")


# ----------------------------------------------------------------------------
# Mounts
# ----------------------------------------------------------------------------
def mountpoints(dev: str) -> list[str]:
    real = os.path.realpath(dev)
    out = []
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and os.path.realpath(parts[0]) == real:
                    out.append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    return out


def swap_active(dev: str) -> bool:
    real = os.path.realpath(dev)
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as f:
            return any(os.path.realpath(l.split()[0]) == real for l in f.readlines()[1:] if l.strip())
    except OSError:
        return False


def unmount(dev: str, log=None) -> list[str]:
    """Unmount every mount point of dev (and swapoff). Returns what was unmounted (to remount later)."""
    done = []
    if swap_active(dev):
        run(["swapoff", dev], timeout=120, log=log, title="Turn swap off")
    for mp in mountpoints(dev):
        code, out = run(["umount", mp], timeout=120, log=log, title="Unmount")
        if code != 0:
            holders = who_uses(mp)
            raise RuntimeError(f"Could not unmount {mp}: {out.strip().splitlines()[-1] if out.strip() else 'busy'}"
                               + (f". In use by: {holders}" if holders else ""))
        done.append(mp)
    return done


def who_uses(mountpoint: str) -> str:
    if shutil.which("fuser"):
        r = subprocess.run(["fuser", "-vm", mountpoint], capture_output=True, text=True)
        lines = [l.strip() for l in (r.stderr or "").splitlines()[1:] if l.strip()]
        return "; ".join(lines[:5])
    return ""


def mount(dev: str, mountpoint: str | None = None, fstype: str | None = None, options: str | None = None, log=None) -> str:
    if not mountpoint:
        base = "/run/media/diskworks" if os.path.isdir("/run/media") else "/mnt"
        name = os.path.basename(dev)
        mountpoint = os.path.join(base, name)
    os.makedirs(mountpoint, exist_ok=True)
    argv = ["mount"]
    if fstype:
        argv += ["-t", fstype]
    if options:
        argv += ["-o", options]
    argv += [dev, mountpoint]
    code, out = run(argv, timeout=120, log=log, title="Mount")
    if code != 0:
        raise RuntimeError(f"Mount failed: {out.strip().splitlines()[-1] if out.strip() else code}")
    return mountpoint


def kernel_supports(fs: str) -> str:
    """'builtin' | 'module' | 'none' for a filesystem name as the kernel knows it."""
    try:
        with open("/proc/filesystems", "r", encoding="utf-8") as f:
            for line in f:
                if line.split()[-1] == fs:
                    return "builtin"
    except OSError:
        pass
    if shutil.which("modprobe"):
        r = subprocess.run(["modprobe", "-n", "-q", fs], capture_output=True, text=True)
        if r.returncode == 0:
            return "module"
    return "none"


def package_manager() -> str | None:
    for pm in ("apt-get", "dnf", "zypper", "pacman"):
        if shutil.which(pm):
            return pm
    return None
