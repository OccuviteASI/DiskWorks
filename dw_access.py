"""The foreign-filesystem ladder (ARCHITECTURE.md §8): the ways to make a partition
readable and writable from this computer, evaluated in order, with every rung explained.

Three ladders (Windows, Linux, macOS), plus the read-only browser that works everywhere
7-Zip is available (partition device opened directly; a temporary image as fallback).
Where only a commercial driver would add write access, the ladder names it with its
website and installs nothing.

Window-process side: `Access` (evaluation without side effects, routes, bookkeeping of
what DiskWorks itself opened, the copy-out job).  Helper side: `helper_verbs()`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

import dw_fs
import dw_inventory as di

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
WINDOWS_NATIVE = {"ntfs", "exfat", "fat32", "fat16", "refs"}
MAC_NATIVE = {"apfs", "hfsplus", "exfat", "fat32", "fat16"}
WSL_TYPES = {"ext4": "ext4", "ext3": "ext3", "ext2": "ext2", "btrfs": "btrfs", "xfs": "xfs", "fat32": "vfat", "fat16": "vfat"}
KERNEL_NAMES = {"ntfs": "ntfs3", "exfat": "exfat", "fat32": "vfat", "fat16": "vfat", "ext4": "ext4", "ext3": "ext3", "ext2": "ext2",
                "btrfs": "btrfs", "xfs": "xfs", "f2fs": "f2fs", "hfsplus": "hfsplus", "apfs": "apfs", "iso9660": "iso9660"}
SEVENZIP_FS = {"apfs", "hfsplus", "ext4", "ext3", "ext2", "ntfs", "fat32", "fat16", "iso9660"}   # what 7-Zip can read
TEMP_IMAGE_LIMIT = 8 * 2**30
DRIVERS = {
    "btrfs": {"name": "WinBtrfs", "service": "btrfs", "fsname": "Btrfs", "url": "https://github.com/maharmstone/btrfs",
              "note": "Open-source, signed kernel driver with full read/write. Once installed, btrfs partitions get a drive letter like any other."},
    "ext4": {"name": "Ext4Fsd", "service": "Ext2Fsd", "fsname": "EXT2", "url": "https://github.com/bobranten/Ext4Fsd",
             "note": "Open-source driver. Newer ext4 features make it mount read-only, so it is still being evaluated (see the requirements ledger D-009)."},
    "ext3": {"name": "Ext4Fsd", "service": "Ext2Fsd", "fsname": "EXT2", "url": "https://github.com/bobranten/Ext4Fsd", "note": ""},
    "ext2": {"name": "Ext4Fsd", "service": "Ext2Fsd", "fsname": "EXT2", "url": "https://github.com/bobranten/Ext4Fsd", "note": ""},
}
# Commercial products that would add write access. Named with their website; DiskWorks never installs them.
COMMERCIAL = {
    ("win32", "apfs"): [("Paragon APFS for Windows", "https://www.paragon-software.com/home/apfs-windows/", "read; write marked experimental by the vendor"),
                        ("OWC MacDrive", "https://macdrive.com/", "read/write HFS+ and APFS")],
    ("win32", "hfsplus"): [("Paragon HFS+ for Windows", "https://www.paragon-drivers.com/en/hfswin/", "read/write"),
                           ("OWC MacDrive", "https://macdrive.com/", "read/write HFS+ and APFS")],
    ("linux", "apfs"): [("Paragon APFS for Linux", "https://www.paragon-software.com/business/apfs-linux/", "read/write")],
    ("darwin", "ntfs"): [("Paragon NTFS for Mac", "https://www.paragon-software.com/home/ntfs-mac/", "read/write"),
                         ("Tuxera NTFS for Mac", "https://ntfsformac.tuxera.com/", "read/write")],
    ("darwin", "ext4"): [("Paragon extFS for Mac", "https://www.paragon-software.com/home/extfs-mac/", "read/write ext2/3/4")],
    ("darwin", "ext3"): [("Paragon extFS for Mac", "https://www.paragon-software.com/home/extfs-mac/", "read/write ext2/3/4")],
    ("darwin", "ext2"): [("Paragon extFS for Mac", "https://www.paragon-software.com/home/extfs-mac/", "read/write ext2/3/4")],
}


def platform_key() -> str:
    return "win32" if IS_WIN else ("darwin" if IS_MAC else "linux")


# ----------------------------------------------------------------------------
# Unprivileged probes
# ----------------------------------------------------------------------------
def windows_service_exists(name: str) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}"):
            return True
    except OSError:
        return False


def windows_letters_by_fs() -> dict[str, list[str]]:
    """{'Btrfs': ['F'], 'NTFS': ['C', ...]} via GetVolumeInformationW (no elevation)."""
    out: dict[str, list[str]] = {}
    if not IS_WIN:
        return out
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i)
        fsname = ctypes.create_unicode_buffer(64)
        ok = k32.GetVolumeInformationW(f"{letter}:\\", None, 0, None, None, None, fsname, 64)
        if ok:
            out.setdefault(fsname.value, []).append(letter)
    return out


_wsl_cache: tuple[float, dict] | None = None


def wsl_state_cached() -> dict:
    global _wsl_cache
    if _wsl_cache and time.time() - _wsl_cache[0] < 60:
        return _wsl_cache[1]
    import dw_win
    try:
        st = dw_win.wsl_state()
    except Exception as e:
        st = {"installed": False, "distros": [], "mountable": False, "why": str(e)}
    _wsl_cache = (time.time(), st)
    return st


def resource_dir() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def sevenzip() -> str | None:
    """The bundled (or installed) 7-Zip command-line tool."""
    if IS_WIN:
        p = os.path.join(resource_dir(), "bin", "win64", "7zip", "7z.exe")
        return p if os.path.isfile(p) else (shutil.which("7z") or None)
    if IS_MAC:
        import dw_mac
        return dw_mac.which("7zz") or dw_mac.which("7z")
    import dw_linux
    return dw_linux.tool("7zz") or dw_linux.tool("7z")


def browser_device(disk: dict, part: dict) -> str:
    """What 7-Zip opens: the partition device (no volume or mount needed)."""
    if IS_WIN:
        return f"\\\\.\\Harddisk{int(disk['number'])}Partition{int(part['number'])}"
    if IS_MAC:
        return part.get("rawDevice") or part.get("device") or ""
    return part.get("device") or ""


# ----------------------------------------------------------------------------
# Ladder evaluation
# ----------------------------------------------------------------------------
def rung(id_: str, title: str, state: str, why: str, action: dict | None = None, **extra) -> dict:
    """state: ok (can do now) | done (already the case) | no (impossible here) | later (planned) | info (a pointer, no action)"""
    r = {"id": id_, "title": title, "state": state, "why": why, "action": action}
    r.update(extra)
    return r


def commercial_rung(fs: str | None) -> dict | None:
    items = COMMERCIAL.get((platform_key(), fs or ""))
    if not items:
        return None
    why = "Full read/write needs a commercial driver; DiskWorks does not install these: " + "; ".join(f"{n} ({note}) {u}" for n, u, note in items)
    return rung("commercial", "Commercial drivers", "info", why, links=[{"name": n, "url": u, "note": note} for n, u, note in items])


def browser_rung(fs: str | None, disk: dict, part: dict) -> dict:
    sz = sevenzip()
    if fs in SEVENZIP_FS and sz:
        return rung("browser", "DiskWorks read-only browser (7-Zip)", "ok",
                    "Browse the files and copy them out without any driver; nothing on the partition is changed.",
                    action={"verb": "browse", "disk": disk["id"], "start": part["start"]}, browse=True)
    if fs in SEVENZIP_FS:
        return rung("browser", "DiskWorks read-only browser (7-Zip)", "no", "7-Zip is not bundled in this build" + (" — install it with `brew install 7zip`." if IS_MAC else "."))
    return rung("browser", "DiskWorks read-only browser", "no", f"7-Zip cannot read {dw_fs.fs_label(fs) if fs else 'this filesystem'}.")


def evaluate(inv: dict, part_id: str, mounted: dict) -> dict:
    disk, part = None, None
    for d in inv.get("disks", []):
        for p in d.get("partitions", []):
            if p["id"] == part_id:
                disk, part = d, p
    if not part:
        raise RuntimeError("That partition is no longer present.")
    fs = part.get("fs")
    label = dw_fs.fs_label(fs) if fs else (part.get("typeName") or "unknown")
    opened = mounted.get(part_id)
    if fs is None and not opened and not (part.get("mountpoints") or part.get("letter")):
        why = ("The filesystem has not been identified yet. Press Unlock so DiskWorks can read the partition's own signature."
               if not inv.get("refined") else "No known filesystem signature was found; the partition may be empty or use something DiskWorks does not recognise.")
        r = rung("identify", "Identify the filesystem", "no" if inv.get("refined") else "later", why)
        return {"part": part_id, "disk": disk["id"], "title": dw_title(part), "fs": None, "fsLabel": part.get("typeName") or "unknown",
                "rungs": [r], "recommended": None, "open": False, "opened": None, "summary": why}
    if IS_WIN:
        rungs = evaluate_windows(inv, disk, part, fs, opened)
    elif IS_MAC:
        rungs = evaluate_mac(inv, disk, part, fs, opened)
    else:
        rungs = evaluate_linux(inv, disk, part, fs, opened)
    first = next((r for r in rungs if r["state"] == "ok" and not r.get("browse")), None) or next((r for r in rungs if r["state"] == "ok"), None)
    done = next((r for r in rungs if r["state"] == "done"), None)
    return {"part": part_id, "disk": disk["id"], "title": dw_title(part), "fs": fs, "fsLabel": label, "rungs": rungs,
            "recommended": first["id"] if first else None, "open": done is not None, "opened": opened,
            "summary": summary(rungs, label)}


def summary(rungs: list[dict], label: str) -> str:
    if any(r["state"] == "done" for r in rungs):
        return f"This {label} partition is already open on this computer."
    ok = [r for r in rungs if r["state"] == "ok" and not r.get("browse")]
    if ok:
        return f"{label}: DiskWorks can open it through '{ok[0]['title']}'."
    if any(r.get("browse") and r["state"] == "ok" for r in rungs):
        return f"{label}: this computer cannot mount it, but DiskWorks can browse it read-only and copy files out."
    later = [r for r in rungs if r["state"] == "later"]
    if later:
        return f"{label}: nothing on this computer can open it today; '{later[0]['title']}' is planned for a later DiskWorks build."
    return f"{label}: this computer cannot open it, and DiskWorks knows no free way to add that."


def dw_title(p: dict) -> str:
    bits = []
    if p.get("letter"):
        bits.append(p["letter"] + ":")
    if p.get("label"):
        bits.append(p["label"])
    elif p.get("name"):
        bits.append(p["name"])
    if not bits:
        bits.append(p.get("typeName") or f"partition {p.get('number')}")
    return " ".join(bits)


def evaluate_windows(inv: dict, disk: dict, part: dict, fs: str | None, opened: dict | None) -> list[dict]:
    rungs: list[dict] = []
    if fs in WINDOWS_NATIVE:
        if part.get("letter"):
            rungs.append(rung("native", "Built into Windows", "done", f"Open in Explorer as {part['letter']}:", path=f"{part['letter']}:\\"))
        else:
            rungs.append(rung("native", "Built into Windows", "ok", f"Windows reads {dw_fs.fs_label(fs)}; it only needs a drive letter.",
                              action={"verb": "letter", "disk": disk["id"], "start": part["start"]}))
        return rungs
    if fs == "bitlocker":
        rungs.append(rung("native", "Built into Windows", "no", "The volume is BitLocker-locked. Unlock it in Windows (Explorer → Unlock drive) first."))
        return rungs
    rungs.append(rung("native", "Built into Windows", "no", f"Windows has no built-in support for {dw_fs.fs_label(fs) if fs else 'this filesystem'}."))
    drv = DRIVERS.get(fs or "")
    letters = windows_letters_by_fs()
    if drv and windows_service_exists(drv["service"]):
        have = letters.get(drv["fsname"], [])
        if have:
            rungs.append(rung("driver", f"{drv['name']} driver (installed)", "done", f"{drv['name']} presents it as {', '.join(l + ':' for l in have)}", path=f"{have[0]}:\\"))
        else:
            rungs.append(rung("driver", f"{drv['name']} driver (installed)", "ok", f"{drv['name']} is installed; assign a drive letter.",
                              action={"verb": "letter", "disk": disk["id"], "start": part["start"]}))
    else:
        rungs.append(rung("driver", "An installed Windows driver", "no",
                          f"No {drv['name'] if drv else 'third-party'} driver for {dw_fs.fs_label(fs) if fs else 'this filesystem'} is installed."))
    wtype = WSL_TYPES.get(fs or "")
    if opened and opened.get("kind") == "wsl":
        rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "done", f"Open in Explorer at {opened['path']}", path=opened["path"]))
    elif fs in ("apfs", "hfsplus"):
        rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no", "The Linux kernel inside WSL is built without HFS+ and APFS support."))
    elif not wtype:
        rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no", f"The Linux kernel inside WSL cannot mount {dw_fs.fs_label(fs) if fs else 'this filesystem'}."))
    else:
        wsl = inv.get("wsl") or wsl_state_cached()
        if not wsl.get("installed"):
            rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no", "WSL is not installed. Windows Features → 'Windows Subsystem for Linux', then install a distribution such as Ubuntu from the Store."))
        elif not wsl.get("mountable"):
            rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no", wsl.get("why") or "WSL 2 with a distribution is required."))
        elif disk.get("system") or disk.get("boot"):
            rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no", "Windows cannot hand its own boot disk to WSL."))
        elif disk.get("removable"):
            rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "no",
                              "Windows cannot hand USB flash media or SD cards to WSL (only fixed disks). A USB hard-disk enclosure works; a flash stick does not."))
        else:
            distro = wsl.get("default") or wsl["distros"][0]["name"]
            rungs.append(rung("wsl", "Attach to WSL (Linux inside Windows)", "ok",
                              f"Attach the whole {disk['name']} to WSL and mount partition {part['number']} as {wtype}; it appears in Explorer under \\\\wsl.localhost\\{distro}\\mnt\\wsl. Other partitions on that disk leave Windows while it is attached.",
                              action={"verb": "wsl_mount", "disk": disk["id"], "number": part["number"], "type": wtype, "distro": distro}))
    if drv:
        rungs.append(rung("install", f"Install the {drv['name']} driver", "later",
                          f"{drv['note']} The installer will ship inside DiskWorks in a later build; today: {drv['url']}", url=drv["url"]))
    elif fs == "xfs":
        rungs.append(rung("install", "Install a Windows driver", "no", "No free xfs driver for Windows exists; WSL is the only route."))
    elif fs in ("apfs", "hfsplus"):
        rungs.append(rung("install", "Install a Windows driver", "no", "No open-source HFS+ / APFS driver for Windows exists."))
    else:
        rungs.append(rung("install", "Install a Windows driver", "no", "No suitable open-source driver is known for this filesystem."))
    c = commercial_rung(fs)
    if c:
        rungs.append(c)
    rungs.append(browser_rung(fs, disk, part))
    return rungs


def evaluate_linux(inv: dict, disk: dict, part: dict, fs: str | None, opened: dict | None) -> list[dict]:
    import dw_linux
    rungs: list[dict] = []
    mps = part.get("mountpoints") or []
    if opened and opened.get("path"):
        rungs.append(rung("mounted", "Mounted by DiskWorks", "done", f"Open at {opened['path']}", path=opened["path"]))
        return rungs
    if mps:
        rungs.append(rung("mounted", "Mounted", "done", f"Open at {mps[0]}", path=mps[0]))
        return rungs
    if fs == "luks":
        rungs.append(rung("kernel", "Linux kernel driver", "no", "Encrypted (LUKS) container: unlock it with your desktop's disk tool first."))
        return rungs
    if fs in ("lvm", "raid"):
        rungs.append(rung("kernel", "Linux kernel driver", "no", f"Member of a {dw_fs.fs_label(fs)} set; open the set, not the member."))
        return rungs
    kname = KERNEL_NAMES.get(fs or "")
    sup = dw_linux.kernel_supports(kname) if kname else "none"
    pm = dw_linux.package_manager()
    if fs == "ntfs" and sup == "none":
        legacy = dw_linux.kernel_supports("ntfs")
        fuse = dw_linux.tool("mount.ntfs-3g") or dw_linux.tool("ntfs-3g")
        rungs.append(rung("kernel", "Linux kernel driver (ntfs3)", "no", "This kernel has no ntfs3 driver" + (" (only the old read-only 'ntfs' one)." if legacy != "none" else ".")))
        if fuse:
            rungs.append(rung("fuse", "ntfs-3g (bundled FUSE driver)", "ok", "Read/write NTFS through the bundled ntfs-3g.",
                              action={"verb": "mount", "device": part["device"], "fstype": "ntfs-3g"}))
        else:
            rungs.append(rung("fuse", "ntfs-3g (FUSE driver)", "no", "ntfs-3g is not bundled in this build and not installed (package ntfs-3g)."))
    elif fs == "hfsplus" and sup != "none":
        rungs.append(rung("kernel", "Linux kernel driver (hfsplus)", "ok",
                          "Linux reads Mac OS Extended volumes. Mounted read-only: writing to a journaled HFS+ volume from Linux risks corrupting it, so DiskWorks does not.",
                          action={"verb": "mount", "device": part["device"], "fstype": "hfsplus", "readonly": True}))
    elif fs == "apfs":
        if sup != "none":
            rungs.append(rung("kernel", "Linux kernel driver (apfs, linux-apfs-rw)", "ok", "The APFS module is installed; it mounts read-only by design.",
                              action={"verb": "mount", "device": part["device"], "fstype": "apfs", "readonly": True}))
        else:
            rungs.append(rung("kernel", "Linux kernel driver (apfs)", "no",
                              "No APFS module in this kernel. The open-source module is packaged as apfs-dkms (Ubuntu 22.04+, Debian): "
                              + (f"`sudo {pm} install apfs-dkms apfsprogs` (read-only mounts)." if pm else "install apfs-dkms and apfsprogs (read-only mounts).")))
    elif kname and sup != "none":
        rungs.append(rung("kernel", f"Linux kernel driver ({kname})", "ok", f"The kernel can mount {dw_fs.fs_label(fs)} ({sup}).",
                          action={"verb": "mount", "device": part["device"], "fstype": kname}))
    elif fs == "refs":
        rungs.append(rung("kernel", "Linux kernel driver", "no", "Linux has no ReFS driver (refsprogs can read it, read-only)."))
    elif kname:
        rungs.append(rung("kernel", f"Linux kernel driver ({kname})", "no",
                          f"This kernel has no {kname} module. DiskWorks cannot bring kernel modules; "
                          + (f"your distribution may offer it (try: {pm} install linux-modules-extra or the filesystem's package)." if pm else "install it with your distribution's package manager.")))
    else:
        rungs.append(rung("kernel", "Linux kernel driver", "no", f"Unknown filesystem{' ' + str(fs) if fs else ''}."))
    c = commercial_rung(fs)
    if c:
        rungs.append(c)
    if not any(r["state"] == "ok" for r in rungs):
        rungs.append(browser_rung(fs, disk, part))
    return rungs


def evaluate_mac(inv: dict, disk: dict, part: dict, fs: str | None, opened: dict | None) -> list[dict]:
    import dw_mac
    rungs: list[dict] = []
    mps = part.get("mountpoints") or []
    if opened and opened.get("path"):
        rungs.append(rung("mounted", "Mounted by DiskWorks", "done", f"Open in Finder at {opened['path']}", path=opened["path"]))
        return rungs
    if mps:
        rungs.append(rung("mounted", "Mounted", "done", f"Open in Finder at {mps[0]}", path=mps[0]))
        return rungs
    dev = part.get("device")
    if fs in MAC_NATIVE:
        rungs.append(rung("native", "Built into macOS", "ok", f"macOS reads and writes {dw_fs.fs_label(fs)}.",
                          action={"verb": "mac_mount", "device": dev}))
        return rungs
    if fs == "ntfs":
        rungs.append(rung("native", "Built into macOS (read-only)", "ok", "macOS mounts NTFS read-only out of the box; files can be read and copied, not changed.",
                          action={"verb": "mac_mount", "device": dev, "readonly": True}))
        if dw_mac.macfuse_installed() and dw_mac.which("ntfs-3g"):
            rungs.append(rung("fuse", "ntfs-3g through macFUSE (read/write)", "ok", "Both are installed; DiskWorks mounts the volume read/write" +
                              (" without a kernel extension (FSKit backend)." if dw_mac.macfuse_fskit_capable() else "."),
                              action={"verb": "fuse_mount", "tool": "ntfs-3g", "device": dev, "name": dw_title(part)}))
        else:
            rungs.append(rung("fuse", "ntfs-3g through macFUSE (read/write)", "no",
                              "Free read/write NTFS needs macFUSE plus ntfs-3g: `brew install --cask macfuse`, `brew tap gromgit/fuse`, "
                              "`brew install gromgit/fuse/ntfs-3g-mac`. macFUSE 5.1+ on macOS 15.4+ can run without a kernel extension; "
                              "older setups need 'Reduced Security' in Recovery on Apple Silicon. Then press Check options again."))
    elif fs in ("ext4", "ext3", "ext2"):
        rungs.append(rung("native", "Built into macOS", "no", "macOS has no ext2/3/4 support of its own."))
        if dw_mac.extendfs_installed():
            rungs.append(rung("fskit", "ExtendFS (FSKit, read-only)", "ok", "ExtendFS is installed; macOS mounts the volume read-only through it.",
                              action={"verb": "mac_mount", "device": dev, "readonly": True}))
        elif dw_mac.macfuse_installed() and dw_mac.which("ext4fuse"):
            rungs.append(rung("fuse", "ext4fuse through macFUSE (read-only)", "ok", "ext4fuse is installed (read-only).",
                              action={"verb": "fuse_mount", "tool": "ext4fuse", "device": dev, "name": dw_title(part)}))
        else:
            rungs.append(rung("fskit", "ExtendFS (FSKit, read-only)", "no",
                              "Free read-only ext2/3/4 on macOS 15.6+: install ExtendFS (https://github.com/kthchew/ExtendFS, also on the Mac App Store); "
                              "it needs no kernel extension. Then press Check options again."))
    elif fs in ("btrfs", "xfs", "f2fs", "swap"):
        rungs.append(rung("native", "Built into macOS", "no", f"macOS has no {dw_fs.fs_label(fs)} support, free or paid. Read it on a Linux machine, or back it up as an image with DiskWorks."))
    elif fs in ("luks", "lvm", "raid"):
        rungs.append(rung("native", "Built into macOS", "no", f"{dw_fs.fs_label(fs)} cannot be opened on macOS."))
    elif fs == "refs":
        rungs.append(rung("native", "Built into macOS", "no", "macOS has no ReFS support."))
    else:
        rungs.append(rung("native", "Built into macOS", "no", f"macOS cannot mount {dw_fs.fs_label(fs) if fs else 'this filesystem'}."))
    c = commercial_rung(fs)
    if c:
        rungs.append(c)
    rungs.append(browser_rung(fs, disk, part))
    return rungs


# ----------------------------------------------------------------------------
# Helper verbs (privileged actions)
# ----------------------------------------------------------------------------
def helper_verbs(helper) -> dict:
    return {
        "wsl_mount": lambda rid, args: wsl_mount(helper, rid, args),
        "wsl_unmount": lambda rid, args: wsl_unmount(helper, rid, args),
        "mount": lambda rid, args: mount(helper, rid, args),
        "unmount": lambda rid, args: unmount(helper, rid, args),
        "letter": lambda rid, args: letter(helper, rid, args),
        "mac_mount": lambda rid, args: mac_mount(helper, rid, args),
        "mac_unmount": lambda rid, args: mac_unmount(helper, rid, args),
        "fuse_mount": lambda rid, args: fuse_mount(helper, rid, args),
        "browse": lambda rid, args: browse(helper, rid, args),
        "copyout": lambda rid, args: copyout(helper, rid, args),
    }


def wsl_mount(helper, rid: int, args: dict) -> dict:
    import dw_win
    disk = helper.find_disk(str(args.get("disk")))
    number = int(args.get("number"))
    wtype = str(args.get("type") or "ext4")
    if wtype not in set(WSL_TYPES.values()):
        raise RuntimeError("Unsupported filesystem type for WSL.")
    if disk.get("system") or disk.get("boot") or disk.get("removable"):
        raise RuntimeError("This disk cannot be handed to WSL (boot disk or removable media).")
    name = f"dw{disk['number']}p{number}"
    log = helper.log_cmd(rid)
    t0 = time.time()
    code, out = dw_win.wsl(["--mount", disk["path"].replace("\\\\.\\PhysicalDrive", "\\\\.\\PHYSICALDRIVE"), "--partition", str(number), "--type", wtype, "--name", name], timeout=180)
    log(f"wsl --mount {disk['path']} --partition {number} --type {wtype} --name {name}", code, (time.time() - t0) * 1000, out, "Attach to WSL")
    if code != 0:
        raise RuntimeError("WSL could not attach the disk: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))
    distro = args.get("distro") or ""
    path = f"\\\\wsl.localhost\\{distro}\\mnt\\wsl\\{name}" if distro else f"\\\\wsl$\\mnt\\wsl\\{name}"
    return {"path": path, "name": name, "linuxPath": f"/mnt/wsl/{name}", "output": out.strip()[-500:]}


def wsl_unmount(helper, rid: int, args: dict) -> dict:
    import dw_win
    disk = helper.find_disk(str(args.get("disk")))
    log = helper.log_cmd(rid)
    t0 = time.time()
    code, out = dw_win.wsl(["--unmount", disk["path"].replace("\\\\.\\PhysicalDrive", "\\\\.\\PHYSICALDRIVE")], timeout=120)
    log(f"wsl --unmount {disk['path']}", code, (time.time() - t0) * 1000, out, "Detach from WSL")
    if code != 0:
        raise RuntimeError("WSL could not detach the disk: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))
    return {"ok": True}


def _check_dev(dev: str) -> str:
    if not re.match(r"^/dev/[A-Za-z0-9/_+-]+$", dev) or not os.path.exists(dev):
        raise RuntimeError("Unknown device.")
    return dev


def mount(helper, rid: int, args: dict) -> dict:
    import dw_linux
    dev = _check_dev(str(args.get("device") or ""))
    fstype = args.get("fstype") or None
    opts = []
    if args.get("readonly"):
        opts.append("ro")
    if fstype == "ntfs3":
        opts.append("windows_names")
        if helper.uid:
            opts += [f"uid={helper.uid}", f"gid={helper.uid}"]
    elif fstype in ("vfat", "exfat", "ntfs-3g", "hfsplus") and helper.uid:
        opts += [f"uid={helper.uid}", f"gid={helper.uid}"]
        if fstype != "hfsplus":
            opts.append("umask=022")
    mp = dw_linux.mount(dev, None, fstype, ",".join(opts) or None, log=helper.log_cmd(rid))
    return {"path": mp}


def unmount(helper, rid: int, args: dict) -> dict:
    import dw_linux
    dev = str(args.get("device") or "")
    if not re.match(r"^/dev/[A-Za-z0-9/_+-]+$", dev):
        raise RuntimeError("Unknown device.")
    done = dw_linux.unmount(dev, log=helper.log_cmd(rid))
    return {"unmounted": done}


def letter(helper, rid: int, args: dict) -> dict:
    import dw_win
    import dw_ops
    disk = helper.find_disk(str(args.get("disk")))
    start = int(args.get("start"))
    script = dw_ops.build_ps({"action": "letter", "disk": disk["number"], "offset": start, "letter": "auto"}) + \
        "\n$p = Get-Partition -DiskNumber %d | Where-Object { $_.Offset -eq %d }\nWrite-Output ('' + $p.DriveLetter)" % (int(disk["number"]), start)
    code, out, err = dw_win.run_powershell(script, timeout=120, log=helper.log_cmd(rid), title="Assign drive letter")
    if code != 0:
        import dw_ops_exec
        raise RuntimeError(dw_ops_exec.friendly_ps_error(err or out))
    l = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return {"path": f"{l}:\\" if l else None, "letter": l or None}


def mac_mount(helper, rid: int, args: dict) -> dict:
    import dw_mac
    dev = _check_dev(str(args.get("device") or ""))
    mp = dw_mac.mount(dev, readonly=bool(args.get("readonly")), log=helper.log_cmd(rid))
    return {"path": mp}


def mac_unmount(helper, rid: int, args: dict) -> dict:
    import dw_mac
    dev = _check_dev(str(args.get("device") or ""))
    dw_mac.unmount(dev, log=helper.log_cmd(rid))
    return {"ok": True}


def fuse_mount(helper, rid: int, args: dict) -> dict:
    """macOS: ntfs-3g (read/write) or ext4fuse (read-only) through macFUSE, mounted under /Volumes."""
    import dw_mac
    dev = _check_dev(str(args.get("device") or ""))
    tool = str(args.get("tool") or "")
    exe = dw_mac.which(tool)
    if tool not in ("ntfs-3g", "ext4fuse") or not exe:
        raise RuntimeError(f"{tool} is not installed.")
    name = re.sub(r"[^A-Za-z0-9 _.-]+", "", str(args.get("name") or "Untitled")).strip() or "Untitled"
    mp = f"/Volumes/{name}"
    n = 1
    while os.path.exists(mp) and os.listdir(mp):
        n += 1
        mp = f"/Volumes/{name} {n}"
    os.makedirs(mp, exist_ok=True)
    try:
        dw_mac.unmount(dev, log=helper.log_cmd(rid))   # macOS may have auto-mounted it read-only
    except Exception:
        pass
    opts = "local,allow_other" if tool == "ntfs-3g" else "allow_other,ro"
    if tool == "ntfs-3g" and dw_mac.macfuse_fskit_capable():
        opts += ",backend=fskit"
    code, out = dw_mac.run([exe, dev, mp, "-o", opts], timeout=120, log=helper.log_cmd(rid), title=f"Mount with {tool}")
    if code != 0:
        raise RuntimeError(f"{tool} failed: " + (out.strip().splitlines()[-1] if out.strip() else f"exit {code}"))
    if helper.uid:
        try:
            os.chown(mp, int(helper.uid), -1)
        except OSError:
            pass
    return {"path": mp}


# ----------------------------------------------------------------------------
# Read-only browser (7-Zip on the partition device; temporary image as fallback)
# ----------------------------------------------------------------------------
def _find_part(helper, args: dict) -> tuple[dict, dict]:
    disk = helper.find_disk(str(args.get("disk")))
    start = int(args.get("start") or 0)
    part = next((p for p in disk.get("partitions", []) if int(p["start"]) == start), None)
    if not part:
        raise RuntimeError("The partition was not found (the disk changed).")
    return disk, part


def _browser_source(helper, rid: int, disk: dict, part: dict) -> str:
    """The path 7-Zip opens: the partition device, or a temporary image of it when the device cannot be opened."""
    cache = getattr(helper, "browser_sources", None)
    if cache is None:
        cache = helper.browser_sources = {}
    key = part["id"]
    if key in cache and os.path.exists(cache[key]):
        return cache[key]
    sz = sevenzip()
    dev = browser_device(disk, part)
    log = helper.log_cmd(rid)
    if dev:
        env = _sz_env()
        r = subprocess.run([sz, "l", "-ba", "-slt", dev, "-i!*", "-r-"], capture_output=True, timeout=300, env=env,
                           creationflags=0x08000000 if IS_WIN else 0)
        text = (r.stdout + r.stderr).decode("utf-8", "replace")
        log(f"{sz} l -ba -slt {dev}", r.returncode, 0, text[-1500:], "Open with 7-Zip")
        if r.returncode in (0, 1) and "Can not open" not in text and "Cannot open" not in text and "ERROR" not in text.upper()[:400]:
            cache[key] = dev
            return dev
        helper.note(rid, "7-Zip could not open the partition device directly; copying the partition to a temporary image first.")
    if int(part["size"]) > TEMP_IMAGE_LIMIT:
        raise RuntimeError(f"This partition ({part['size'] // 2**30} GiB) is too large for the read-only browser's fallback (limit {TEMP_IMAGE_LIMIT // 2**30} GiB).")
    import tempfile
    import dw_image
    tmp = os.path.join(tempfile.gettempdir(), f"diskworks-browse-{re.sub(r'[^A-Za-z0-9]', '_', part['id'])}.img")
    dev_r = helper.open_ro(disk)
    try:
        with open(tmp, "wb") as f:
            pos, length = int(part["start"]), int(part["size"])
            pr = dw_image.Progress(helper, rid, length, "read")
            done = 0
            while done < length:
                n = min(1 << 20, length - done)
                data = dev_r.read(pos + done, n)
                if len(data) < n:
                    raise RuntimeError("Short read while copying the partition.")
                f.write(data)
                done += n
                pr.tick(done)
                helper.check_cancel()
            pr.tick(done, force=True)
    finally:
        dev_r.close()
    cache[key] = tmp
    return tmp


def _sz_env() -> dict:
    env = dict(os.environ, LC_ALL="C")
    if IS_MAC:
        import dw_mac
        env = dw_mac.clean_env()
    elif IS_LINUX:
        import dw_linux
        env = dw_linux.tool_env()
    return env


def parse_slt(text: str) -> list[dict]:
    """7-Zip `l -slt -ba` output → [{name, path, size, dir, mtime}]."""
    out = []
    block: dict = {}
    for line in text.splitlines() + [""]:
        line = line.rstrip("\r")
        if not line.strip():
            if block.get("Path"):
                path = block["Path"].replace("\\", "/")
                attrs = block.get("Attributes", "")
                is_dir = attrs.startswith("D") or block.get("Folder", "").strip() == "+"
                try:
                    size = int(block.get("Size") or 0)
                except ValueError:
                    size = 0
                out.append({"path": path, "name": path.rsplit("/", 1)[-1], "size": size, "dir": is_dir, "mtime": block.get("Modified", "")})
            block = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            block[k.strip()] = v.strip()
    return out


def browse(helper, rid: int, args: dict) -> dict:
    sz = sevenzip()
    if not sz:
        raise RuntimeError("7-Zip is not available in this build.")
    disk, part = _find_part(helper, args)
    folder = str(args.get("path") or "").strip("/").replace("\\", "/")
    src = _browser_source(helper, rid, disk, part)
    pattern = f"{folder}/*" if folder else "*"
    argv = [sz, "l", "-ba", "-slt", "-r-", src, f"-i!{pattern}"]
    r = subprocess.run(argv, capture_output=True, timeout=600, env=_sz_env(), creationflags=0x08000000 if IS_WIN else 0)
    text = r.stdout.decode("utf-8", "replace")
    helper.log_cmd(rid)(" ".join(argv), r.returncode, 0, (text[-800:] + r.stderr.decode("utf-8", "replace")[-400:]).strip(), "List files")
    if r.returncode not in (0, 1):
        raise RuntimeError("7-Zip could not list that folder: " + (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1])
    entries = [e for e in parse_slt(text) if e["path"] != folder]
    depth = folder.count("/") + 1 if folder else 0
    entries = [e for e in entries if e["path"].count("/") == depth]
    entries.sort(key=lambda e: (not e["dir"], -e["size"] if not e["dir"] else 0, e["name"].lower()))
    return {"path": folder, "entries": entries[:5000], "total": len(entries), "source": "device" if src.startswith(("/dev", "\\\\.\\")) else "temporary image"}


def copyout(helper, rid: int, args: dict) -> dict:
    sz = sevenzip()
    if not sz:
        raise RuntimeError("7-Zip is not available in this build.")
    disk, part = _find_part(helper, args)
    paths = [str(p).strip("/").replace("\\", "/") for p in (args.get("paths") or []) if str(p).strip()]
    dest = str(args.get("dest") or "")
    if not paths:
        raise RuntimeError("Nothing selected.")
    if not dest or not os.path.isdir(dest):
        raise RuntimeError("Choose a destination folder first.")
    src = _browser_source(helper, rid, disk, part)
    argv = [sz, "x", "-y", "-bsp1", "-bso0", src, f"-o{dest}"] + paths
    t0 = time.time()

    def on_line(line: str) -> None:
        m = re.search(r"(\d{1,3})%", line)
        if m:
            helper.progress(rid, phase="copy", percent=int(m.group(1)), message=line[-120:])
    if IS_WIN:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=0x08000000)
        buf = bytearray()
        text = bytearray()
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            text += ch
            if ch in (b"\r", b"\n"):
                on_line(buf.decode("utf-8", "replace"))
                buf = bytearray()
            else:
                buf += ch
        proc.wait()
        code, out = proc.returncode, text.decode("utf-8", "replace")
    elif IS_MAC:
        import dw_mac
        code, out = dw_mac.run(argv, timeout=36000, on_line=on_line)
    else:
        import dw_linux
        code, out = dw_linux.run(argv, timeout=36000, on_line=on_line)
    helper.log_cmd(rid)(" ".join(argv), code, (time.time() - t0) * 1000, out[-2000:], "Copy files out")
    if code not in (0, 1):
        raise RuntimeError("7-Zip could not copy the files: " + (out.strip().splitlines() or ["?"])[-1])
    if not IS_WIN and getattr(helper, "uid", None):
        for root, dirs, files in os.walk(dest):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(root, n), int(helper.uid), -1)
                except OSError:
                    pass
    return {"ok": True, "dest": dest, "count": len(paths), "warnings": code == 1}


# ----------------------------------------------------------------------------
# Window-process side
# ----------------------------------------------------------------------------
class Access:
    def __init__(self, jobs):
        self.jobs = jobs
        self.app = jobs.app
        self.mounted: dict[str, dict] = {}   # part id -> {kind, path, disk, device}
        self.events = self.app.log.__class__(2000)
        self.job_state: dict = {"running": False, "done": False, "error": None, "result": None}
        self.thread: threading.Thread | None = None

    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/access/state":
            h._json({"mounted": self.mounted, "sevenzip": bool(sevenzip())})
            return True
        if path == "/api/access/events":
            since = int(q.get("since", ["0"])[0])
            h._json({"events": self.events.since(since), "seq": self.events.seq, "state": self.job_state})
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/access/ladder":
            h._json(evaluate(self.app.current_inventory(), str(body.get("part")), self.mounted))
            return True
        if path == "/api/access/open":
            h._json(self.open(body))
            return True
        if path == "/api/access/close":
            h._json(self.close(body))
            return True
        if path == "/api/access/browse":
            h._json(self.browse(body))
            return True
        if path == "/api/access/copyout":
            h._json(self.copyout(body))
            return True
        if path == "/api/access/pickdir":
            h._json(self.pick_dir())
            return True
        return False

    def _part(self, part_id: str) -> tuple[dict, dict]:
        inv = self.app.current_inventory()
        for d in inv["disks"]:
            for p in d["partitions"]:
                if p["id"] == part_id:
                    return d, p
        raise RuntimeError("That partition is no longer present.")

    def open(self, body: dict) -> dict:
        inv = self.app.current_inventory()
        part_id = str(body.get("part"))
        ev = evaluate(inv, part_id, self.mounted)
        rid = body.get("rung") or ev.get("recommended")
        r = next((x for x in ev["rungs"] if x["id"] == rid), None)
        if not r or r["state"] != "ok" or not r.get("action"):
            raise RuntimeError("That option is not available for this partition.")
        act = r["action"]
        if act["verb"] == "browse":
            return {"ok": True, "browse": True, "rung": r["id"]}
        # Linux: try udisks2 as the logged-in user first (no prompt for removable media)
        if act["verb"] == "mount" and IS_LINUX and shutil.which("udisksctl") and not self.app.helper_ready() and not act.get("readonly"):
            rr = subprocess.run(["udisksctl", "mount", "-b", act["device"], "--no-user-interaction"], capture_output=True, text=True, timeout=60)
            self.app.log_cmd(f"udisksctl mount -b {act['device']} --no-user-interaction", rr.returncode, 0, (rr.stdout + rr.stderr).strip(), "Mount (udisks2)")
            m = re.search(r"at (\S+)", rr.stdout or "")
            if rr.returncode == 0 and m:
                self.mounted[part_id] = {"kind": "udisks", "path": m.group(1), "disk": ev["disk"], "device": act["device"]}
                self.app.refresh_inventory()
                return {"ok": True, "path": m.group(1), "rung": r["id"], "via": "udisks2"}
        helper = self.jobs.helper()
        res = helper.request(act["verb"], act, timeout=600, on_event=self.jobs._forward_log)
        path = res.get("path")
        kind = {"wsl_mount": "wsl", "mount": "mount", "letter": "letter", "mac_mount": "mac", "fuse_mount": "fuse"}[act["verb"]]
        self.mounted[part_id] = {"kind": kind, "path": path, "disk": ev["disk"], "device": act.get("device"), "name": res.get("name")}
        self.app.info(f"Opened {ev['title']} via {r['title']}: {path}")
        self.app.refresh_inventory()
        return {"ok": True, "path": path, "rung": r["id"], "linuxPath": res.get("linuxPath")}

    def close(self, body: dict) -> dict:
        part_id = str(body.get("part"))
        m = self.mounted.get(part_id)
        if not m:
            raise RuntimeError("DiskWorks did not open this partition, so it has nothing to close.")
        if m["kind"] == "udisks":
            rr = subprocess.run(["udisksctl", "unmount", "-b", m["device"]], capture_output=True, text=True, timeout=60)
            if rr.returncode != 0:
                raise RuntimeError((rr.stderr or rr.stdout).strip() or "unmount failed")
        elif m["kind"] == "wsl":
            self.jobs.helper().request("wsl_unmount", {"disk": m["disk"]}, timeout=180, on_event=self.jobs._forward_log)
        elif m["kind"] in ("mount",):
            self.jobs.helper().request("unmount", {"device": m["device"]}, timeout=180, on_event=self.jobs._forward_log)
        elif m["kind"] in ("mac", "fuse"):
            self.jobs.helper().request("mac_unmount", {"device": m["device"]}, timeout=180, on_event=self.jobs._forward_log)
        self.mounted.pop(part_id, None)
        self.app.refresh_inventory()
        return {"ok": True}

    def browse(self, body: dict) -> dict:
        d, p = self._part(str(body.get("part")))
        helper = self.jobs.helper()
        return helper.request("browse", {"disk": d["id"], "start": p["start"], "path": body.get("path") or ""}, timeout=1800,
                              on_event=self.jobs._forward_log)

    def pick_dir(self) -> dict:
        if not (self.app.windowed and self.app.windows):
            return {"path": None, "manual": True}
        import webview
        try:
            res = self.app.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        except Exception as e:
            raise RuntimeError(f"The folder dialog could not be opened: {e}")
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        return {"path": res}

    def copyout(self, body: dict) -> dict:
        if self.thread and self.thread.is_alive():
            raise RuntimeError("A copy is still running.")
        d, p = self._part(str(body.get("part")))
        helper = self.jobs.helper()
        args = {"disk": d["id"], "start": p["start"], "paths": body.get("paths") or [], "dest": body.get("dest") or ""}
        if not args["paths"]:
            raise RuntimeError("Select something to copy first.")
        if not args["dest"]:
            raise RuntimeError("Choose a destination folder first.")
        self.events.clear()
        self.job_state = {"running": True, "done": False, "error": None, "result": None}

        def run():
            def on_event(msg):
                if msg.get("event") == "progress":
                    self.events.push({"type": "progress", "percent": msg.get("percent"), "message": msg.get("message")})
                else:
                    self.jobs._forward_log(msg)
            try:
                res = helper.request("copyout", args, on_event=on_event, timeout=7 * 24 * 3600)
                res.pop("id", None)
                res.pop("event", None)
                self.job_state.update({"running": False, "done": True, "result": res})
                self.events.push({"type": "done", "ok": True, "result": res})
            except RuntimeError as e:
                self.job_state.update({"running": False, "done": True, "error": str(e)})
                self.events.push({"type": "done", "ok": False, "error": str(e)})
        self.thread = threading.Thread(target=run, daemon=True, name="copyout")
        self.thread.start()
        return {"ok": True}
