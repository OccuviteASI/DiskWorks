"""Imaging (ARCHITECTURE.md §9): write an image or hybrid ISO to a drive, back a drive or
partition up to a (sparse, optionally zstd-compressed) image with a manifest, restore, verify.

Window-process side: `ImageJobs` (routes, inspection, native file dialogs, job thread).
Helper side: `helper_verbs()` (the raw copy loops, run as administrator / root).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import mmap
import os
import re
import struct
import sys
import threading
import time

import dw_fs
import dw_inventory as di

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
BLOCK = 1 << 20
ZERO = bytes(BLOCK)

try:  # Python 3.14
    from compression import zstd as _zstd
except Exception:  # pragma: no cover
    _zstd = None


# ----------------------------------------------------------------------------
# Source inspection (unprivileged; reads the file only)
# ----------------------------------------------------------------------------
def compression_of(head: bytes) -> str | None:
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if head[:3] == b"BZh":
        return "bzip2"
    return None


def open_source(path: str):
    """(stream, raw_size_or_None, compression) - the stream yields the decompressed image."""
    with open(path, "rb") as f:
        head = f.read(16)
    comp = compression_of(head)
    if comp is None:
        return open(path, "rb", buffering=0), os.path.getsize(path), None
    if comp == "gzip":
        return gzip.open(path, "rb"), gzip_isize(path), comp
    if comp == "xz":
        return lzma.open(path, "rb"), None, comp
    if comp == "bzip2":
        import bz2
        return bz2.open(path, "rb"), None, comp
    if comp == "zstd":
        if _zstd is None:
            raise RuntimeError("This build cannot read .zst files (Python without zstd support).")
        size = None
        try:
            with open(path, "rb") as f:
                info = _zstd.get_frame_info(f.read(64))
                size = info.decompressed_size
        except Exception:
            size = None
        return _zstd.open(path, "rb"), size, comp
    raise RuntimeError("Unknown compression")


def gzip_isize(path: str) -> int | None:
    try:
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            n = struct.unpack("<I", f.read(4))[0]
        return n if n else None   # modulo 4 GiB - treated as an estimate by the caller
    except OSError:
        return None


def inspect(path: str) -> dict:
    if not path or not os.path.isfile(path):
        raise RuntimeError("Pick an image file first.")
    st = os.stat(path)
    out = {"path": path, "name": os.path.basename(path), "fileSize": st.st_size, "compression": None, "rawSize": st.st_size,
           "rawSizeEstimate": False, "kind": "raw", "hybrid": False, "iso": False, "windowsMedia": False, "vhd": False,
           "notes": [], "manifest": None}
    manifest = manifest_for(path)
    if manifest:
        out["manifest"] = manifest
    stream, raw_size, comp = open_source(path)
    try:
        out["compression"] = comp
        if comp:
            out["rawSize"] = raw_size
            out["rawSizeEstimate"] = comp == "gzip"
            if raw_size is None:
                out["notes"].append("Compressed image: the uncompressed size is only known while writing.")
        head = stream.read(65536)
    finally:
        stream.close()
    b0 = head[:512]
    out["hybrid"] = dw_fs.is_hybrid_image(b0)
    out["table"] = dw_fs.identify_table(b0, head[512:1024]) if len(head) >= 1024 else "none"
    if len(head) > 32774 and head[32769:32774] == b"CD001":
        out["iso"] = True
        out["kind"] = "iso"
        out["volumeId"] = head[32808:32840].decode("ascii", "replace").strip()
    if not comp and st.st_size >= 512:
        with open(path, "rb") as f:
            f.seek(-512, os.SEEK_END)
            foot = f.read(512)
        if foot[:8] == b"conectix":
            out["vhd"] = True
            out["kind"] = "vhd"
            disk_type = struct.unpack(">I", foot[60:64])[0]
            if disk_type != 2:
                out["notes"].append("Only fixed-size VHD files can be written directly; this one is dynamic or differencing.")
                out["kind"] = "vhd-dynamic"
            else:
                out["rawSize"] = st.st_size - 512
    if out["iso"]:
        wm = windows_media_check(path) if not comp else None
        if wm:
            out["windowsMedia"] = True
            out["kind"] = "windows-iso"
            out["notes"].append("Windows installation media: it needs 'ISO mode' (extract + UEFI:NTFS), which is not in this build yet. Written raw it will not boot.")
        elif out["hybrid"]:
            out["notes"].append("Hybrid ISO: written raw it boots from USB (most Linux ISOs).")
        else:
            out["notes"].append("Plain ISO without a USB boot record: written raw it will NOT boot from a USB stick.")
    elif out["kind"] == "raw":
        if out["table"] in ("gpt", "mbr"):
            out["notes"].append(f"Raw disk image with a {out['table'].upper()} partition table.")
        else:
            out["notes"].append("No partition table found at the start; this may be a single-filesystem image or not a disk image at all.")
    return out


def windows_media_check(path: str) -> bool | None:
    try:
        import pycdlib
    except ImportError:
        return None
    iso = pycdlib.PyCdlib()
    try:
        iso.open(path)
    except Exception:
        return None
    try:
        for facade in ("udf", "joliet", "iso"):
            try:
                if facade == "udf" and iso.has_udf():
                    f = iso.get_udf_facade()
                elif facade == "joliet" and iso.has_joliet():
                    f = iso.get_joliet_facade()
                elif facade == "iso":
                    f = iso.get_iso9660_facade()
                else:
                    continue
                names = {c.file_identifier().decode("utf-8", "replace").lower().rstrip(";1") for c in f.list_children("/sources")}
                if any(n.startswith("install.") for n in names):
                    return True
            except Exception:
                continue
        return False
    finally:
        try:
            iso.close()
        except Exception:
            pass


def manifest_for(path: str) -> dict | None:
    for cand in (path + ".json", re.sub(r"\.(img|raw|dd)(\.(zst|gz|xz))?$", ".json", path)):
        if cand != path and os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    m = json.load(f)
                if isinstance(m, dict) and m.get("diskworks"):
                    m["_path"] = cand
                    return m
            except (OSError, ValueError):
                pass
    return None


# ----------------------------------------------------------------------------
# Helper side: the copy loops
# ----------------------------------------------------------------------------
def helper_verbs(helper) -> dict:
    return {
        "image_write": lambda rid, args: image_write(helper, rid, args),
        "image_read": lambda rid, args: image_read(helper, rid, args),
        "image_verify": lambda rid, args: image_verify(helper, rid, args),
    }


class Progress:
    def __init__(self, helper, rid: int, total: int | None, phase: str):
        self.helper, self.rid, self.total, self.phase = helper, rid, total, phase
        self.t0 = time.time()
        self.last = 0.0
        self.done = 0

    def tick(self, done: int, force: bool = False) -> None:
        self.done = done
        now = time.time()
        if not force and now - self.last < 0.25:
            return
        self.last = now
        el = now - self.t0
        speed = done / el if el > 0 else 0
        eta = (self.total - done) / speed if (self.total and speed > 0) else None
        self.helper.progress(self.rid, phase=self.phase, bytes=done, total=self.total, speed=speed, eta=eta, elapsed=el)


def _open_disk_for_write(helper, disk: dict, log):
    if IS_WIN:
        import dw_win
        dev = dw_win.RawDevice(disk["path"], write=True, sector=disk.get("logicalSector") or 512)
        try:
            dev.lock_volumes(int(disk["number"]))
        except Exception:
            dev.close()
            raise
        return dev
    if IS_MAC:
        import dw_mac
        dw_mac.unmount_disk(disk["name"], log=log)
        return dw_mac.RawDevice(disk["path"], write=True, sector=disk.get("logicalSector") or 512)
    import dw_linux
    for p in disk.get("partitions", []):
        if p.get("device"):
            dw_linux.unmount(p["device"], log=log)
    return dw_linux.RawDevice(disk["path"], write=True, sector=disk.get("logicalSector") or 512)


def _open_disk_for_read(helper, disk: dict):
    if IS_WIN:
        import dw_win
        return dw_win.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)
    if IS_MAC:
        import dw_mac
        return dw_mac.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)
    import dw_linux
    return dw_linux.RawDevice(disk["path"], write=False, sector=disk.get("logicalSector") or 512)


def _guard_target(disk: dict, override: bool) -> None:
    if disk.get("system") or disk.get("boot"):
        raise RuntimeError(f"{disk['name']} holds the running operating system; DiskWorks will not write an image over it.")
    if disk.get("locked") and not override:
        raise RuntimeError(f"{disk['name']}: {'; '.join(disk['locked'])}.")


def image_write(helper, rid: int, args: dict) -> dict:
    """Write a raw image / hybrid ISO (possibly compressed) to a whole disk, then verify."""
    path = str(args.get("path") or "")
    disk = helper.find_disk(str(args.get("disk")))
    _guard_target(disk, bool(args.get("override")))
    log = helper.log_cmd(rid)
    stream, raw_size, comp = open_source(path)
    sector = int(disk.get("logicalSector") or 512)
    disk_size = int(disk["size"])
    if raw_size and comp != "gzip" and raw_size > disk_size:
        stream.close()
        raise RuntimeError(f"The image ({raw_size:,} bytes) is larger than {disk['name']} ({disk_size:,} bytes).")
    manifest = manifest_for(path)
    if manifest and manifest.get("source", {}).get("logicalSector") not in (None, sector):
        helper.note(rid, "The backup came from a disk with a different sector size; it may not be usable on this one.")
    helper.note(rid, f"Writing {os.path.basename(path)} to {disk['name']} ({disk.get('model')})")
    t0 = time.time()
    dev = _open_disk_for_write(helper, disk, log)
    buf = mmap.mmap(-1, BLOCK)
    mv = memoryview(buf)
    sha = hashlib.sha256()
    block_hashes: list[bytes] = []
    written = 0
    pr = Progress(helper, rid, raw_size, "write")
    try:
        while True:
            helper.check_cancel()
            n = _readinto(stream, mv)
            if n == 0:
                break
            if written + n > disk_size:
                raise RuntimeError("The image is larger than the disk; stopped at the end of the disk.")
            sha.update(mv[:n])
            block_hashes.append(hashlib.blake2b(mv[:n], digest_size=16).digest())
            padded = ((n + sector - 1) // sector) * sector
            if padded > n:
                mv[n:padded] = bytes(padded - n)
            dev.write_aligned(written, mv[:padded])
            written += n
            pr.tick(written)
        dev.flush()
        pr.tick(written, force=True)
        elapsed = time.time() - t0
        log(f"write {path} -> {disk['path']}", 0, elapsed * 1000, f"{written:,} bytes", "Write image")
        result = {"written": written, "sha256": sha.hexdigest(), "elapsed": elapsed, "verified": None, "mismatches": []}
        if args.get("verify", True):
            result.update(_verify_blocks(helper, rid, dev, block_hashes, written, sector))
        try:
            dev.rescan()
        except Exception:
            pass
        return result
    except Exception:
        if helper.cancel_event.is_set():
            try:  # a half-written stick must not look bootable
                dev.write_aligned(0, memoryview(mmap.mmap(-1, BLOCK)))
                dev.flush()
            except Exception:
                pass
        raise
    finally:
        try:
            stream.close()
        finally:
            dev.close()
            _post_write(disk, log)


def _readinto(stream, mv: memoryview) -> int:
    """Fill mv as far as possible (compressed streams return short reads)."""
    total = 0
    while total < len(mv):
        try:
            n = stream.readinto(mv[total:])
        except AttributeError:
            chunk = stream.read(len(mv) - total)
            n = len(chunk)
            mv[total:total + n] = chunk
        if not n:
            break
        total += n
    return total


def _verify_blocks(helper, rid: int, dev, block_hashes: list[bytes], length: int, sector: int) -> dict:
    pr = Progress(helper, rid, length, "verify")
    buf = mmap.mmap(-1, BLOCK)
    mv = memoryview(buf)
    mismatches: list[int] = []
    pos = 0
    for i, h in enumerate(block_hashes):
        helper.check_cancel()
        n = min(BLOCK, length - pos)
        data = dev.read(pos, n)
        if hashlib.blake2b(data, digest_size=16).digest() != h:
            mismatches.append(pos)
            if len(mismatches) > 50:
                break
        pos += n
        pr.tick(pos)
    pr.tick(pos, force=True)
    return {"verified": not mismatches, "mismatches": mismatches}


def _post_write(disk: dict, log) -> None:
    if IS_WIN:
        import dw_win
        try:
            dw_win.run_powershell(f"Update-Disk -Number {int(disk['number'])}", timeout=60, log=log, title="Rescan disk")
        except Exception:
            pass
    elif IS_MAC:
        import dw_mac
        try:
            dw_mac.mount_disk(disk["name"], log=log)
        except Exception:
            pass
    else:
        import dw_linux
        try:
            dw_linux.reread_table(disk["path"], log=log)
        except Exception:
            pass


def image_verify(helper, rid: int, args: dict) -> dict:
    path = str(args.get("path") or "")
    disk = helper.find_disk(str(args.get("disk")))
    stream, raw_size, comp = open_source(path)
    dev = _open_disk_for_read(helper, disk)
    buf = mmap.mmap(-1, BLOCK)
    mv = memoryview(buf)
    pr = Progress(helper, rid, raw_size, "verify")
    pos = 0
    mismatches: list[int] = []
    try:
        while True:
            helper.check_cancel()
            n = _readinto(stream, mv)
            if n == 0:
                break
            data = dev.read(pos, n)
            if data != bytes(mv[:n]):
                mismatches.append(pos)
                if len(mismatches) > 50:
                    break
            pos += n
            pr.tick(pos)
        pr.tick(pos, force=True)
    finally:
        stream.close()
        dev.close()
    return {"compared": pos, "verified": not mismatches, "mismatches": mismatches}


def image_read(helper, rid: int, args: dict) -> dict:
    """Back up a disk or one partition to a raw (sparse) image or a zstd stream, with a manifest."""
    disk = helper.find_disk(str(args.get("disk")))
    dest = str(args.get("dest") or "")
    compress = str(args.get("compress") or "zstd")
    level = int(args.get("level") or 3)
    start, length, part = 0, int(disk["size"]), None
    if args.get("part"):
        part = next((p for p in disk.get("partitions", []) if p["id"] == args["part"]), None)
        if not part:
            raise RuntimeError("That partition is no longer present.")
        start, length = int(part["start"]), int(part["size"])
    if not dest:
        raise RuntimeError("Choose where to save the backup.")
    if compress == "zstd" and _zstd is None:
        raise RuntimeError("This build cannot write .zst files; choose an uncompressed image.")
    if os.path.abspath(os.path.dirname(dest)) == os.path.abspath(dest):
        raise RuntimeError("Choose a file name for the backup.")
    log = helper.log_cmd(rid)
    helper.note(rid, f"Backing up {disk['name']}{' partition ' + str(part['number']) if part else ''} ({length:,} bytes) to {dest}")
    dev = _open_disk_for_read(helper, disk)
    sha = hashlib.sha256()
    zero_ranges: list[list[int]] = []
    t0 = time.time()
    pr = Progress(helper, rid, length, "read")
    pos = 0
    out = None
    try:
        if compress == "zstd":
            opts = {_zstd.CompressionParameter.compression_level: level,
                    _zstd.CompressionParameter.checksum_flag: 1}
            try:
                workers = max(1, min(8, (os.cpu_count() or 2) - 1))
                opts[_zstd.CompressionParameter.nb_workers] = workers
                out = _zstd.open(dest, "wb", options=opts)
            except Exception:
                opts.pop(_zstd.CompressionParameter.nb_workers, None)
                out = _zstd.open(dest, "wb", options=opts)
        else:
            out = open(dest, "wb")
            _make_sparse(out)
        while pos < length:
            helper.check_cancel()
            n = min(BLOCK, length - pos)
            data = dev.read(start + pos, n)
            if len(data) < n:
                raise RuntimeError(f"Short read at offset {start + pos} (the device returned {len(data)} of {n} bytes).")
            sha.update(data)
            if data == ZERO[:n]:
                if zero_ranges and zero_ranges[-1][0] + zero_ranges[-1][1] == pos:
                    zero_ranges[-1][1] += n
                else:
                    zero_ranges.append([pos, n])
                if compress == "zstd":
                    out.write(data)
                else:
                    out.seek(n, os.SEEK_CUR)   # leave a hole
            else:
                out.write(data)
            pos += n
            pr.tick(pos)
        if compress != "zstd":
            out.truncate(length)
        out.close()
        out = None
        pr.tick(pos, force=True)
        elapsed = time.time() - t0
        manifest = {
            "diskworks": _version(), "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"model": disk.get("model"), "serial": disk.get("serial"), "bus": disk.get("bus"), "size": disk["size"],
                       "logicalSector": disk.get("logicalSector"), "physicalSector": disk.get("physicalSector"), "table": disk.get("table"),
                       "partition": ({"number": part["number"], "start": part["start"], "size": part["size"], "fs": part.get("fs"),
                                      "label": part.get("label"), "typeGuid": part.get("typeGuid")} if part else None),
                       "disk": disk["name"], "path": disk["path"]},
            "image": {"file": os.path.basename(dest), "compression": compress if compress == "zstd" else None, "level": level if compress == "zstd" else None,
                      "rawSize": length, "sha256": sha.hexdigest(), "zeroRanges": zero_ranges[:5000], "zeroBytes": sum(r[1] for r in zero_ranges),
                      "fileSize": os.path.getsize(dest)},
            "layout": {k: v for k, v in disk.items() if k != "segments"},
        }
        mpath = dest + ".json"
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        _chown_to_user(helper, dest)
        _chown_to_user(helper, mpath)
        log(f"read {disk['path']} -> {dest}", 0, elapsed * 1000, f"{pos:,} bytes, {manifest['image']['fileSize']:,} on disk", "Back up")
        return {"read": pos, "fileSize": manifest["image"]["fileSize"], "sha256": sha.hexdigest(), "manifest": mpath,
                "zeroBytes": manifest["image"]["zeroBytes"], "elapsed": elapsed}
    finally:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
        dev.close()


def _make_sparse(f) -> None:
    if not IS_WIN:
        return
    try:
        import ctypes
        import ctypes.wintypes as wt
        import msvcrt
        h = msvcrt.get_osfhandle(f.fileno())
        got = wt.DWORD(0)
        ctypes.windll.kernel32.DeviceIoControl(wt.HANDLE(h), 0x000900C4, None, 0, None, 0, ctypes.byref(got), None)
    except Exception:
        pass


def _chown_to_user(helper, path: str) -> None:
    if IS_WIN or not getattr(helper, "uid", None):
        return
    try:
        import pwd
        pw = pwd.getpwuid(int(helper.uid))
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass


def _version() -> str:
    try:
        import diskworks
        return diskworks.VERSION
    except Exception:
        return "?"


# ----------------------------------------------------------------------------
# Window-process side
# ----------------------------------------------------------------------------
class ImageJobs:
    def __init__(self, jobs):
        self.jobs = jobs
        self.app = jobs.app
        self.events = self.app.log.__class__(4000)
        self.state: dict = {"running": False, "kind": None, "phase": None, "progress": None, "result": None, "error": None,
                            "started": None, "finished": None, "title": ""}
        self.thread: threading.Thread | None = None

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/image/events":
            since = int(q.get("since", ["0"])[0])
            h._json({"events": self.events.since(since), "seq": self.events.seq, "state": self.state})
            return True
        if path == "/api/image/state":
            h._json(self.state)
            return True
        if path == "/api/image/targets":
            h._json(self.targets())
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/image/inspect":
            h._json(inspect(str(body.get("path") or "")))
            return True
        if path == "/api/image/pick":
            h._json(self.pick(body))
            return True
        if path == "/api/image/write":
            h._json(self.start("image_write", body, "Writing image"))
            return True
        if path == "/api/image/backup":
            h._json(self.start("image_read", body, "Backing up"))
            return True
        if path == "/api/image/verify":
            h._json(self.start("image_verify", body, "Verifying"))
            return True
        if path == "/api/image/cancel":
            self.jobs.cancel_all()
            h._json({"ok": True})
            return True
        return False

    def targets(self) -> dict:
        inv = self.app.current_inventory()
        out = []
        for d in inv.get("disks", []):
            out.append({"id": d["id"], "name": d["name"], "model": d.get("model"), "serial": d.get("serial"), "bus": d.get("bus"),
                        "size": d["size"], "removable": bool(d.get("removable") or d.get("hotplug") or d.get("bus") in ("USB", "SD", "MMC")),
                        "system": bool(d.get("system") or d.get("boot")), "locked": d.get("locked") or [],
                        "letters": [p["letter"] + ":" for p in d["partitions"] if p.get("letter")] +
                                   [m for p in d["partitions"] for m in (p.get("mountpoints") or []) if not m.endswith(":\\")],
                        "partitions": [{"id": p["id"], "number": p["number"], "title": p.get("label") or p.get("typeName"), "size": p["size"],
                                        "fs": p.get("fs"), "letter": p.get("letter"), "start": p["start"]} for p in d["partitions"]],
                        "table": d.get("table"), "logicalSector": d.get("logicalSector")})
        out.sort(key=lambda t: (t["system"], not t["removable"], t["name"]))
        return {"targets": out, "hash": inv.get("hash")}

    def pick(self, body: dict) -> dict:
        kind = body.get("kind") or "open"
        if not (self.app.windowed and self.app.windows):
            return {"path": None, "manual": True}
        import webview
        win = self.app.windows[0]
        try:
            if kind == "save":
                res = win.create_file_dialog(webview.FileDialog.SAVE, directory=body.get("dir") or _docs_dir(),
                                             save_filename=body.get("name") or "backup.img.zst")
            else:
                res = win.create_file_dialog(webview.FileDialog.OPEN, directory=body.get("dir") or _docs_dir(), allow_multiple=False,
                                             file_types=("Disk images (*.iso;*.img;*.raw;*.dd;*.vhd;*.gz;*.xz;*.zst;*.bz2)", "All files (*.*)"))
        except Exception as e:
            raise RuntimeError(f"The file dialog could not be opened: {e}")
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        return {"path": res}

    def start(self, verb: str, body: dict, title: str) -> dict:
        with self.jobs.lock:
            if self.jobs.running() or self.running():
                raise RuntimeError("Another job is still running.")
            helper = self.jobs.helper()
            inv = self.app.current_inventory()
            disk = next((d for d in inv["disks"] if d["id"] == body.get("disk")), None)
            if not disk:
                raise RuntimeError("Pick a drive.")
            if verb in ("image_write",):
                _guard_target(disk, False)
                if not body.get("confirm"):
                    raise RuntimeError("Confirm the target drive first.")
                if not body.get("path"):
                    raise RuntimeError("Pick an image file first.")
            if verb == "image_read" and not body.get("dest"):
                raise RuntimeError("Choose where to save the backup.")
            args = {k: body.get(k) for k in ("path", "disk", "part", "dest", "compress", "level", "verify") if k in body}
            self.events.clear()
            self.state = {"running": True, "kind": verb, "phase": "start", "progress": None, "result": None, "error": None,
                          "started": time.time(), "finished": None, "title": title, "disk": disk["name"], "model": disk.get("model")}
            self.events.push({"type": "start", "title": title, "disk": disk["name"]})
            self.jobs.cancel.clear()
            self.thread = threading.Thread(target=self._run, args=(helper, verb, args), daemon=True, name="image-job")
            self.jobs.thread = self.thread
            self.thread.start()
            return {"ok": True}

    def _run(self, helper, verb: str, args: dict) -> None:
        def on_event(msg):
            if msg.get("event") == "progress":
                self.state["phase"] = msg.get("phase")
                self.state["progress"] = {k: msg.get(k) for k in ("phase", "bytes", "total", "speed", "eta", "elapsed")}
                self.events.push({"type": "progress", **self.state["progress"]})
            else:
                self.jobs._forward_log(msg)
                if msg.get("msg"):
                    self.events.push({"type": "note", "msg": msg["msg"]})
        try:
            res = helper.request(verb, args, on_event=on_event, timeout=7 * 24 * 3600)
            res.pop("id", None)
            res.pop("event", None)
            self.state["result"] = res
            self.events.push({"type": "done", "ok": True, "result": res})
            self.app.info(f"{self.state.get('title')} finished on {self.state.get('disk')}")
        except RuntimeError as e:
            self.state["error"] = str(e)
            self.events.push({"type": "done", "ok": False, "error": str(e)})
            self.app.info(f"{self.state.get('title')} stopped: {e}")
        finally:
            self.state.update({"running": False, "finished": time.time()})
            self.app.inv_wanted.set()


def _docs_dir() -> str:
    for name in ("Documents", "Downloads"):
        d = os.path.join(os.path.expanduser("~"), name)
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")
