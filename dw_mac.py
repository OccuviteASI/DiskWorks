"""macOS backend: inventory from `diskutil … -plist`, exact geometry from `gpt -r show`
(root), raw device I/O on /dev/rdiskN, diskutil mount/unmount, hdiutil test disks.
Everything that changes a disk runs inside the root helper (ARCHITECTURE.md §1).

Written and unit-checked against recorded diskutil output; the first run on real Mac
hardware is Kenton's (MACOS.md §7).
"""
from __future__ import annotations

try:
    import fcntl          # macOS / Linux only; the module is also imported for unit checks on Windows
except ImportError:   # pragma: no cover
    fcntl = None
import mmap
import os
import plistlib
import re
import struct
import subprocess
import sys
import time

import dw_fs

DISKUTIL = "/usr/sbin/diskutil"
GPT = "/usr/sbin/gpt"
FDISK = "/sbin/fdisk"
HDIUTIL = "/usr/bin/hdiutil"
OSASCRIPT = "/usr/bin/osascript"
OPEN = "/usr/bin/open"
FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
DKIOCGETBLOCKSIZE = 0x40046418
DKIOCGETBLOCKCOUNT = 0x40086419

# diskutil "Content" strings -> GPT type GUIDs (so the rest of the app sees the same ids)
CONTENT_GUIDS = {
    "EFI": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    "Apple_APFS": "7c3457ef-0000-11aa-aa11-00306543ecac",
    "Apple_APFS_ISC": "69646961-6700-11aa-aa11-00306543ecac",
    "Apple_APFS_Recovery": "52637672-7900-11aa-aa11-00306543ecac",
    "Apple_HFS": "48465300-0000-11aa-aa11-00306543ecac",
    "Apple_Boot": "426f6f74-0000-11aa-aa11-00306543ecac",
    "Apple_CoreStorage": "53746f72-6167-11aa-aa11-00306543ecac",
    "Microsoft Basic Data": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
    "Microsoft Reserved": "e3c9e316-0b5c-4db8-817d-f92df00215ae",
    "Windows Recovery": "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
    "Linux Filesystem": "0fc63daf-8483-4772-8e79-3d69d8477de4",
    "Linux Swap": "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f",
    "Linux LVM": "e6d6d379-f507-44c2-a23c-238f2a3df928",
    "BIOS Boot Partition": "21686148-6449-6e6f-744e-656564454649",
}
# what diskutil listFilesystems calls things -> our ids (fallback when the plist is unavailable)
MAC_FORMATS = {"apfs": "APFS", "hfsplus": "JHFS+", "exfat": "ExFAT", "fat32": "MS-DOS FAT32", "fat16": "MS-DOS FAT16"}
FS_FROM_DISKUTIL = {"apfs": "apfs", "hfs": "hfsplus", "hfsx": "hfsplus", "msdos": "fat32", "exfat": "exfat", "ntfs": "ntfs",
                    "ufsd_ntfs": "ntfs", "ext4": "ext4", "ext3": "ext3", "ext2": "ext2", "udf": "iso9660", "cd9660": "iso9660"}


# ----------------------------------------------------------------------------
# Running tools (absolute paths, scrubbed environment: PyInstaller sets DYLD_*)
# ----------------------------------------------------------------------------
def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DYLD_", "LD_"))}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    env["LC_ALL"] = "C"
    return env


def run(argv: list[str], timeout: float = 600.0, log=None, title: str = "", on_line=None, input_text: str | None = None) -> tuple[int, str]:
    t0 = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, env=clean_env())
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


def plist(argv: list[str], timeout: float = 60.0) -> dict:
    r = subprocess.run(argv, capture_output=True, timeout=timeout, env=clean_env())
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"{argv[0]} failed: {(r.stderr or r.stdout).decode('utf-8', 'replace').strip()[:200]}")
    try:
        data = plistlib.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(f"Could not read the plist from {argv[1] if len(argv) > 1 else argv[0]}: {e}")
    return data if isinstance(data, dict) else {}


def parse_list_plist(data: dict) -> dict:
    """Split `diskutil list -plist` into physical disks and synthesized APFS containers."""
    physical, containers = [], {}
    for d in data.get("AllDisksAndPartitions") or []:
        ident = d.get("DeviceIdentifier")
        if not ident:
            continue
        if d.get("APFSVolumes") is not None and d.get("Partitions") is None:
            containers[ident] = {"volumes": d.get("APFSVolumes") or [], "stores": [s.get("APFSPhysicalStore") if isinstance(s, dict) else s
                                                                                     for s in d.get("APFSPhysicalStores") or []],
                                 "size": int(d.get("Size") or 0)}
        else:
            physical.append(d)
    return {"physical": physical, "containers": containers}


def content_to_type(content: str | None, table: str) -> tuple[str | None, int | None, str]:
    c = (content or "").strip()
    if table == "gpt":
        return CONTENT_GUIDS.get(c), None, dw_fs.gpt_type_name(CONTENT_GUIDS.get(c)) if CONTENT_GUIDS.get(c) else (c or "Unknown type")
    m = re.match(r"^(?:DOS_FAT_32|Windows_FAT_32)$", c)
    if m:
        return None, 0x0c, dw_fs.mbr_type_name(0x0c)
    mbr = {"Windows_NTFS": 0x07, "DOS_FAT_16": 0x06, "Linux": 0x83, "Apple_HFS": 0xaf, "Apple_APFS": 0xaf}.get(c)
    return None, mbr, dw_fs.mbr_type_name(mbr) if mbr is not None else (c or "Unknown type")


def mac_inventory(info_fn=None, list_fn=None, root_fn=None) -> dict:
    """Unprivileged inventory. The *_fn hooks let the unit test feed recorded plists."""
    import dw_inventory as di
    list_fn = list_fn or (lambda: plist([DISKUTIL, "list", "-plist"]))
    info_fn = info_fn or (lambda ident: plist([DISKUTIL, "info", "-plist", ident]))
    root_fn = root_fn or (lambda: plist([DISKUTIL, "info", "-plist", "/"]))
    parsed = parse_list_plist(list_fn())
    try:
        root = root_fn()
    except Exception:
        root = {}
    boot_stores = {s.get("APFSPhysicalStore") if isinstance(s, dict) else s for s in root.get("APFSPhysicalStores") or []}
    if not boot_stores and root.get("ParentWholeDisk"):
        boot_stores = {root.get("DeviceIdentifier")}
    boot_disks = {re.sub(r"s\d+$", "", s) for s in boot_stores if s}
    disks: list[dict] = []
    for d in parsed["physical"]:
        ident = d["DeviceIdentifier"]
        try:
            info = info_fn(ident)
        except Exception:
            info = {}
        if info.get("VirtualOrPhysical") == "Virtual":
            continue
        content = (d.get("Content") or info.get("Content") or "")
        table = {"GUID_partition_scheme": "gpt", "FDisk_partition_scheme": "mbr", "Apple_partition_scheme": "apm"}.get(content, "none")
        size = int(d.get("Size") or info.get("Size") or info.get("TotalSize") or 0)
        block = int(info.get("DeviceBlockSize") or 512)
        internal = bool(info.get("Internal", True))
        removable = bool(info.get("RemovableMediaOrExternalDevice") or info.get("RemovableMedia") or info.get("Ejectable"))
        disk = {
            "id": f"disk:{ident}", "number": int(re.sub(r"\D", "", ident) or 0), "name": ident, "path": f"/dev/{ident}", "rawPath": f"/dev/r{ident}",
            "model": (info.get("MediaName") or info.get("IORegistryEntryName") or "").strip(),
            "serial": "", "bus": (info.get("BusProtocol") or "").replace("PCI-Express", "PCIe") or "Unknown",
            "media": "SSD" if info.get("SolidState") else ("HDD" if info.get("SolidState") is False else None),
            "size": size, "logicalSector": block, "physicalSector": block, "table": table,
            "removable": removable and not internal, "hotplug": not internal, "internal": internal,
            "system": ident in boot_disks, "boot": ident in boot_disks,
            "readonly": info.get("WritableMedia") is False, "offline": False, "clustered": False,
            "health": (info.get("SMARTStatus") or "").replace("Not Supported", "") or "", "location": "",
            "uniqueId": info.get("DiskUUID") or "", "largestFree": 0, "guid": info.get("DiskUUID") or "",
            "partitions": [], "approx": True,
        }
        head, _ = di._gpt_reserve(block) if table == "gpt" else (block, 0)
        cursor = max(head, 40 * 512) if table == "gpt" else block
        for idx, p in enumerate(d.get("Partitions") or [], start=1):
            pid = p.get("DeviceIdentifier") or f"{ident}s{idx}"
            try:
                pinfo = info_fn(pid)
            except Exception:
                pinfo = {}
            psize = int(p.get("Size") or pinfo.get("Size") or pinfo.get("TotalSize") or 0)
            type_guid, mbr_type, type_name = content_to_type(p.get("Content") or pinfo.get("Content"), table)
            fst = (pinfo.get("FilesystemType") or "").lower()
            fs = FS_FROM_DISKUTIL.get(fst) or dw_fs.normalize_fs(fst) if fst else None
            if fs == "fat32" and "16" in (pinfo.get("FilesystemName") or ""):
                fs = "fat16"
            container = pinfo.get("APFSContainerReference") or p.get("APFSContainerReference")
            mount = pinfo.get("MountPoint") or p.get("MountPoint")
            num = int(re.sub(r"^.*s(\d+)$", r"\1", pid)) if re.search(r"s\d+$", pid) else idx
            part = {
                "id": f"part:{pid}", "disk": disk["id"], "number": num, "start": cursor, "size": psize,
                "typeGuid": type_guid, "mbrType": mbr_type, "typeName": type_name, "guid": (pinfo.get("DiskUUID") or "").lower() or None,
                "name": p.get("VolumeName") or pinfo.get("VolumeName") or None, "fs": fs, "fsSource": "os" if fs else None,
                "fsVersion": None, "label": p.get("VolumeName") or pinfo.get("VolumeName") or "", "uuid": pinfo.get("VolumeUUID") or None,
                "letter": None, "mountpoints": [mount] if mount else [],
                "used": None, "free": None, "volSize": None,
                "flags": {"boot": False, "system": False, "active": False, "hidden": bool(pinfo.get("OSInternal")), "readonly": pinfo.get("Writable") is False,
                          "esp": (p.get("Content") or pinfo.get("Content")) == "EFI"},
                "health": "", "device": f"/dev/{pid}", "rawDevice": f"/dev/r{pid}", "holders": [], "swapActive": False, "pagefile": False,
                "isBootVolume": pid in boot_stores, "approx": True, "content": p.get("Content") or pinfo.get("Content") or "",
            }
            if pinfo.get("VolumeSize") or pinfo.get("TotalSize"):
                vs = int(pinfo.get("VolumeSize") or pinfo.get("TotalSize") or 0)
                fr = int(pinfo.get("FreeSpace") or pinfo.get("VolumeFreeSpace") or 0) if pinfo.get("FreeSpace") is not None or pinfo.get("VolumeFreeSpace") is not None else None
                part["volSize"] = vs
                if fr is not None:
                    part["free"] = fr
                    part["used"] = max(0, vs - fr)
            if container and container in parsed["containers"]:
                c = parsed["containers"][container]
                vols = []
                for v in c["volumes"]:
                    vols.append({"device": v.get("DeviceIdentifier"), "name": v.get("VolumeName") or "", "used": int(v.get("CapacityInUse") or v.get("Size") or 0),
                                 "mountpoint": v.get("MountPoint") or "", "roles": v.get("Roles") or [], "internal": bool(v.get("OSInternal"))})
                part["apfsContainer"] = container
                part["apfsVolumes"] = vols
                part["fs"] = "apfs"
                part["fsSource"] = "os"
                part["label"] = part["label"] or ", ".join(v["name"] for v in vols if v["name"] and not v["internal"])[:60]
                part["mountpoints"] = [v["mountpoint"] for v in vols if v["mountpoint"]]
                part["used"] = sum(v["used"] for v in vols) or None
                part["volSize"] = c["size"] or psize
                if part["used"] is not None and part["volSize"]:
                    part["free"] = max(0, part["volSize"] - part["used"])
                if any(v["mountpoint"] == "/" for v in vols):
                    part["isBootVolume"] = True
                    disk["system"] = disk["boot"] = True
            disk["partitions"].append(part)
            cursor += psize
        disks.append(disk)
    inv = di.finish(disks, elevated=(os.geteuid() == 0) if hasattr(os, "geteuid") else False)
    inv["approx"] = True
    return inv


# ----------------------------------------------------------------------------
# Root-side refinement: exact starts and free space from gpt / fdisk
# ----------------------------------------------------------------------------
GPT_ROW = re.compile(r"^\s*(\d+)\s+(\d+)(?:\s+(\d+))?(?:\s+(.*?))?\s*$")   # free-space rows have no index and no contents


def parse_gpt_show(text: str) -> tuple[list[dict], int]:
    """Rows of `gpt -r show`: [{start, size, index|None, contents}] in 512-byte sectors, plus the sector size hint."""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("start"):
            continue
        m = GPT_ROW.match(line)
        if not m:
            continue
        start, size, idx, contents = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4) or ""
        rows.append({"start": start, "size": size, "index": int(idx) if idx else None, "contents": contents})
    return rows, 512


def refine_disk_with_gpt(disk: dict, text: str) -> None:
    rows, unit = parse_gpt_show(text)
    if not rows:
        return
    by_index = {r["index"]: r for r in rows if r["index"]}
    for p in disk.get("partitions", []):
        r = by_index.get(int(p["number"]))
        if r:
            p["start"] = r["start"] * unit
            p["size"] = r["size"] * unit
            p["end"] = p["start"] + p["size"]
            p["approx"] = False
            m = re.search(r"GPT part - ([0-9A-Fa-f-]{36})", r["contents"])
            if m and not p.get("typeGuid"):
                p["typeGuid"] = m.group(1).lower()
                p["typeName"] = dw_fs.gpt_type_name(p["typeGuid"])
    disk["approx"] = False


def parse_fdisk_dump(text: str) -> list[dict]:
    """`fdisk -d`: one line per slot 'start,size,id,active,...' in 512-byte sectors."""
    out = []
    for n, line in enumerate(l for l in text.splitlines() if l.strip() and "," in l):
        parts = [x.strip() for x in line.split(",")]
        try:
            out.append({"slot": n + 1, "start": int(parts[0]), "size": int(parts[1]), "id": int(parts[2], 0) if parts[2] else 0})
        except (ValueError, IndexError):
            continue
    return out


def refine_disk_with_fdisk(disk: dict, text: str) -> None:
    slots = {s["slot"]: s for s in parse_fdisk_dump(text) if s["size"]}
    for p in disk.get("partitions", []):
        s = slots.get(int(p["number"]))
        if s:
            p["start"] = s["start"] * 512
            p["size"] = s["size"] * 512
            p["end"] = p["start"] + p["size"]
            p["approx"] = False
            if p.get("mbrType") is None and s["id"]:
                p["mbrType"] = s["id"]
                p["typeName"] = dw_fs.mbr_type_name(s["id"])
    disk["approx"] = False


def refine(inv: dict, log=None) -> None:
    """Called in the root helper after mac_inventory(): exact geometry for every disk."""
    import dw_inventory as di
    for d in inv.get("disks", []):
        try:
            if d.get("table") == "gpt":
                code, out = run([GPT, "-r", "show", d["path"]], timeout=30, log=log, title="Partition map")
                if code == 0:
                    refine_disk_with_gpt(d, out)
            elif d.get("table") == "mbr":
                code, out = run([FDISK, "-d", d.get("rawPath") or d["path"]], timeout=30, log=log, title="Partition map")
                if code == 0:
                    refine_disk_with_fdisk(d, out)
        except Exception as e:
            d["refineError"] = str(e)
        d["partitions"].sort(key=lambda p: p["start"])
        di.compute_gaps(d)
        di.apply_protection(d)
    inv["approx"] = any(d.get("approx") for d in inv.get("disks", []))
    inv["hash"] = di.layout_hash(inv["disks"])


# ----------------------------------------------------------------------------
# Raw device access (/dev/rdiskN: sector-aligned only)
# ----------------------------------------------------------------------------
class RawDevice:
    def __init__(self, path: str, write: bool = False, sector: int = 512):
        if path.startswith("/dev/disk"):
            path = path.replace("/dev/disk", "/dev/rdisk", 1)
        self.path = path
        self.fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0))
        self.sector = sector
        try:
            buf = bytearray(4)
            fcntl.ioctl(self.fd, DKIOCGETBLOCKSIZE, buf)
            self.sector = struct.unpack("I", buf)[0] or sector
        except OSError:
            pass
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
            b1, b2 = bytearray(4), bytearray(8)
            fcntl.ioctl(self.fd, DKIOCGETBLOCKSIZE, b1)
            fcntl.ioctl(self.fd, DKIOCGETBLOCKCOUNT, b2)
            return struct.unpack("I", b1)[0] * struct.unpack("Q", b2)[0]
        except OSError:
            return 0

    def read(self, offset: int, length: int) -> bytes:
        s = self.sector
        a_off = offset - (offset % s)
        a_len = ((offset + length - a_off + s - 1) // s) * s
        data = bytearray()
        pos = a_off
        while len(data) < a_len:
            chunk = os.pread(self.fd, min(a_len - len(data), 1 << 20), pos)
            if not chunk:
                break
            data += chunk
            pos += len(chunk)
        skip = offset - a_off
        return bytes(data[skip: skip + length])

    def write_aligned(self, offset: int, mv) -> int:
        return os.pwrite(self.fd, mv, offset)

    def flush(self) -> None:
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def rescan(self) -> None:
        pass  # diskarbitrationd re-probes on close; the caller runs `diskutil mountDisk`


# ----------------------------------------------------------------------------
# Mounting, test disks, permissions
# ----------------------------------------------------------------------------
def unmount_disk(ident: str, log=None) -> None:
    code, out = run([DISKUTIL, "unmountDisk", "force", f"/dev/{ident}"], timeout=120, log=log, title="Unmount disk")
    if code != 0:
        raise RuntimeError("macOS could not unmount the disk: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))


def mount_disk(ident: str, log=None) -> None:
    run([DISKUTIL, "mountDisk", f"/dev/{ident}"], timeout=120, log=log, title="Mount disk")


def unmount(dev: str, log=None) -> None:
    code, out = run([DISKUTIL, "unmount", "force", dev], timeout=120, log=log, title="Unmount")
    if code != 0 and "not mounted" not in out.lower() and "was already unmounted" not in out.lower():
        raise RuntimeError("macOS could not unmount the volume: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))


def mount(dev: str, readonly: bool = False, log=None) -> str:
    argv = [DISKUTIL, "mount"] + (["readOnly"] if readonly else []) + [dev]
    code, out = run(argv, timeout=120, log=log, title="Mount")
    if code != 0:
        raise RuntimeError("macOS could not mount the volume: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))
    try:
        info = plist([DISKUTIL, "info", "-plist", dev])
        return info.get("MountPoint") or ""
    except Exception:
        m = re.search(r"mounted at (.+)$", out.strip(), re.M)
        return m.group(1).strip() if m else ""


def list_filesystems() -> list[str]:
    """Format names diskutil accepts (`diskutil listFilesystems -plist`), falling back to the known set."""
    try:
        data = plist([DISKUTIL, "listFilesystems", "-plist"])
        names = []
        for entry in data.get("FormattableFilesystems") or data.get("Filesystems") or []:
            if isinstance(entry, dict):
                n = entry.get("PersonalityName") or entry.get("FilesystemName") or entry.get("Name")
                if n:
                    names.append(str(n))
        if names:
            return names
    except Exception:
        pass
    return list(MAC_FORMATS.values()) + ["Free Space"]


def hdiutil_attach(path: str, log=None) -> str:
    """Attach a raw image file as a disk without mounting it; returns the diskN identifier."""
    code, out = run([HDIUTIL, "attach", "-nomount", "-imagekey", "diskimage-class=CRawDiskImage", path], timeout=120, log=log, title="Attach test disk")
    if code != 0:
        raise RuntimeError("hdiutil could not attach the image: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))
    m = re.search(r"(/dev/disk\d+)", out)
    if not m:
        raise RuntimeError("hdiutil attached the image but did not report a disk identifier.")
    return m.group(1).split("/")[-1]


def hdiutil_detach(ident: str, log=None) -> None:
    run([HDIUTIL, "detach", f"/dev/{ident}"], timeout=120, log=log, title="Detach test disk")


def tcc_probe(raw_path: str) -> str:
    """Try to open a raw device read-only from the window process, so macOS shows its
    'Removable Volumes' consent for the app (a root helper cannot raise the prompt itself).
    Returns 'ok', 'denied' or 'missing'."""
    try:
        fd = os.open(raw_path, os.O_RDONLY)
        os.close(fd)
        return "ok"
    except PermissionError:
        return "denied"
    except OSError:
        return "missing"


def open_full_disk_access_settings() -> None:
    subprocess.Popen([OPEN, FDA_URL], env=clean_env())


# ----------------------------------------------------------------------------
# Third-party tools the Access ladder can use
# ----------------------------------------------------------------------------
def which(name: str) -> str | None:
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/sbin", "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def macfuse_installed() -> bool:
    return os.path.isdir("/Library/Filesystems/macfuse.fs") or os.path.isdir("/Library/Filesystems/osxfuse.fs")


def macfuse_fskit_capable() -> bool:
    """macFUSE >= 5.1 offers `-o backend=fskit` (no kernel extension) on macOS 15.4+."""
    try:
        with open("/Library/Filesystems/macfuse.fs/Contents/version.plist", "rb") as f:
            ver = str(plistlib.load(f).get("CFBundleShortVersionString") or "0")
        major, minor = (int(x) for x in (ver.split(".") + ["0"])[:2])
        return (major, minor) >= (5, 1)
    except Exception:
        return False


def extendfs_installed() -> bool:
    return any(os.path.isdir(p) for p in ("/Applications/ExtendFS.app", os.path.expanduser("~/Applications/ExtendFS.app")))
