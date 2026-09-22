"""Disk inventory: what DiskWorks knows about the machine's disks, partitions,
volumes and unallocated space, on Windows and Linux, without elevation.

The result is a plain dict (JSON-ready) with this shape (ARCHITECTURE.md §2):

  {"ts", "platform", "hash", "elevated", "disks": [ {
      "id", "number", "name", "path", "model", "serial", "bus", "media", "size",
      "logicalSector", "physicalSector", "table", "removable", "hotplug", "system",
      "boot", "readonly", "offline", "health", "largestFree", "locked": [reasons],
      "partitions": [ {"id", "disk", "number", "start", "size", "end", "typeGuid",
                        "mbrType", "typeName", "guid", "name", "fs", "fsSource", "label",
                        "uuid", "letter", "mountpoints", "used", "free", "flags": {...},
                        "health", "locked": [reasons], "allow": [ops still allowed],
                        "device"} ],
      "gaps": [ {"id", "disk", "start", "size"} ],
      "segments": [ {"kind": "part"|"gap", "id", "start", "size"} ]   # drawing order
  } ] }

Windows: one PowerShell batch over the Storage Management CIM classes (what Disk
Management shows); falls back to the classic Win32_* classes when MSFT_Disk is
access-denied.  Linux: `lsblk -J -b -O` plus /sys for partition starts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import dw_fs

IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
PLATFORM = "win32" if IS_WIN else ("linux" if IS_LINUX else sys.platform)
MIN_GAP = 4 * 2**20          # unallocated regions smaller than this are alignment slack, not shown
MIN_ALIGN = 2**20            # partitions start and end on 1 MiB boundaries
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

BUS_NAMES = {0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "FireWire", 5: "SSA", 6: "Fibre Channel",
             7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 14: "Virtual",
             15: "File-backed virtual", 16: "Storage Spaces", 17: "NVMe", 18: "SCM", 19: "UFS"}
MEDIA_NAMES = {0: None, 3: "HDD", 4: "SSD", 5: "SCM"}
HEALTH_NAMES = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}
DRIVE_TYPES = {0: "Unknown", 1: "Invalid", 2: "Removable", 3: "Fixed", 4: "Remote", 5: "CD-ROM", 6: "RAM disk"}


# ----------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------
def layout_hash(disks: list[dict]) -> str:
    """Hash of everything a partition-table write would change; compared before each write."""
    h = hashlib.sha1()
    for d in disks:
        h.update(f"{d['id']}|{d.get('table')}|{d.get('size')}|".encode())
        for p in d.get("partitions", []):
            h.update(f"{p['start']}:{p['size']}:{p.get('typeGuid') or p.get('mbrType')}|".encode())
        h.update(b";")
    return h.hexdigest()[:16]


def _gpt_reserve(logical: int) -> tuple[int, int]:
    """Bytes reserved at the head (MBR + header + 128 entries) and tail (entries + header) of a GPT disk."""
    entries = 128 * 128
    head = 2 * logical + ((entries + logical - 1) // logical) * logical
    tail = logical + ((entries + logical - 1) // logical) * logical
    return head, tail


def compute_gaps(disk: dict) -> None:
    """Fill disk['gaps'] and disk['segments'] from its partitions and table type."""
    logical = int(disk.get("logicalSector") or 512)
    size = int(disk.get("size") or 0)
    table = disk.get("table")
    # Gaps end on the 1 MiB boundary a partition could actually reach (below the GPT backup
    # header), so the size shown is the size that can be used.
    if table == "gpt":
        first, tail = _gpt_reserve(logical)
        last = (size - tail) - ((size - tail) % MIN_ALIGN)
    elif table == "mbr":
        first, last = logical, size - (size % MIN_ALIGN)
    else:
        first, last = 0, size
    parts = sorted(disk.get("partitions", []), key=lambda p: p["start"])
    # Logical partitions live inside an extended one: exclude the container from gap math.
    containers = [p for p in parts if p.get("mbrType") in (0x05, 0x0f, 0x85)]
    usable = [p for p in parts if p not in containers]
    gaps: list[dict] = []
    segments: list[dict] = []
    cursor = first
    if table in ("none", "unknown") and not parts and size:
        gaps.append({"id": f"gap:{disk['id']}:0", "disk": disk["id"], "start": 0, "size": size})
        segments.append({"kind": "gap", "id": gaps[0]["id"], "start": 0, "size": size})
    else:
        for p in usable:
            if p["start"] - cursor >= MIN_GAP:
                g = {"id": f"gap:{disk['id']}:{cursor}", "disk": disk["id"], "start": cursor, "size": p["start"] - cursor}
                gaps.append(g)
                segments.append({"kind": "gap", "id": g["id"], "start": g["start"], "size": g["size"]})
            segments.append({"kind": "part", "id": p["id"], "start": p["start"], "size": p["size"]})
            cursor = max(cursor, p["start"] + p["size"])
        if last - cursor >= MIN_GAP:
            g = {"id": f"gap:{disk['id']}:{cursor}", "disk": disk["id"], "start": cursor, "size": last - cursor}
            gaps.append(g)
            segments.append({"kind": "gap", "id": g["id"], "start": g["start"], "size": g["size"]})
    disk["gaps"] = gaps
    disk["segments"] = segments


def apply_protection(disk: dict) -> None:
    """Fill the 'locked' reasons and 'allow' lists (ARCHITECTURE.md §4 protection set)."""
    dlock: list[str] = []
    if disk.get("system") or disk.get("boot"):
        dlock.append("Holds the running operating system")
    if disk.get("readonly"):
        dlock.append("The disk is read-only")
    if disk.get("offline"):
        dlock.append("The disk is offline")
    if disk.get("clustered"):
        dlock.append("Cluster disk")
    disk["locked"] = dlock
    for p in disk.get("partitions", []):
        lock: list[str] = []
        allow: list[str] = []
        tg = (p.get("typeGuid") or "").lower()
        if tg in dw_fs.PROTECTED_GPT:
            lock.append(dw_fs.PROTECTED_GPT[tg])
        if p.get("mbrType") in dw_fs.PROTECTED_MBR:
            lock.append(dw_fs.PROTECTED_MBR[p["mbrType"]])
        fl = p.get("flags") or {}
        if fl.get("system") and "EFI System partition (the computer boots from it)" not in lock:
            lock.append("System partition (holds the boot files)")
        if fl.get("boot") or p.get("isBootVolume"):
            # The running OS volume: Windows lets you shrink/grow it online; nothing else.
            lock.append("Holds the running operating system")
            allow = ["resize", "label", "check"]
        if p.get("fs") in ("bitlocker",):
            lock.append("BitLocker encrypted volume (decrypt it in Windows first)")
        if p.get("fs") in ("luks",):
            lock.append("Encrypted (LUKS) container")
            allow = ["open"]
        if p.get("fs") in ("lvm", "raid"):
            lock.append(f"Member of a {dw_fs.fs_label(p['fs'])} set")
        if p.get("holders"):
            lock.append("In use by " + ", ".join(p["holders"]))
        if p.get("pagefile"):
            lock.append("Holds a Windows page file")
            allow = ["resize", "label", "check"]
        if p.get("swapActive"):
            lock.append("Active swap space (swapoff first)")
        if disk.get("readonly"):
            lock.append("The disk is read-only")
        p["locked"] = lock
        p["allow"] = allow if lock else []


def finish(disks: list[dict], elevated: bool = False) -> dict:
    for d in disks:
        d.setdefault("partitions", [])
        for p in d["partitions"]:
            p["end"] = p["start"] + p["size"]
            p.setdefault("flags", {})
            p.setdefault("mountpoints", [])
        d["partitions"].sort(key=lambda p: p["start"])
        compute_gaps(d)
        apply_protection(d)
    disks.sort(key=lambda d: (d.get("number") if isinstance(d.get("number"), int) else 1e9, d["name"]))
    return {"ts": time.time(), "platform": PLATFORM, "hash": layout_hash(disks), "elevated": elevated, "disks": disks}


# ----------------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------------
PS_STORAGE = r'''
$ErrorActionPreference = 'Stop'
$ns = 'root/Microsoft/Windows/Storage'
$d = @(Get-CimInstance -Namespace $ns -ClassName MSFT_Disk | Select-Object Number,Path,FriendlyName,Model,Manufacturer,SerialNumber,BusType,Size,LogicalSectorSize,PhysicalSectorSize,PartitionStyle,IsSystem,IsBoot,IsReadOnly,IsOffline,IsClustered,LargestFreeExtent,NumberOfPartitions,UniqueId,Location,Guid,Signature,HealthStatus,OfflineReason)
$p = @(Get-CimInstance -Namespace $ns -ClassName MSFT_Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,AccessPaths,Offset,Size,GptType,MbrType,Guid,IsActive,IsBoot,IsSystem,IsHidden,IsReadOnly,IsOffline,NoDefaultDriveLetter,IsShadowCopy,Type)
$v = @(Get-CimInstance -Namespace $ns -ClassName MSFT_Volume | Select-Object DriveLetter,Path,FileSystem,FileSystemType,FileSystemLabel,Size,SizeRemaining,DriveType,HealthStatus,UniqueId,AllocationUnitSize)
$pd = @(Get-CimInstance -Namespace $ns -ClassName MSFT_PhysicalDisk | Select-Object DeviceId,FriendlyName,SerialNumber,MediaType,BusType,Size,SpindleSpeed,Model,FirmwareVersion,HealthStatus)
$pf = @(Get-CimInstance -ClassName Win32_PageFileUsage | Select-Object Name)
@{disks=$d;parts=$p;vols=$v;phys=$pd;pagefiles=$pf} | ConvertTo-Json -Depth 5 -Compress
'''

PS_CLASSIC = r'''
$ErrorActionPreference = 'Stop'
$d = @(Get-CimInstance Win32_DiskDrive | Select-Object Index,DeviceID,Model,SerialNumber,Size,InterfaceType,MediaType,BytesPerSector,Partitions,Status,PNPDeviceID)
$p = @(Get-CimInstance Win32_DiskPartition | Select-Object DiskIndex,Index,DeviceID,StartingOffset,Size,Type,Bootable,BootPartition,PrimaryPartition,Description)
$l = @(Get-CimInstance Win32_LogicalDiskToPartition | ForEach-Object { @{part=$_.Antecedent.DeviceID; disk=$_.Dependent.DeviceID} })
$v = @(Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,FileSystem,VolumeName,Size,FreeSpace,VolumeSerialNumber,DriveType)
@{disks=$d;parts=$p;links=$l;vols=$v} | ConvertTo-Json -Depth 5 -Compress
'''


def run_powershell(script: str, timeout: float = 40.0) -> tuple[int, str, str]:
    """Run a PowerShell script (passed encoded, so quoting is never an issue)."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out = r.stdout.decode("utf-8", "replace") if r.stdout else ""
    err = r.stderr.decode("utf-8", "replace") if r.stderr else ""
    return r.returncode, out, err


def _aslist(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _letter(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, int):
        return chr(v).upper() if 65 <= (v & 0xDF) <= 90 else None
    s = str(v).strip("\x00 ")
    return s[0].upper() if s and s[0].isalpha() else None


def _cim_bool(v) -> bool:
    return bool(v) if not isinstance(v, str) else v.lower() == "true"


def win_inventory() -> dict:
    code, out, err = run_powershell(PS_STORAGE)
    if code == 0 and out.strip().startswith("{"):
        try:
            data = json.loads(out)
            return _win_from_storage(data)
        except ValueError:
            pass
    # Standard users may be refused MSFT_Disk; fall back to the classic classes.
    code2, out2, err2 = run_powershell(PS_CLASSIC)
    if code2 == 0 and out2.strip().startswith("{"):
        inv = _win_from_classic(json.loads(out2))
        inv["note"] = "Reduced detail: the Storage Management classes refused access (" + (err.strip().splitlines() or ["?"])[0][:160] + ")"
        return inv
    raise RuntimeError("Windows did not return the disk list: " + (err or err2 or "no output").strip().splitlines()[0][:200])


def _win_from_storage(data: dict) -> dict:
    vols_by_path: dict[str, dict] = {}
    for v in _aslist(data.get("vols")):
        path = (v.get("Path") or "").rstrip("\\").lower()
        if path:
            vols_by_path[path] = v
    phys_by_id = {str(x.get("DeviceId")): x for x in _aslist(data.get("phys"))}
    pagefile_letters = set()
    for pf in _aslist(data.get("pagefiles")):
        n = str(pf.get("Name") or "")
        if len(n) >= 2 and n[1] == ":":
            pagefile_letters.add(n[0].upper())
    disks: list[dict] = []
    parts_by_disk: dict[int, list[dict]] = {}
    for p in _aslist(data.get("parts")):
        parts_by_disk.setdefault(int(p.get("DiskNumber")), []).append(p)
    for d in _aslist(data.get("disks")):
        num = int(d.get("Number"))
        style = int(d.get("PartitionStyle") or 0)
        table = {1: "mbr", 2: "gpt"}.get(style, "none")
        ph = phys_by_id.get(str(num)) or {}
        bus = BUS_NAMES.get(int(d.get("BusType") or 0), "Unknown")
        disk = {
            "id": f"disk:{num}", "number": num, "name": f"Disk {num}", "path": f"\\\\.\\PhysicalDrive{num}",
            "model": (d.get("FriendlyName") or d.get("Model") or "").strip(),
            "serial": (d.get("SerialNumber") or ph.get("SerialNumber") or "").strip(),
            "bus": bus, "media": MEDIA_NAMES.get(int(ph.get("MediaType") or 0)),
            "size": int(d.get("Size") or 0), "logicalSector": int(d.get("LogicalSectorSize") or 512),
            "physicalSector": int(d.get("PhysicalSectorSize") or 512), "table": table,
            "removable": False,     # refined below from the volumes' DriveType
            "hotplug": bus in ("USB", "SD", "MMC", "FireWire"),
            "system": _cim_bool(d.get("IsSystem")), "boot": _cim_bool(d.get("IsBoot")),
            "readonly": _cim_bool(d.get("IsReadOnly")), "offline": _cim_bool(d.get("IsOffline")),
            "clustered": _cim_bool(d.get("IsClustered")),
            "health": HEALTH_NAMES.get(int(d.get("HealthStatus") or 0), "Unknown"),
            "location": d.get("Location") or "", "uniqueId": d.get("UniqueId") or "",
            "largestFree": int(d.get("LargestFreeExtent") or 0), "guid": (d.get("Guid") or "").strip("{}"),
            "partitions": [],
        }
        for p in parts_by_disk.get(num, []):
            pnum = int(p.get("PartitionNumber") or 0)
            gpt = (p.get("GptType") or "").strip("{}").lower() or None
            mbr = int(p.get("MbrType")) if p.get("MbrType") not in (None, "") else None
            letter = _letter(p.get("DriveLetter"))
            aps = [a for a in _aslist(p.get("AccessPaths")) if a]
            vol = None
            for a in aps:
                vol = vols_by_path.get(a.rstrip("\\").lower())
                if vol:
                    break
            fs = dw_fs.normalize_fs(vol.get("FileSystem")) if vol else None
            if table == "gpt":
                type_name = dw_fs.gpt_type_name(gpt)
            else:
                type_name = dw_fs.mbr_type_name(mbr)
            mounts = [a for a in aps if not a.startswith("\\\\?\\")]
            part = {
                "id": f"part:{num}:{pnum}", "disk": disk["id"], "number": pnum,
                "start": int(p.get("Offset") or 0), "size": int(p.get("Size") or 0),
                "typeGuid": gpt, "mbrType": mbr, "typeName": type_name, "winType": p.get("Type") or "",
                "guid": (p.get("Guid") or "").strip("{}") or None, "name": None,
                "fs": fs, "fsSource": "os" if fs else None,
                "label": (vol.get("FileSystemLabel") if vol else None) or "",
                "uuid": None, "letter": letter, "mountpoints": mounts,
                "used": (int(vol["Size"]) - int(vol["SizeRemaining"])) if vol and vol.get("Size") else None,
                "free": int(vol["SizeRemaining"]) if vol and vol.get("SizeRemaining") is not None else None,
                "volSize": int(vol["Size"]) if vol and vol.get("Size") else None,
                "flags": {"boot": _cim_bool(p.get("IsBoot")), "system": _cim_bool(p.get("IsSystem")),
                          "active": _cim_bool(p.get("IsActive")), "hidden": _cim_bool(p.get("IsHidden")),
                          "readonly": _cim_bool(p.get("IsReadOnly")), "esp": gpt == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                          "noletter": _cim_bool(p.get("NoDefaultDriveLetter")), "shadow": _cim_bool(p.get("IsShadowCopy"))},
                "health": HEALTH_NAMES.get(int(vol.get("HealthStatus") or 0), "") if vol else "",
                "driveType": DRIVE_TYPES.get(int(vol.get("DriveType") or 0)) if vol else None,
                "clusterSize": int(vol.get("AllocationUnitSize") or 0) if vol else None,
                "device": aps[0] if aps else None,
                "pagefile": bool(letter and letter in pagefile_letters),
                "isBootVolume": _cim_bool(p.get("IsBoot")),
            }
            if vol and vol.get("DriveType") == 2:
                disk["removable"] = True
            if vol and (vol.get("FileSystem") or "").upper() == "RAW" and part["size"]:
                part["fs"] = None
                part["fsSource"] = "raw"
            disk["partitions"].append(part)
        disks.append(disk)
    return finish(disks)


def _win_from_classic(data: dict) -> dict:
    vols = {v.get("DeviceID"): v for v in _aslist(data.get("vols"))}
    links: dict[str, str] = {}
    for l in _aslist(data.get("links")):
        links[str(l.get("part"))] = str(l.get("disk"))
    disks: list[dict] = []
    parts_by_disk: dict[int, list[dict]] = {}
    for p in _aslist(data.get("parts")):
        parts_by_disk.setdefault(int(p.get("DiskIndex")), []).append(p)
    for d in _aslist(data.get("disks")):
        num = int(d.get("Index"))
        iface = (d.get("InterfaceType") or "").upper()
        disk = {
            "id": f"disk:{num}", "number": num, "name": f"Disk {num}", "path": f"\\\\.\\PhysicalDrive{num}",
            "model": (d.get("Model") or "").strip(), "serial": (d.get("SerialNumber") or "").strip(),
            "bus": {"USB": "USB", "SCSI": "SCSI", "IDE": "ATA", "HDC": "ATA", "1394": "FireWire"}.get(iface, iface or "Unknown"),
            "media": None, "size": int(d.get("Size") or 0), "logicalSector": int(d.get("BytesPerSector") or 512),
            "physicalSector": int(d.get("BytesPerSector") or 512), "table": "unknown",
            "removable": "removable" in (d.get("MediaType") or "").lower(), "hotplug": iface == "USB",
            "system": False, "boot": False, "readonly": False, "offline": False, "clustered": False,
            "health": d.get("Status") or "", "location": "", "uniqueId": d.get("PNPDeviceID") or "",
            "largestFree": 0, "guid": "", "partitions": [],
        }
        for p in parts_by_disk.get(num, []):
            pid = str(p.get("DeviceID"))
            vol = vols.get(links.get(pid))
            desc = (p.get("Type") or "")
            is_gpt = "GPT" in desc.upper()
            if is_gpt:
                disk["table"] = "gpt"
            elif disk["table"] == "unknown" and desc:
                disk["table"] = "mbr"
            fs = dw_fs.normalize_fs(vol.get("FileSystem")) if vol else None
            letter = (vol.get("DeviceID") or "")[:1] if vol else None
            if _cim_bool(p.get("BootPartition")):
                disk["boot"] = True
                disk["system"] = True
            part = {
                "id": f"part:{num}:{int(p.get('Index')) + 1}", "disk": disk["id"], "number": int(p.get("Index")) + 1,
                "start": int(p.get("StartingOffset") or 0), "size": int(p.get("Size") or 0),
                "typeGuid": None, "mbrType": None, "typeName": desc, "winType": desc, "guid": None, "name": None,
                "fs": fs, "fsSource": "os" if fs else None, "label": (vol.get("VolumeName") if vol else None) or "",
                "uuid": (vol.get("VolumeSerialNumber") if vol else None), "letter": letter or None,
                "mountpoints": [f"{letter}:\\"] if letter else [],
                "used": (int(vol["Size"]) - int(vol["FreeSpace"])) if vol and vol.get("Size") else None,
                "free": int(vol["FreeSpace"]) if vol and vol.get("FreeSpace") is not None else None,
                "flags": {"boot": _cim_bool(p.get("BootPartition")), "system": _cim_bool(p.get("Bootable")),
                          "active": _cim_bool(p.get("Bootable")), "hidden": False, "readonly": False, "esp": "EFI" in desc.upper()},
                "health": "", "device": f"\\\\.\\{letter}:" if letter else None, "pagefile": False,
                "isBootVolume": _cim_bool(p.get("BootPartition")),
            }
            disk["partitions"].append(part)
        disks.append(disk)
    return finish(disks)


# ----------------------------------------------------------------------------
# Linux
# ----------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C"))
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _sys_read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _mount_source(target: str) -> str | None:
    code, out = _run(["findmnt", "-n", "-o", "SOURCE", "--target", target])
    if code != 0 or not out.strip():
        return None
    src = out.strip().splitlines()[0].strip()
    # strip btrfs subvolume suffix "/dev/sda2[/@]"
    return re.sub(r"\[.*\]$", "", src)


def _realdev(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def linux_inventory() -> dict:
    code, out = _run(["lsblk", "-J", "-b", "-O"])
    if code != 0 or not out.strip():
        code, out = _run(["lsblk", "-J", "-b", "-o",
                          "NAME,KNAME,PATH,MAJ:MIN,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,PARTTYPE,PARTTYPENAME,PARTLABEL,PARTUUID,PARTFLAGS,MOUNTPOINT,MOUNTPOINTS,FSAVAIL,FSUSED,FSSIZE,MODEL,SERIAL,VENDOR,REV,TRAN,RM,RO,ROTA,HOTPLUG,PTTYPE,PTUUID,START,LOG-SEC,PHY-SEC,WWN"])
    if code != 0 or not out.strip():
        raise RuntimeError("lsblk did not return the disk list (util-linux is required)")
    data = json.loads(out)
    devices = data.get("blockdevices") or []

    # Protection set: devices behind the running system.
    protected_leaf: set[str] = set()
    for tgt in ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home"):
        src = _mount_source(tgt)
        if src and src.startswith("/dev/"):
            protected_leaf.add(_realdev(src))
    swaps: set[str] = set()
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                dev = line.split()[0]
                if dev.startswith("/dev/"):
                    swaps.add(_realdev(dev))
    except OSError:
        pass
    live = any(os.path.isdir(p) for p in ("/run/live/medium", "/run/initramfs/live", "/run/archiso")) or \
        ("boot=live" in (_sys_read("/proc/cmdline") or ""))

    def walk(dev: dict, parent: dict | None, disk: dict | None, out_disks: list[dict]):
        t = dev.get("type") or ""
        name = dev.get("name") or dev.get("kname")
        path = dev.get("path") or f"/dev/{name}"
        if t in ("disk", "loop") and parent is None:
            if t == "loop" and (dev.get("fstype") or "").lower() == "squashfs":
                return   # snap / live-media images are not disks the user manages
            size = _int(dev.get("size"))
            if size <= 0:
                return
            pttype = (dev.get("pttype") or "").lower()
            table = {"gpt": "gpt", "dos": "mbr", "mbr": "mbr"}.get(pttype, "none" if not pttype else pttype)
            tran = (dev.get("tran") or "").upper() or ("VIRTUAL" if t == "loop" else "")
            d = {
                "id": f"disk:{name}", "number": None, "name": name, "path": path,
                "model": (dev.get("model") or "").strip() or ((dev.get("vendor") or "").strip() + " " + name).strip(),
                "serial": (dev.get("serial") or "").strip(), "bus": {"NVME": "NVMe", "SATA": "SATA", "USB": "USB", "SAS": "SAS",
                                                                     "ATA": "ATA", "SPI": "SPI", "MMC": "MMC", "SD": "SD",
                                                                     "VIRTUAL": "Virtual", "": "Unknown"}.get(tran, tran),
                "media": None if dev.get("rota") is None else ("HDD" if dev.get("rota") in (True, "1", 1) else "SSD"),
                "size": size, "logicalSector": _int(dev.get("log-sec"), 512), "physicalSector": _int(dev.get("phy-sec"), 512),
                "table": table, "removable": dev.get("rm") in (True, "1", 1), "hotplug": dev.get("hotplug") in (True, "1", 1),
                "system": False, "boot": False, "readonly": dev.get("ro") in (True, "1", 1), "offline": False, "clustered": False,
                "health": "", "location": "", "uniqueId": dev.get("wwn") or "", "largestFree": 0, "guid": dev.get("ptuuid") or "",
                "partitions": [], "live": live,
            }
            if _realdev(path) in protected_leaf or _realdev(path) in swaps:
                d["system"] = d["boot"] = True
            out_disks.append(d)
            for ch in dev.get("children") or []:
                walk(ch, dev, d, out_disks)
            # a whole-disk filesystem (no partition table): show it as one volume
            if dev.get("fstype") and not dev.get("children"):
                fs = dw_fs.normalize_fs(dev.get("fstype"))
                d["wholeDiskFs"] = fs
                mps = dev.get("mountpoints")
                if mps is None:
                    mps = [dev.get("mountpoint")] if dev.get("mountpoint") else []
                mps = [m for m in mps if m]
                real = _realdev(path)
                d["partitions"].append({
                    "id": f"part:{name}:whole", "disk": d["id"], "number": 0, "start": 0, "size": size,
                    "typeGuid": None, "mbrType": None, "typeName": "Whole-disk filesystem", "guid": None, "name": None,
                    "fs": fs, "fsSource": "os", "fsVersion": dev.get("fsver") or None, "label": dev.get("label") or "",
                    "uuid": dev.get("uuid") or None, "letter": None, "mountpoints": mps,
                    "used": _int(dev.get("fsused")) if dev.get("fsused") is not None else None,
                    "free": _int(dev.get("fsavail")) if dev.get("fsavail") is not None else None,
                    "volSize": _int(dev.get("fssize")) if dev.get("fssize") is not None else None,
                    "flags": {"boot": real in protected_leaf and "/" in mps, "system": False, "active": False,
                              "hidden": False, "readonly": d["readonly"], "esp": False},
                    "health": "", "device": path, "holders": [], "swapActive": real in swaps, "pagefile": False,
                    "isBootVolume": real in protected_leaf and "/" in mps, "whole": True,
                })
            return
        if t == "part" and disk is not None:
            start_sectors = _int(dev.get("start"))
            if not start_sectors:
                start_sectors = _int(_sys_read(f"/sys/class/block/{name}/start"))
            mps = dev.get("mountpoints")
            if mps is None:
                mps = [dev.get("mountpoint")] if dev.get("mountpoint") else []
            mps = [m for m in mps if m]
            fs = dw_fs.normalize_fs(dev.get("fstype"))
            ptype = (dev.get("parttype") or "").lower() or None
            gpt = ptype if disk["table"] == "gpt" and ptype and len(ptype) > 4 else None
            mbr = int(ptype, 16) if disk["table"] == "mbr" and ptype and len(ptype) <= 4 else None
            holders = []
            try:
                holders = sorted(os.listdir(f"/sys/class/block/{name}/holders"))
            except OSError:
                pass
            children = [c.get("type") for c in (dev.get("children") or [])]
            real = _realdev(path)
            num = int(re.sub(r"^.*?(\d+)$", r"\1", name)) if re.search(r"\d+$", name) else len(disk["partitions"]) + 1
            p = {
                "id": f"part:{name}", "disk": disk["id"], "number": num,
                "start": start_sectors * 512, "size": _int(dev.get("size")),
                "typeGuid": gpt, "mbrType": mbr,
                "typeName": dev.get("parttypename") or (dw_fs.gpt_type_name(gpt) if gpt else dw_fs.mbr_type_name(mbr)),
                "guid": dev.get("partuuid") or None, "name": dev.get("partlabel") or None,
                "fs": fs, "fsSource": "os" if fs else None, "fsVersion": dev.get("fsver") or None,
                "label": dev.get("label") or "", "uuid": dev.get("uuid") or None, "letter": None,
                "mountpoints": mps, "used": _int(dev.get("fsused")) if dev.get("fsused") is not None else None,
                "free": _int(dev.get("fsavail")) if dev.get("fsavail") is not None else None,
                "volSize": _int(dev.get("fssize")) if dev.get("fssize") is not None else None,
                "flags": {"boot": real in protected_leaf and any(m == "/" for m in mps), "system": False,
                          "active": "boot" in (dev.get("partflags") or "").lower() or "0x80" in (dev.get("partflags") or ""),
                          "hidden": False, "readonly": dev.get("ro") in (True, "1", 1),
                          "esp": gpt == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" or mbr == 0xef},
                "health": "", "device": path, "holders": [h for h in holders] + [c for c in children if c in ("crypt", "lvm", "raid1", "raid0", "raid5", "raid6", "raid10", "dm")],
                "swapActive": real in swaps, "pagefile": False,
                "isBootVolume": real in protected_leaf and "/" in mps,
            }
            if real in protected_leaf:
                disk["system"] = disk["boot"] = True
            disk["partitions"].append(p)
            return

    disks: list[dict] = []
    for dev in devices:
        walk(dev, None, None, disks)
    return finish(disks, elevated=(hasattr(os, "geteuid") and os.geteuid() == 0))


# ----------------------------------------------------------------------------
def inventory() -> dict:
    if IS_WIN:
        return win_inventory()
    if IS_LINUX:
        return linux_inventory()
    if sys.platform == "darwin":
        import dw_mac
        return dw_mac.mac_inventory()
    raise RuntimeError(f"DiskWorks does not support {sys.platform}")


def load_fixture(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    disks = data["disks"] if isinstance(data, dict) else data
    return finish(disks)


if __name__ == "__main__":
    inv = load_fixture(sys.argv[1]) if len(sys.argv) > 1 else inventory()
    print(json.dumps(inv, indent=1))
