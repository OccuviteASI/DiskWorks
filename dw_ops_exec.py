"""Helper-side execution of planned steps (dw_ops) and filesystem probes.
Imported by dw_helper; every function here runs as administrator / root."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time

import subprocess

import dw_fs
import dw_inventory as di
import dw_ops

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def verbs(helper) -> dict:
    out = {
        "step": lambda rid, args: run_step(helper, rid, args),
        "probe": lambda rid, args: probe(helper, rid, args),
        "vhd": lambda rid, args: vhd(helper, rid, args),
    }
    try:
        import dw_image
        out.update(dw_image.helper_verbs(helper))
    except ImportError:
        pass
    try:
        import dw_access
        out.update(dw_access.helper_verbs(helper))
    except ImportError:
        pass
    return out


def vhd(helper, rid: int, args: dict) -> dict:
    """Test disks: create+attach or detach a VHD (Windows, diskpart) / a loop device (Linux)."""
    action = args.get("action")
    path = str(args.get("path") or "")
    size_mb = int(args.get("sizeMB") or 1024)
    log = helper.log_cmd(rid)
    if not path or not re.match(r"^[A-Za-z0-9_./:\\ -]+$", path):
        raise RuntimeError("Give the image file a plain path.")
    if IS_WIN:
        import dw_win
        if action == "create":
            code, out = dw_win.run_diskpart([f'create vdisk file="{path}" maximum={size_mb} type=expandable',
                                             f'select vdisk file="{path}"', "attach vdisk"], log=log, title="Create test disk")
            if code != 0:
                raise RuntimeError("diskpart could not create the virtual disk: " + last_line(out))
            time.sleep(1.5)
            script = ("$ErrorActionPreference='Stop'\n"
                      f"$d = Get-Disk | Where-Object {{ $_.Location -eq {dw_ops.ps_quote(path)} }}\n"
                      "if (-not $d) { throw 'attached disk not found' }\nWrite-Output $d.Number")
            code, out, err = dw_win.run_powershell(script, timeout=60, log=log, title="Find test disk")
            if code != 0:
                raise RuntimeError(friendly_ps_error(err or out))
            return {"disk": f"disk:{int(out.strip().splitlines()[-1])}", "path": path}
        if action == "detach":
            code, out = dw_win.run_diskpart([f'select vdisk file="{path}"', "detach vdisk"], log=log, title="Detach test disk")
            if code != 0:
                raise RuntimeError("diskpart could not detach the virtual disk: " + last_line(out))
            return {"ok": True}
        raise RuntimeError("vhd action must be create or detach")
    if IS_MAC:
        import dw_mac
        if action == "create":
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.truncate(size_mb * 2**20)
            ident = dw_mac.hdiutil_attach(path, log=log)
            return {"disk": f"disk:{ident}", "path": path, "device": f"/dev/{ident}"}
        if action == "detach":
            r = subprocess.run([dw_mac.HDIUTIL, "info", "-plist"], capture_output=True, timeout=60, env=dw_mac.clean_env())
            try:
                import plistlib
                info = plistlib.loads(r.stdout)
                for img in info.get("images", []):
                    if os.path.realpath(img.get("image-path", "")) == os.path.realpath(path):
                        for ent in img.get("system-entities", []):
                            dev = ent.get("dev-entry", "")
                            if re.fullmatch(r"/dev/disk\d+", dev):
                                dw_mac.hdiutil_detach(dev.split("/")[-1], log=log)
            except Exception as e:
                raise RuntimeError(f"Could not find the attached image: {e}")
            return {"ok": True}
        raise RuntimeError("vhd action must be create or detach")
    import dw_linux
    if action == "create":
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.truncate(size_mb * 2**20)
        code, out = dw_linux.run(["losetup", "-fP", "--show", path], timeout=30, log=log, title="Attach test disk")
        if code != 0:
            raise RuntimeError("losetup failed: " + last_line(out))
        dev = last_line(out)
        return {"disk": f"disk:{os.path.basename(dev)}", "path": path, "device": dev}
    if action == "detach":
        code, out = dw_linux.run(["losetup", "-j", path], timeout=30, log=log, title="Find test disk")
        for line in out.splitlines():
            dev = line.split(":")[0]
            if dev.startswith("/dev/loop"):
                dw_linux.run(["losetup", "-d", dev], timeout=30, log=log, title="Detach test disk")
        return {"ok": True}
    raise RuntimeError("vhd action must be create or detach")


# ----------------------------------------------------------------------------
def friendly_ps_error(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    msg = ""
    for l in lines:
        if l.startswith(("At line:", "+ ", "CategoryInfo", "FullyQualifiedErrorId", "PSComputerName")):
            continue
        msg = l
        break
    msg = re.sub(r"^.*?:\s*", "", msg, count=1) if re.match(r"^[A-Za-z-]+ : ", msg) else msg
    table = {
        "Not enough available capacity": "There is not enough free space on the disk for that size.",
        "Access is denied": "Windows denied access. Close programs that use the drive and try again.",
        "The requested access path is already in use": "That drive letter is already in use.",
        "The specified object was not found": "The partition was not found; the disk has changed.",
        "Size Not Supported": "Windows does not support that size for this partition.",
        "Cannot shrink a partition containing a volume with errors": "The volume has errors; run Check first.",
        "The volume is in use": "The volume is in use by another program.",
        "Format-Volume : Failed": "Formatting failed.",
    }
    for k, v in table.items():
        if k.lower() in (text or "").lower():
            return v + (f" ({msg})" if msg and v not in msg else "")
    return msg or "The command failed without a message."


def run_step(helper, rid: int, args: dict) -> dict:
    step = args.get("step") or {}
    expect_hash = args.get("hash")
    kind = step.get("kind")
    a = step.get("args") or {}
    title = step.get("title") or kind
    log = helper.log_cmd(rid)
    if expect_hash:
        # Compare against the same refined view the window planned on: the raw lsblk view can
        # lack the table type (no udev database, e.g. in containers) and would refuse every step.
        inv = di.inventory()
        helper.refine(inv, rid)
        if inv.get("hash") != expect_hash:
            raise RuntimeError("The disks changed since the plan was made. Nothing was done; review the queue.")
        helper.inventory_cache = inv
    helper.check_cancel()
    if IS_WIN:
        return run_step_windows(helper, rid, step, kind, a, title, log)
    if IS_MAC:
        return run_step_mac(helper, rid, step, kind, a, title, log)
    return run_step_linux(helper, rid, step, kind, a, title, log)


# ----------------------------------------------------------------------------
# macOS
# ----------------------------------------------------------------------------
def run_step_mac(helper, rid, step, kind, a, title, log) -> dict:
    import dw_mac
    on_line = lambda line: progress_from_line(helper, rid, line)
    if kind == "diskutil":
        argv = dw_ops.build_diskutil(a, step["diskPath"])
        if a.get("expectStart") is not None and a.get("device"):
            disk = helper.find_disk(step["disk"])
            part = next((p for p in disk.get("partitions", []) if p.get("device") == a["device"]), None)
            if part is None:
                raise RuntimeError(f"{a['device']} does not exist (the disk changed). Nothing was done.")
            if not part.get("approx") and int(part["start"]) != int(a["expectStart"]):
                raise RuntimeError(f"{a['device']} now starts at {part['start']}, expected {a['expectStart']}: the disk changed. Nothing was done.")
        code, out = dw_mac.run(argv, timeout=36000, log=log, title=title, on_line=on_line)
        if code != 0:
            raise RuntimeError(f"diskutil failed: {last_line(out)}")
        if a.get("wipe"):
            disk = helper.find_disk(step["disk"])
            dw_mac.unmount_disk(disk["name"], log=log)
            dev = dw_mac.RawDevice(disk["path"], write=True)
            try:
                zero_head_tail(dev, disk, log)
            finally:
                dev.close()
            dw_mac.mount_disk(disk["name"], log=log)
        return {"ok": True, "output": out[-2000:]}
    if kind == "unmount":
        if a.get("device"):
            dw_mac.unmount(a["device"], log=log)
        return {"ok": True}
    if kind == "raw_zero":
        disk = helper.find_disk(step["disk"])
        dw_mac.unmount_disk(disk["name"], log=log)
        dev = dw_mac.RawDevice(disk["path"], write=True)
        try:
            zero_head_tail(dev, disk, log)
        finally:
            dev.close()
        dw_mac.mount_disk(disk["name"], log=log)
        return {"ok": True}
    if kind == "note":
        return {"ok": True}
    raise RuntimeError(f"Step kind '{kind}' is not available on macOS.")


# ----------------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------------
def run_step_windows(helper, rid, step, kind, a, title, log) -> dict:
    import dw_win
    if kind == "ps":
        script = dw_ops.build_ps(a)
        fs = a.get("fs")
        if a.get("action") in ("create", "format") and fs == "fat32" and int(a.get("size") or 0) > 32 * 2**30:
            raise RuntimeError("Windows itself refuses FAT32 above 32 GiB. DiskWorks' own FAT32 formatter is not in this build yet; use exFAT or a smaller partition.")
        if a.get("action") in ("create", "format") and fs == "refs":
            helper.note(rid, "ReFS is only available on some Windows editions; if this fails, pick NTFS.")
        code, out, err = dw_win.run_powershell(script, log=log, title=title)
        if code != 0:
            raise RuntimeError(friendly_ps_error(err or out))
        return {"ok": True, "output": (out or "").strip()[-2000:]}
    if kind == "raw_zero":
        disk = helper.find_disk(step["disk"])
        dev = dw_win.RawDevice(disk["path"], write=True, sector=disk.get("logicalSector") or 512)
        try:
            dev.lock_volumes(int(disk["number"]))
            zero_head_tail(dev, disk, log)
            dev.rescan()
        finally:
            dev.close()
        return {"ok": True}
    if kind == "note":
        return {"ok": True}
    raise RuntimeError(f"Step kind '{kind}' is not available on Windows.")


def zero_head_tail(dev, disk: dict, log) -> None:
    import mmap
    size = int(disk["size"])
    buf = mmap.mmap(-1, 1 << 20)
    t0 = time.time()
    dev.write_aligned(0, buf)
    tail = (size // (disk.get("logicalSector") or 512)) * (disk.get("logicalSector") or 512) - (1 << 20)
    if tail > (1 << 20):
        dev.write_aligned(tail, buf)
    dev.flush()
    log(f"zero 1 MiB at 0 and at {tail}", 0, (time.time() - t0) * 1000, "", "Zero head and tail")


# ----------------------------------------------------------------------------
# Linux
# ----------------------------------------------------------------------------
def _sys_start(dev_path: str) -> int | None:
    name = os.path.basename(dev_path)
    try:
        with open(f"/sys/class/block/{name}/start", "r", encoding="utf-8") as f:
            return int(f.read().strip()) * 512
    except OSError:
        return None


def check_expect(a: dict) -> None:
    dev = a.get("device")
    exp = a.get("expectStart")
    if dev and exp is not None:
        got = _sys_start(dev)
        if got is None:
            raise RuntimeError(f"{dev} does not exist (the disk changed).")
        if got != int(exp):
            raise RuntimeError(f"{dev} now starts at {got}, expected {exp}: the disk changed. Nothing was done.")


def progress_from_line(helper, rid: int, line: str) -> None:
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
    if m:
        try:
            helper.progress(rid, percent=float(m.group(1)), message=line[:120])
        except Exception:
            pass


def run_step_linux(helper, rid, step, kind, a, title, log) -> dict:
    import dw_linux
    disk_path = step.get("diskPath")
    on_line = lambda line: progress_from_line(helper, rid, line)
    if kind == "unmount":
        if a.get("all"):
            disk = helper.find_disk(step["disk"])
            for p in disk.get("partitions", []):
                if p.get("device"):
                    dw_linux.unmount(p["device"], log=log)
        elif a.get("device"):
            dw_linux.unmount(a["device"], log=log)
        return {"ok": True}
    if kind == "sfdisk":
        argv, stdin = dw_ops.build_sfdisk(a, disk_path)
        if a.get("action") in ("delete", "resize", "type") and a.get("expectStart") is not None:
            disk = helper.find_disk(step["disk"])
            dev = dw_ops.part_device(disk, int(a["number"]))
            check_expect({"device": dev, "expectStart": a["expectStart"]})
        code, out = dw_linux.run(argv, timeout=600, on_line=on_line, log=log, title=title, input_text=stdin)
        if code != 0:
            raise RuntimeError(f"sfdisk failed: {last_line(out)}")
        dw_linux.reread_table(disk_path, log=log)
        return {"ok": True}
    if kind == "tool":
        act = a.get("action")
        fs = a.get("fs")
        if act in ("mkfs", "label", "fsck", "fsck_before", "fsresize"):
            wait_for_device(a.get("device"))
            check_expect(a)
        argv = dw_ops.build_tool(a)
        if "<mountpoint>" in argv:
            # btrfs / xfs resize work on a mounted filesystem: mount to a private point, run, unmount
            mp = tempfile.mkdtemp(prefix="diskworks-")
            try:
                dw_linux.mount(a["device"], mp, log=log)
                argv = [mp if x == "<mountpoint>" else x for x in argv]
                code, out = dw_linux.run(argv, timeout=36000, on_line=on_line, log=log, title=title)
            finally:
                try:
                    dw_linux.run(["umount", mp], timeout=60, log=log, title="Unmount")
                except Exception:
                    pass
                try:
                    os.rmdir(mp)
                except OSError:
                    pass
        else:
            code, out = dw_linux.run(argv, timeout=36000, on_line=on_line, log=log, title=title)
        if code != 0 and not (act in ("fsck", "fsck_before") and fs in ("ext4", "ext3", "ext2") and code in (1, 2)):
            raise RuntimeError(f"{argv[0]} failed: {last_line(out)}")
        return {"ok": True, "output": out[-2000:]}
    if kind == "raw_zero":
        disk = helper.find_disk(step["disk"])
        dev = dw_linux.RawDevice(disk["path"], write=True)
        try:
            zero_head_tail(dev, disk, log)
            dev.rescan()
        finally:
            dev.close()
        return {"ok": True}
    if kind == "note":
        return {"ok": True}
    raise RuntimeError(f"Step kind '{kind}' is not available on Linux.")


def wait_for_device(dev: str | None, timeout: float = 10.0) -> None:
    if not dev:
        return
    t0 = time.time()
    while not os.path.exists(dev) and time.time() - t0 < timeout:
        time.sleep(0.2)
    if not os.path.exists(dev):
        raise RuntimeError(f"{dev} did not appear after the partition table was written.")


def last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else "no output"


# ----------------------------------------------------------------------------
# Probes (minimum size for the resize dialog)
# ----------------------------------------------------------------------------
def probe(helper, rid: int, args: dict) -> dict:
    disk = helper.find_disk(str(args.get("disk")))
    start = int(args.get("start") or 0)
    part = next((p for p in disk.get("partitions", []) if p["start"] == start), None)
    if not part:
        raise RuntimeError("The partition was not found (the disk changed).")
    fs = part.get("fs")
    out = {"minSize": None, "maxSize": None, "used": part.get("used"), "note": ""}
    log = helper.log_cmd(rid)
    if IS_WIN:
        import dw_win
        script = (dw_ops.ps_find_partition(int(disk["number"]), start) +
                  f"$s = Get-PartitionSupportedSize -DiskNumber {int(disk['number'])} -PartitionNumber $p.PartitionNumber\n"
                  "Write-Output ('' + $s.SizeMin + ' ' + $s.SizeMax)")
        code, o, e = dw_win.run_powershell(script, timeout=180, log=log, title="Supported sizes")
        if code == 0:
            m = re.search(r"(\d+)\s+(\d+)", o)
            if m:
                out["minSize"], out["maxSize"] = int(m.group(1)), int(m.group(2))
        else:
            out["note"] = friendly_ps_error(e or o)
        return out
    if IS_MAC:
        import dw_mac
        try:
            if fs in ("hfsplus", "apfs"):
                argv = ([dw_mac.DISKUTIL, "apfs", "resizeContainer", part["device"], "limits", "-plist"] if fs == "apfs"
                        else [dw_mac.DISKUTIL, "resizeVolume", part["device"], "limits", "-plist"])
                r = subprocess.run(argv, capture_output=True, timeout=120, env=dw_mac.clean_env())
                log(" ".join(argv), r.returncode, 0, r.stdout.decode("utf-8", "replace")[-2000:], "Resize limits")
                if r.returncode == 0 and r.stdout.strip():
                    import plistlib
                    lim = plistlib.loads(r.stdout)
                    for k, v in lim.items():
                        kl = k.lower()
                        if "min" in kl and isinstance(v, (int, float)):
                            out["minSize"] = int(v)
                        elif "max" in kl and isinstance(v, (int, float)):
                            out["maxSize"] = int(v)
                else:
                    out["note"] = last_line(r.stdout.decode("utf-8", "replace"))
            elif fs in ("exfat", "fat32", "fat16"):
                out["minSize"] = part["size"]
                out["note"] = "macOS cannot resize FAT or exFAT volumes."
        except Exception as e:
            out["note"] = str(e)
        nxt = [q for q in disk["partitions"] if q["start"] > start]
        limit = min(q["start"] for q in nxt) if nxt else dw_ops.usable_end(disk)
        if not out.get("maxSize"):
            out["maxSize"] = limit - start
        return out
    import dw_linux
    dev = part.get("device")
    try:
        if fs in ("ext4", "ext3", "ext2"):
            code, o = dw_linux.run(["tune2fs", "-l", dev], timeout=60, log=log, title="Filesystem details")
            bs = int(re.search(r"Block size:\s+(\d+)", o).group(1)) if code == 0 and re.search(r"Block size:\s+(\d+)", o) else 4096
            code, o = dw_linux.run(["resize2fs", "-P", dev], timeout=600, log=log, title="Minimum size")
            m = re.search(r"minimum size of the filesystem:\s*(\d+)", o)
            if m:
                out["minSize"] = int(m.group(1)) * bs
            elif code != 0:
                out["note"] = "Run Check first: " + last_line(o)
        elif fs == "ntfs":
            code, o = dw_linux.run(["ntfsresize", "--info", "-f", dev], timeout=600, log=log, title="Minimum size")
            m = re.search(r"You might resize at\s+(\d+)\s+bytes", o)
            if m:
                out["minSize"] = int(m.group(1)) + 64 * 2**20
            elif code != 0:
                out["note"] = last_line(o)
        elif fs == "btrfs":
            mp = tempfile.mkdtemp(prefix="diskworks-")
            try:
                dw_linux.mount(dev, mp, log=log)
                code, o = dw_linux.run(["btrfs", "filesystem", "usage", "-b", mp], timeout=120, log=log, title="Space used")
                m = re.search(r"Used:\s+(\d+)", o)
                if m:
                    out["minSize"] = int(m.group(1)) * 2 + 512 * 2**20
            finally:
                try:
                    dw_linux.run(["umount", mp], timeout=60)
                except Exception:
                    pass
                try:
                    os.rmdir(mp)
                except OSError:
                    pass
        elif fs == "xfs":
            out["minSize"] = part["size"]
            out["note"] = "xfs can only grow."
        elif fs in ("exfat",):
            out["minSize"] = part["size"]
            out["note"] = "exFAT cannot be resized."
        elif fs == "swap":
            out["minSize"] = 2**20
    except Exception as e:
        out["note"] = str(e)
    nxt = [q for q in disk["partitions"] if q["start"] > start]
    limit = min(q["start"] for q in nxt) if nxt else dw_ops.usable_end(disk)
    out["maxSize"] = limit - start
    return out
