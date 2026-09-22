"""The operation engine: pending operations -> validated plan -> preview layout + ordered
platform steps with command previews (ARCHITECTURE.md §4).

Both processes use this module: the window process to plan and preview, the helper to
rebuild each step's command from its structured arguments (it never trusts command
text sent over the wire).

Operation dicts (from the UI):
  {"op": "create", "gap": id, "disk": id, "start": bytes, "size": bytes, "fs": "ntfs", "label": "", "letter": "E"|None,
   "typeGuid": guid|None, "mbrType": int|None, "quick": True, "name": ""}
  {"op": "delete", "part": id}
  {"op": "format", "part": id, "fs": "ext4", "label": "", "quick": True, "cluster": int|None}
  {"op": "label", "part": id, "label": ""}
  {"op": "letter", "part": id, "letter": "F"|None}            (Windows)
  {"op": "resize", "part": id, "size": bytes}                 (start stays)
  {"op": "check", "part": id, "repair": bool}
  {"op": "table", "disk": id, "table": "gpt"|"mbr"}           (destructive: removes every partition)
  {"op": "wipe", "disk": id, "zero": bool}

Step dicts (to the helper):
  {"n": i, "op": opIndex, "title": "...", "kind": "ps"|"diskpart"|"tool"|"sfdisk"|"unmount"|"note"|"raw_zero",
   "args": {...}, "cmd": "preview text", "disk": diskId, "destructive": bool}
"""
from __future__ import annotations

import copy
import re

import dw_fs
import dw_inventory as di

ALIGN = 2**20
BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"
LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
LINUX_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class PlanError(ValueError):
    pass


def _align_down(n: int) -> int:
    return n - (n % ALIGN)


def _align_up(n: int) -> int:
    return ((n + ALIGN - 1) // ALIGN) * ALIGN


def default_type(fs: str | None, table: str) -> tuple[str | None, int | None]:
    fam = dw_fs.FS.get(fs or "", {}).get("family")
    if table == "gpt":
        if fs == "swap":
            return LINUX_SWAP, None
        return (LINUX_FS if fam == "linux" else BASIC_DATA), None
    if fs == "swap":
        return None, 0x82
    if fam == "linux":
        return None, 0x83
    if fs in ("fat32",):
        return None, 0x0c
    if fs in ("fat16",):
        return None, 0x06
    return None, 0x07


def part_device(disk: dict, number: int) -> str:
    """Device path of partition `number` on `disk`: Linux /dev/sda3, /dev/nvme0n1p3, /dev/loop0p1;
    macOS /dev/disk2s3."""
    base = disk["path"]
    if base.startswith("/dev/disk"):
        return f"{base}s{number}"
    if re.search(r"\d$", base):
        return f"{base}p{number}"
    return f"{base}{number}"


MAC_FORMATS = {"apfs": "APFS", "hfsplus": "JHFS+", "exfat": "ExFAT", "fat32": "MS-DOS FAT32", "fat16": "MS-DOS FAT16"}


def mac_format(fs: str | None) -> str:
    return MAC_FORMATS.get(fs or "", "%noformat%")


def check_label(fs: str | None, label: str) -> str:
    label = (label or "").strip()
    limits = {"ntfs": 32, "exfat": 11, "fat32": 11, "fat16": 11, "refs": 32, "ext4": 16, "ext3": 16, "ext2": 16,
              "xfs": 12, "btrfs": 255, "f2fs": 512, "swap": 15}
    lim = limits.get(fs or "", 32)
    if len(label) > lim:
        raise PlanError(f"A {dw_fs.fs_label(fs)} label can be at most {lim} characters.")
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', label):
        raise PlanError('The label cannot contain \\ / : * ? " < > |')
    return label


def check_letter(letter, inv: dict) -> str | None:
    if letter in (None, "", "none", "auto"):
        return None if letter in (None, "", "none") else "auto"
    letter = str(letter).strip().rstrip(":").upper()
    if letter not in LINUX_LETTERS or letter in ("A", "B"):
        raise PlanError("Pick a drive letter from C to Z.")
    used = {p.get("letter") for d in inv["disks"] for p in d["partitions"] if p.get("letter")}
    if letter in used:
        raise PlanError(f"Drive letter {letter}: is already in use.")
    return letter


# ----------------------------------------------------------------------------
# Planner
# ----------------------------------------------------------------------------
class Planner:
    def __init__(self, inv: dict, platform: str):
        self.inv = copy.deepcopy(inv)
        self.platform = platform
        self.win = platform == "win32"
        self.mac = platform == "darwin"
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.texts: list[str] = []
        self.new_count = 0
        self.destructive = False
        self.touched: set[str] = set()

    # -- lookups ---------------------------------------------------------------
    def disk(self, disk_id: str) -> dict:
        d = di_find_disk(self.inv, disk_id)
        if not d:
            raise PlanError("That disk is no longer present.")
        return d

    def part(self, part_id: str) -> tuple[dict, dict]:
        for d in self.inv["disks"]:
            for p in d["partitions"]:
                if p["id"] == part_id:
                    return d, p
        raise PlanError("That partition is no longer present (the disks changed). Review the queue.")

    def gap(self, gap_id: str) -> tuple[dict, dict]:
        for d in self.inv["disks"]:
            for g in d.get("gaps", []):
                if g["id"] == gap_id:
                    return d, g
        raise PlanError("That unallocated space is no longer there (the disks changed). Review the queue.")

    def dev_of(self, d: dict, p: dict) -> str | None:
        """Linux / macOS device path; for a partition queued for creation it is derived from its number."""
        return p.get("device") or (part_device(d, int(p["number"])) if not self.win else None)

    def mac_name(self, p: dict, fallback: str = "Untitled") -> str:
        return (p.get("label") or p.get("name") or fallback)[:64]

    def refresh(self, d: dict) -> None:
        d["partitions"].sort(key=lambda p: p["start"])
        for p in d["partitions"]:
            p["end"] = p["start"] + p["size"]
        di.compute_gaps(d)
        di.apply_protection(d)

    def require_unlocked(self, d: dict, p: dict | None, op: str) -> None:
        if p is not None and p.get("locked") and op not in (p.get("allow") or []):
            raise PlanError(f"{title_of(p)}: {'; '.join(p['locked'])}.")
        if p is None and d.get("locked"):
            raise PlanError(f"{d['name']}: {'; '.join(d['locked'])}.")

    def step(self, op_index: int, title: str, kind: str, args: dict, disk: dict, destructive: bool = False) -> dict:
        s = {"n": len(self.steps) + 1, "op": op_index, "title": title, "kind": kind, "args": args,
             "disk": disk["id"], "diskPath": disk["path"], "diskNumber": disk.get("number"), "destructive": destructive}
        s["cmd"] = build_command(s, self.platform)
        self.steps.append(s)
        self.touched.add(disk["id"])
        if destructive:
            self.destructive = True
        return s

    # -- operations ----------------------------------------------------------------
    def apply(self, i: int, op: dict) -> None:
        kind = op.get("op")
        fn = getattr(self, "op_" + str(kind), None)
        if not fn:
            raise PlanError(f"Unknown operation '{kind}'.")
        fn(i, op)

    def gap_for(self, op: dict) -> tuple[dict, dict]:
        """The gap named by the op, or - when earlier queued ops moved it - the gap on the same
        disk that now contains the requested start (gap ids embed their start offset)."""
        gid = str(op.get("gap") or "")
        try:
            return self.gap(gid)
        except PlanError:
            pass
        disk_id = op.get("disk")
        if not disk_id and gid.startswith("gap:"):
            disk_id = gid[4:].rsplit(":", 1)[0]
        d = self.disk(str(disk_id))
        gaps = d.get("gaps", [])
        start = op.get("start")
        if start is None and gaps:
            return d, gaps[0]
        if start is not None:
            for g in gaps:
                if g["start"] <= int(start) < g["start"] + g["size"]:
                    return d, g
            for g in gaps:
                if g["start"] >= int(start):
                    return d, g
        raise PlanError("That unallocated space is no longer there (the disks changed). Review the queue.")

    def op_create(self, i: int, op: dict) -> None:
        d, g = self.gap_for(op)
        self.require_unlocked(d, None, "create")
        if d["table"] not in ("gpt", "mbr"):
            raise PlanError(f"{d['name']} has no partition table yet. Add 'New partition table' first.")
        start = int(op.get("start") or g["start"])
        size = int(op.get("size") or (g["start"] + g["size"] - start))
        start = max(g["start"], _align_up(start))
        # Partitions begin and end on 1 MiB boundaries. The end never goes past the aligned
        # end of the gap - Windows' own free-extent limit is the same aligned position.
        end = _align_down(min(g["start"] + g["size"], start + size))
        size = end - start
        if size < 4 * ALIGN:
            raise PlanError("There is not enough usable space here for a partition of at least 4 MiB "
                            "(partitions are placed on 1 MiB boundaries).")
        fs = op.get("fs") or None
        if fs and fs not in dw_fs.FS:
            raise PlanError(f"Unknown filesystem '{fs}'.")
        if fs and dw_fs.can(fs, "create", self.platform) is None:
            raise PlanError(f"{dw_fs.fs_label(fs)} cannot be created on this system. {create_hint(fs, self.platform)}")
        info = dw_fs.FS.get(fs or "", {})
        if fs and info.get("min") and size < info["min"]:
            raise PlanError(f"{dw_fs.fs_label(fs)} needs at least {di_fmt(info['min'])}.")
        if fs and info.get("max") and size > info["max"]:
            raise PlanError(f"{dw_fs.fs_label(fs)} cannot be larger than {di_fmt(info['max'])}.")
        if d["table"] == "mbr" and len([p for p in d["partitions"] if p.get("mbrType") not in (0x05, 0x0f, 0x85)]) >= 4:
            raise PlanError("An MBR disk can hold at most 4 primary partitions; use GPT for more.")
        label = check_label(fs, op.get("label") or "")
        letter = check_letter(op.get("letter"), self.inv) if self.win else None
        type_guid, mbr_type = default_type(fs, d["table"])
        if op.get("typeGuid"):
            type_guid = str(op["typeGuid"]).strip("{}").lower()
        if op.get("mbrType") not in (None, ""):
            mbr_type = int(op["mbrType"])
        self.new_count += 1
        number = max([p["number"] for p in d["partitions"]] + [0]) + 1
        p = {
            "id": f"new:{self.new_count}", "disk": d["id"], "number": number, "start": start, "size": size, "end": start + size,
            "typeGuid": type_guid if d["table"] == "gpt" else None, "mbrType": mbr_type if d["table"] == "mbr" else None,
            "typeName": dw_fs.gpt_type_name(type_guid) if d["table"] == "gpt" else dw_fs.mbr_type_name(mbr_type),
            "guid": None, "name": (op.get("name") or "") or None, "fs": fs, "fsSource": "planned", "label": label, "uuid": None,
            "letter": letter if letter not in ("auto",) else None, "mountpoints": [], "used": None, "free": None,
            "flags": {}, "health": "", "device": None, "pendingOp": "create", "new": True,
        }
        d["partitions"].append(p)
        self.refresh(d)
        self.texts.append(f"Create a {di_fmt(size)} {dw_fs.fs_label(fs) if fs else 'unformatted'} partition on {d['name']}"
                          + (f" labelled '{label}'" if label else "") + (f" as {letter}:" if letter and letter != 'auto' else ""))
        if self.win:
            self.step(i, f"Create partition on {d['name']}", "ps", {
                "action": "create", "disk": d["number"], "offset": start, "size": size, "gptType": p["typeGuid"],
                "mbrType": p["mbrType"], "fs": fs, "label": label, "letter": letter, "quick": op.get("quick", True),
                "cluster": op.get("cluster")}, d, destructive=False)
        elif self.mac:
            others = [q for q in d["partitions"] if q is not p]
            prev = [q for q in others if q["start"] + q["size"] <= start]
            if not others:
                self.step(i, f"Create partition on {d['name']}", "diskutil", {
                    "action": "partitionDisk", "table": d["table"], "format": mac_format(fs), "name": label or "Untitled",
                    "size": size, "fs": fs}, d, destructive=False)
            elif prev:
                after = max(prev, key=lambda q: q["start"])
                self.step(i, f"Create partition on {d['name']}", "diskutil", {
                    "action": "addPartition", "after": part_device(d, int(after["number"])), "format": mac_format(fs),
                    "name": label or "Untitled", "size": size, "fs": fs}, d, destructive=False)
            else:
                raise PlanError("macOS can only add a partition after an existing one; use the free space at the end of the disk.")
        else:
            self.step(i, f"Create partition on {d['name']}", "sfdisk", {
                "action": "append", "start": start, "size": size, "type": p["typeGuid"] or (f"{mbr_type:02x}" if mbr_type is not None else None),
                "name": p["name"], "number": number}, d)
            if fs:
                self.step(i, f"Format the new partition as {dw_fs.fs_label(fs)}", "tool", {
                    "action": "mkfs", "fs": fs, "label": label, "device": part_device(d, number), "quick": op.get("quick", True),
                    "cluster": op.get("cluster"), "expectStart": start}, d)

    def op_delete(self, i: int, op: dict) -> None:
        d, p = self.part(str(op.get("part")))
        if p.get("new"):
            raise PlanError("Remove the pending 'create' from the queue instead of deleting the new partition.")
        self.require_unlocked(d, p, "delete")
        d["partitions"].remove(p)
        self.refresh(d)
        self.texts.append(f"Delete {title_of(p)} ({di_fmt(p['size'])}) on {d['name']}")
        if self.win:
            self.step(i, f"Delete {title_of(p)}", "ps", {"action": "delete", "disk": d["number"], "offset": p["start"]}, d, destructive=True)
        elif self.mac:
            self.step(i, f"Delete {title_of(p)}", "diskutil", {"action": "eraseVolume", "format": "Free Space", "name": "%noformat%",
                                                                "device": self.dev_of(d, p), "expectStart": p["start"]}, d, destructive=True)
        else:
            self.step(i, f"Unmount {title_of(p)}", "unmount", {"device": p["device"]}, d)
            self.step(i, f"Delete {title_of(p)}", "sfdisk", {"action": "delete", "number": p["number"], "expectStart": p["start"]}, d, destructive=True)

    def op_format(self, i: int, op: dict) -> None:
        d, p = self.part(str(op.get("part")))
        self.require_unlocked(d, p, "format")
        fs = op.get("fs")
        if fs not in dw_fs.FS or dw_fs.FS[fs].get("detect_only"):
            raise PlanError(f"Unknown filesystem '{fs}'.")
        if dw_fs.can(fs, "create", self.platform) is None:
            raise PlanError(f"{dw_fs.fs_label(fs)} cannot be created on this system. {create_hint(fs, self.platform)}")
        info = dw_fs.FS[fs]
        if info.get("min") and p["size"] < info["min"]:
            raise PlanError(f"{dw_fs.fs_label(fs)} needs at least {di_fmt(info['min'])}; this partition is {di_fmt(p['size'])}.")
        if info.get("max") and p["size"] > info["max"]:
            raise PlanError(f"{dw_fs.fs_label(fs)} cannot be larger than {di_fmt(info['max'])}.")
        label = check_label(fs, op.get("label") or "")
        old = p.get("fs")
        p.update({"fs": fs, "fsSource": "planned", "label": label, "used": None, "free": None, "volSize": None, "pendingOp": "format"})
        if d["table"] == "gpt" and dw_fs.FS[fs]["family"] != dw_fs.FS.get(old or "", {}).get("family") and p.get("typeGuid") in (BASIC_DATA, LINUX_FS, LINUX_SWAP, None):
            p["typeGuid"], _ = default_type(fs, "gpt")
            p["typeName"] = dw_fs.gpt_type_name(p["typeGuid"])
        self.refresh(d)
        self.texts.append(f"Format {title_of(p)} on {d['name']} as {dw_fs.fs_label(fs)}" + (f" labelled '{label}'" if label else ""))
        if self.win:
            self.step(i, f"Format {title_of(p)} as {dw_fs.fs_label(fs)}", "ps", {
                "action": "format", "disk": d["number"], "offset": p["start"], "fs": fs, "label": label,
                "quick": op.get("quick", True), "cluster": op.get("cluster"), "gptType": p.get("typeGuid")}, d, destructive=True)
        elif self.mac:
            self.step(i, f"Format {title_of(p)} as {dw_fs.fs_label(fs)}", "diskutil", {
                "action": "eraseVolume", "format": mac_format(fs), "name": label or "Untitled", "device": self.dev_of(d, p),
                "expectStart": p["start"], "fs": fs}, d, destructive=True)
        else:
            dev = self.dev_of(d, p)
            self.step(i, f"Unmount {title_of(p)}", "unmount", {"device": dev}, d)
            if d["table"] == "gpt" and p.get("typeGuid"):
                self.step(i, "Set the partition type", "sfdisk", {"action": "type", "number": p["number"], "type": p["typeGuid"], "expectStart": p["start"]}, d)
            self.step(i, f"Format {title_of(p)} as {dw_fs.fs_label(fs)}", "tool", {
                "action": "mkfs", "fs": fs, "label": label, "device": dev, "quick": op.get("quick", True),
                "cluster": op.get("cluster"), "expectStart": p["start"]}, d, destructive=True)

    def op_label(self, i: int, op: dict) -> None:
        d, p = self.part(str(op.get("part")))
        self.require_unlocked(d, p, "label")
        if not p.get("fs"):
            raise PlanError("This partition has no filesystem to label.")
        if dw_fs.can(p["fs"], "label", self.platform) is None:
            raise PlanError(f"{dw_fs.fs_label(p['fs'])} labels cannot be changed on this system.")
        label = check_label(p["fs"], op.get("label") or "")
        p["label"] = label
        p["pendingOp"] = p.get("pendingOp") or "label"
        self.texts.append(f"Label {title_of(p)} as '{label}'")
        if self.win:
            self.step(i, f"Set label '{label}'", "ps", {"action": "label", "disk": d["number"], "offset": p["start"], "label": label}, d)
        elif self.mac:
            self.step(i, f"Set label '{label}'", "diskutil", {"action": "rename", "device": self.dev_of(d, p), "name": label or "Untitled",
                                                            "expectStart": p["start"]}, d)
        else:
            dev = self.dev_of(d, p)
            if dw_fs.can(p["fs"], "label", "linux") == "off":
                self.step(i, f"Unmount {title_of(p)}", "unmount", {"device": dev}, d)
            self.step(i, f"Set label '{label}'", "tool", {"action": "label", "fs": p["fs"], "label": label, "device": dev, "expectStart": p["start"]}, d)

    def op_letter(self, i: int, op: dict) -> None:
        if not self.win:
            raise PlanError("Drive letters exist only on Windows.")
        d, p = self.part(str(op.get("part")))
        self.require_unlocked(d, p, "letter")
        letter = check_letter(op.get("letter"), self.inv)
        old = p.get("letter")
        p["letter"] = None if letter in (None, "auto") else letter
        p["pendingOp"] = p.get("pendingOp") or "letter"
        self.texts.append(f"{'Remove the drive letter of' if not letter else 'Give ' + letter + ': to'} {title_of(p)}")
        self.step(i, f"Drive letter {letter or 'removed'} for {title_of(p)}", "ps",
                  {"action": "letter", "disk": d["number"], "offset": p["start"], "letter": letter, "old": old}, d)

    def op_resize(self, i: int, op: dict) -> None:
        d, p = self.part(str(op.get("part")))
        self.require_unlocked(d, p, "resize")
        new = int(op.get("size") or 0)
        new = _align_up(new) if new < p["size"] else new
        if new == p["size"]:
            raise PlanError("The size did not change.")
        fs = p.get("fs")
        grow = new > p["size"]
        cap = dw_fs.can(fs, "grow" if grow else "shrink", self.platform) if fs else "off"
        if fs and cap is None:
            raise PlanError(f"{dw_fs.fs_label(fs)} cannot be {'grown' if grow else 'shrunk'} on this system.")
        if fs in ("fat32", "fat16") and not self.win:
            raise PlanError("FAT resizing is not available in this build yet.")
        # room: from the partition start to the start of the next partition (or the end of the usable disk)
        nxt = [q for q in d["partitions"] if q["start"] > p["start"]]
        limit = min(q["start"] for q in nxt) if nxt else usable_end(d)
        if p["start"] + new > limit:
            raise PlanError(f"Only {di_fmt(limit - p['start'])} is available to the right of this partition.")
        minimum = int(op.get("minSize") or 0) or (p.get("used") or 0)
        if not grow and fs and new < minimum:
            raise PlanError(f"The filesystem needs at least {di_fmt(minimum)}.")
        info = dw_fs.FS.get(fs or "", {})
        if fs and info.get("min") and new < info["min"]:
            raise PlanError(f"{dw_fs.fs_label(fs)} needs at least {di_fmt(info['min'])}.")
        old = p["size"]
        p["size"] = new
        p["pendingOp"] = "resize"
        self.refresh(d)
        self.texts.append(f"{'Grow' if grow else 'Shrink'} {title_of(p)} from {di_fmt(old)} to {di_fmt(new)}")
        if self.win:
            self.step(i, f"{'Grow' if grow else 'Shrink'} {title_of(p)} to {di_fmt(new)}", "ps",
                      {"action": "resize", "disk": d["number"], "offset": p["start"], "size": new}, d, destructive=not grow)
        elif self.mac:
            self.step(i, f"{'Grow' if grow else 'Shrink'} {title_of(p)} to {di_fmt(new)}", "diskutil",
                      {"action": "resizeContainer" if fs == "apfs" else "resizeVolume", "device": self.dev_of(d, p), "size": new,
                       "expectStart": p["start"], "fs": fs}, d, destructive=not grow)
        else:
            dev = self.dev_of(d, p)
            if fs and cap == "off":
                self.step(i, f"Unmount {title_of(p)}", "unmount", {"device": dev}, d)
            if grow:
                self.step(i, "Grow the partition", "sfdisk", {"action": "resize", "number": p["number"], "size": new, "expectStart": p["start"]}, d)
                if fs:
                    self.step(i, f"Grow the {dw_fs.fs_label(fs)} filesystem", "tool", {"action": "fsresize", "fs": fs, "device": dev, "size": None, "expectStart": p["start"]}, d)
            else:
                if fs:
                    self.step(i, f"Check the {dw_fs.fs_label(fs)} filesystem", "tool", {"action": "fsck_before", "fs": fs, "device": dev, "expectStart": p["start"]}, d)
                    self.step(i, f"Shrink the {dw_fs.fs_label(fs)} filesystem to {di_fmt(new)}", "tool", {"action": "fsresize", "fs": fs, "device": dev, "size": new, "expectStart": p["start"]}, d, destructive=True)
                self.step(i, "Shrink the partition", "sfdisk", {"action": "resize", "number": p["number"], "size": new, "expectStart": p["start"]}, d, destructive=True)
            if fs == "ntfs":
                self.warnings.append("After resizing NTFS from Linux, boot Windows twice so it runs its own checks.")

    def op_check(self, i: int, op: dict) -> None:
        d, p = self.part(str(op.get("part")))
        self.require_unlocked(d, p, "check")
        fs = p.get("fs")
        if not fs or dw_fs.can(fs, "check", self.platform) is None:
            raise PlanError("There is no checker for this filesystem on this system.")
        repair = bool(op.get("repair"))
        self.texts.append(f"{'Check and repair' if repair else 'Check'} {title_of(p)}")
        if self.win:
            self.step(i, f"Check {title_of(p)}", "ps", {"action": "check", "disk": d["number"], "offset": p["start"], "repair": repair, "letter": p.get("letter")}, d)
        elif self.mac:
            self.step(i, f"Check {title_of(p)}", "diskutil", {"action": "repairVolume" if repair else "verifyVolume", "device": self.dev_of(d, p),
                                                            "expectStart": p["start"]}, d)
        else:
            dev = self.dev_of(d, p)
            self.step(i, f"Unmount {title_of(p)}", "unmount", {"device": dev}, d)
            self.step(i, f"Check {title_of(p)}", "tool", {"action": "fsck", "fs": fs, "device": dev, "repair": repair, "expectStart": p["start"]}, d)

    def op_table(self, i: int, op: dict) -> None:
        d = self.disk(str(op.get("disk")))
        self.require_unlocked(d, None, "table")
        table = str(op.get("table") or "gpt").lower()
        if table not in ("gpt", "mbr"):
            raise PlanError("The partition table must be GPT or MBR.")
        had = len(d["partitions"])
        d["partitions"] = []
        d["table"] = table
        if self.win and table == "gpt":
            # Initialize-Disk / Set-Disk always add a 16 MiB Microsoft Reserved partition first.
            head, _ = di._gpt_reserve(int(d.get("logicalSector") or 512))
            d["partitions"].append({
                "id": f"new:msr:{d['id']}", "disk": d["id"], "number": 1, "start": head, "size": 16 * 2**20, "end": head + 16 * 2**20,
                "typeGuid": "e3c9e316-0b5c-4db8-817d-f92df00215ae", "mbrType": None, "typeName": "Microsoft Reserved", "guid": None,
                "name": None, "fs": None, "fsSource": None, "label": "", "uuid": None, "letter": None, "mountpoints": [],
                "used": None, "free": None, "flags": {"hidden": True}, "health": "", "device": None, "pendingOp": "create", "new": True})
        self.refresh(d)
        self.texts.append(f"New {table.upper()} partition table on {d['name']}" + (f" (removes {had} partition{'s' if had != 1 else ''})" if had else "")
                          + (" — Windows adds a 16 MiB reserved partition" if self.win and table == "gpt" else ""))
        if self.win:
            self.step(i, f"New {table.upper()} table on {d['name']}", "ps", {"action": "table", "disk": d["number"], "table": table, "hadPartitions": had > 0}, d, destructive=had > 0)
        elif self.mac:
            self.step(i, f"New {table.upper()} table on {d['name']}", "diskutil", {"action": "newTable", "table": table}, d, destructive=had > 0)
        else:
            self.step(i, f"Unmount everything on {d['name']}", "unmount", {"device": d["path"], "all": True}, d)
            self.step(i, f"New {table.upper()} table on {d['name']}", "sfdisk", {"action": "label", "table": table}, d, destructive=had > 0)

    def op_wipe(self, i: int, op: dict) -> None:
        d = self.disk(str(op.get("disk")))
        self.require_unlocked(d, None, "wipe")
        had = len(d["partitions"])
        d["partitions"] = []
        d["table"] = "none"
        self.refresh(d)
        self.texts.append(f"Wipe {d['name']} (remove the partition table" + (" and zero the first and last MiB" if op.get("zero") else "") + ")")
        if self.win:
            self.step(i, f"Wipe {d['name']}", "ps", {"action": "wipe", "disk": d["number"]}, d, destructive=True)
        elif self.mac:
            self.step(i, f"Wipe {d['name']}", "diskutil", {"action": "newTable", "table": "gpt", "wipe": True}, d, destructive=True)
        else:
            self.step(i, f"Unmount everything on {d['name']}", "unmount", {"device": d["path"], "all": True}, d)
            self.step(i, f"Wipe {d['name']}", "tool", {"action": "wipefs", "device": d["path"]}, d, destructive=True)
        if op.get("zero"):
            self.step(i, "Zero the first and last MiB", "raw_zero", {"headTail": True}, d, destructive=True)


def usable_end(d: dict) -> int:
    """Last byte a partition may reach: below the GPT backup header, on a 1 MiB boundary
    (what Windows reports as the largest free extent and what sfdisk aligns to)."""
    if d.get("table") == "gpt":
        _, tail = di._gpt_reserve(int(d.get("logicalSector") or 512))
        return _align_down(int(d["size"]) - tail)
    return _align_down(int(d["size"]))


def di_find_disk(inv: dict, disk_id: str) -> dict | None:
    for d in inv.get("disks", []):
        if d["id"] == disk_id:
            return d
    return None


def di_fmt(n: int) -> str:
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f} {u}" if n >= 100 or u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TiB"


def title_of(p: dict) -> str:
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


def create_hint(fs: str, platform: str) -> str:
    if platform == "win32" and dw_fs.FS.get(fs, {}).get("family") == "linux":
        return "Use the Access tab to attach the disk to WSL and format it there, or create it from Linux."
    if fs == "refs":
        return "ReFS needs Windows Pro for Workstations, Enterprise or Server."
    return ""


def plan(inv: dict, ops: list[dict], platform: str) -> dict:
    """Validate and expand a queue. Never raises for per-op problems: they come back in 'errors'."""
    pl = Planner(inv, platform)
    errors: list[dict] = []
    for i, op in enumerate(ops):
        try:
            pl.apply(i, op)
        except PlanError as e:
            errors.append({"op": i, "message": str(e)})
            break
        except Exception as e:
            errors.append({"op": i, "message": f"Could not plan this operation: {e}"})
            break
    preview = pl.inv
    preview["preview"] = True
    return {"preview": preview, "steps": pl.steps, "texts": pl.texts, "warnings": pl.warnings, "errors": errors,
            "destructive": pl.destructive, "touched": sorted(pl.touched)}


# ----------------------------------------------------------------------------
# Command builders (preview text AND what the helper runs)
# ----------------------------------------------------------------------------
def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


PS_FS_NAMES = {"ntfs": "NTFS", "exfat": "exFAT", "fat32": "FAT32", "fat16": "FAT", "refs": "ReFS"}


def ps_find_partition(disk: int, offset: int) -> str:
    return (f"$p = Get-Partition -DiskNumber {int(disk)} | Where-Object {{ $_.Offset -eq {int(offset)} }}\n"
            f"if (-not $p) {{ throw 'The partition at offset {int(offset)} on disk {int(disk)} was not found (the disk changed).' }}\n")


def build_ps(a: dict) -> str:
    act = a.get("action")
    disk = int(a.get("disk"))
    lines = ["$ErrorActionPreference = 'Stop'"]
    if act == "create":
        fsn = PS_FS_NAMES.get(a.get("fs") or "")
        args = f"-DiskNumber {disk} -Offset {int(a['offset'])} -Size {int(a['size'])}"
        if a.get("gptType"):
            args += f" -GptType '{{{a['gptType']}}}'"
        elif a.get("mbrType") is not None:
            args += f" -MbrType {int(a['mbrType'])}"
        letter = a.get("letter")
        if letter == "auto":
            args += " -AssignDriveLetter"
        elif letter:
            args += f" -DriveLetter {letter}"
        lines.append(f"$p = New-Partition {args}")
        if fsn:
            fargs = f"-FileSystem {fsn}"
            if a.get("label"):
                fargs += f" -NewFileSystemLabel {ps_quote(a['label'])}"
            if a.get("cluster"):
                fargs += f" -AllocationUnitSize {int(a['cluster'])}"
            if not a.get("quick", True):
                fargs += " -Full"
            lines.append(f"$p | Format-Volume {fargs} -Confirm:$false | Out-Null")
        lines.append(f"Update-Disk -Number {disk}")
        lines.append("Write-Output ('Created partition ' + $p.PartitionNumber)")
    elif act == "delete":
        lines.append(ps_find_partition(disk, a["offset"]).rstrip())
        lines.append(f"Remove-Partition -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -Confirm:$false")
        lines.append(f"Update-Disk -Number {disk}")
    elif act == "format":
        fsn = PS_FS_NAMES.get(a.get("fs") or "")
        lines.append(ps_find_partition(disk, a["offset"]).rstrip())
        if a.get("gptType"):
            lines.append(f"if ($p.GptType -and $p.GptType -ne '{{{a['gptType']}}}') {{ Set-Partition -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -GptType '{{{a['gptType']}}}' }}")
        fargs = f"-FileSystem {fsn}"
        if a.get("label"):
            fargs += f" -NewFileSystemLabel {ps_quote(a['label'])}"
        if a.get("cluster"):
            fargs += f" -AllocationUnitSize {int(a['cluster'])}"
        if not a.get("quick", True):
            fargs += " -Full"
        lines.append(f"$p | Format-Volume {fargs} -Confirm:$false | Out-Null")
        lines.append(f"Update-Disk -Number {disk}")
    elif act == "label":
        lines.append(ps_find_partition(disk, a["offset"]).rstrip())
        lines.append(f"$p | Get-Volume | Set-Volume -NewFileSystemLabel {ps_quote(a.get('label') or '')}")
    elif act == "letter":
        lines.append(ps_find_partition(disk, a["offset"]).rstrip())
        # the current letter is read live: a partition created earlier in the same queue got its letter only then
        lines.append("$cur = [string]$p.DriveLetter; if ($cur -eq [string][char]0) { $cur = '' }")
        if a.get("letter") == "auto":
            lines.append(f"if (-not $cur.Trim()) {{ Add-PartitionAccessPath -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -AssignDriveLetter }}")
        elif a.get("letter"):
            lines.append(f"Set-Partition -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -NewDriveLetter {a['letter']}")
        else:
            lines.append(f"if ($cur.Trim()) {{ Remove-PartitionAccessPath -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -AccessPath ($cur.Trim() + ':\\') }}")
        lines.append(f"Update-Disk -Number {disk}")
    elif act == "resize":
        lines.append(ps_find_partition(disk, a["offset"]).rstrip())
        lines.append(f"$s = Get-PartitionSupportedSize -DiskNumber {disk} -PartitionNumber $p.PartitionNumber")
        lines.append(f"$want = [uint64]{int(a['size'])}")
        lines.append("if ($want -lt $s.SizeMin) { throw ('Windows can shrink this volume to at most ' + [math]::Round($s.SizeMin/1MB) + ' MiB (files that cannot be moved are in the way).') }")
        lines.append("if ($want -gt $s.SizeMax) { throw ('This partition can grow to at most ' + [math]::Round($s.SizeMax/1MB) + ' MiB.') }")
        lines.append(f"Resize-Partition -DiskNumber {disk} -PartitionNumber $p.PartitionNumber -Size $want")
        lines.append(f"Update-Disk -Number {disk}")
    elif act == "check":
        letter = a.get("letter")
        if letter:
            lines.append(f"Repair-Volume -DriveLetter {letter} {'-OfflineScanAndFix' if a.get('repair') else '-Scan'}")
        else:
            lines.append(ps_find_partition(disk, a["offset"]).rstrip())
            lines.append(f"$p | Get-Volume | Repair-Volume {'-OfflineScanAndFix' if a.get('repair') else '-Scan'}")
    elif act == "table":
        table = "GPT" if a.get("table") == "gpt" else "MBR"
        if a.get("hadPartitions"):
            lines.append(f"Clear-Disk -Number {disk} -RemoveData -RemoveOEM -Confirm:$false")
        lines.append(f"$d = Get-Disk -Number {disk}")
        lines.append(f"if ($d.PartitionStyle -eq 'RAW') {{ Initialize-Disk -Number {disk} -PartitionStyle {table} }} else {{ Set-Disk -Number {disk} -PartitionStyle {table} }}")
        lines.append(f"Update-Disk -Number {disk}")
    elif act == "wipe":
        lines.append(f"Clear-Disk -Number {disk} -RemoveData -RemoveOEM -Confirm:$false")
        lines.append(f"Update-Disk -Number {disk}")
    else:
        raise ValueError(f"unknown PowerShell action {act}")
    return "\n".join(lines)


def build_tool(a: dict) -> list[str]:
    """argv for a Linux filesystem tool step."""
    act = a.get("action")
    fs = a.get("fs")
    dev = a.get("device")
    label = a.get("label") or ""
    if act == "mkfs":
        quick = a.get("quick", True)
        cluster = a.get("cluster")
        if fs == "ext4" or fs == "ext3" or fs == "ext2":
            argv = [f"mkfs.{fs}", "-F", "-q"]
            if fs == "ext4":
                argv += ["-O", "^orphan_file,^metadata_csum_seed"]   # N-007: mountable by older kernels and Windows drivers
            if cluster:
                argv += ["-b", str(cluster)]
            if label:
                argv += ["-L", label]
            return argv + [dev]
        if fs == "xfs":
            return ["mkfs.xfs", "-f"] + (["-L", label] if label else []) + [dev]
        if fs == "btrfs":
            return ["mkfs.btrfs", "-f"] + (["-L", label] if label else []) + [dev]
        if fs == "ntfs":
            argv = ["mkntfs", "-F"] + (["-Q"] if quick else [])
            if cluster:
                argv += ["-c", str(cluster)]
            if label:
                argv += ["-L", label]
            return argv + [dev]
        if fs == "exfat":
            argv = ["mkfs.exfat"]
            if cluster:
                argv += ["-c", str(cluster)]
            if label:
                argv += ["-L", label]
            return argv + [dev]
        if fs in ("fat32", "fat16"):
            argv = ["mkfs.fat", "-F", "32" if fs == "fat32" else "16"]
            if cluster:
                argv += ["-s", str(max(1, int(cluster) // 512))]
            if label:
                argv += ["-n", label.upper()[:11]]
            return argv + [dev]
        if fs == "f2fs":
            return ["mkfs.f2fs", "-f"] + (["-l", label] if label else []) + [dev]
        if fs == "swap":
            return ["mkswap", "-f"] + (["-L", label] if label else []) + [dev]
        if fs == "hfsplus":
            return ["mkfs.hfsplus", "-J"] + (["-v", label] if label else []) + [dev]
        raise ValueError(f"no mkfs for {fs}")
    if act == "label":
        if fs in ("ext4", "ext3", "ext2"):
            return ["e2label", dev, label]
        if fs == "ntfs":
            return ["ntfslabel", "-f", dev, label]
        if fs == "exfat":
            return ["exfatlabel", dev, label]
        if fs in ("fat32", "fat16"):
            return ["fatlabel", dev, label.upper()[:11]]
        if fs == "xfs":
            return ["xfs_admin", "-L", label or "--", dev]
        if fs == "btrfs":
            return ["btrfs", "filesystem", "label", dev, label]
        if fs == "swap":
            return ["swaplabel", "-L", label, dev]
        if fs == "f2fs":
            return ["f2fslabel", dev, label]
        raise ValueError(f"no label tool for {fs}")
    if act == "fsck" or act == "fsck_before":
        repair = bool(a.get("repair")) or act == "fsck_before"
        if fs in ("ext4", "ext3", "ext2"):
            return ["e2fsck", "-f", "-y" if repair else "-n", dev]
        if fs == "ntfs":
            return ["ntfsfix", "-d", dev] if repair else ["ntfsfix", "-n", dev]
        if fs == "exfat":
            return ["fsck.exfat", "-y" if repair else "-n", dev]
        if fs in ("fat32", "fat16"):
            return ["fsck.fat", "-a" if repair else "-n", dev]
        if fs == "xfs":
            return ["xfs_repair", dev] if repair else ["xfs_repair", "-n", dev]
        if fs == "btrfs":
            return ["btrfs", "check", "--repair", "--force", dev] if repair else ["btrfs", "check", dev]
        if fs == "f2fs":
            return ["fsck.f2fs", "-f", dev] if repair else ["fsck.f2fs", dev]
        if fs == "hfsplus":
            return ["fsck.hfsplus", "-fy", dev] if repair else ["fsck.hfsplus", "-fn", dev]
        raise ValueError(f"no checker for {fs}")
    if act == "fsresize":
        size = a.get("size")
        if fs in ("ext4", "ext3", "ext2"):
            return ["resize2fs", "-p", dev] + ([f"{int(size) // 1024}K"] if size else [])
        if fs == "ntfs":
            return ["ntfsresize", "-f", "-f"] + (["-s", str(int(size))] if size else []) + [dev]
        if fs == "btrfs":
            return ["btrfs", "filesystem", "resize", str(int(size)) if size else "max", "<mountpoint>"]
        if fs == "xfs":
            return ["xfs_growfs", "<mountpoint>"]
        if fs == "swap":
            return ["mkswap", "-f", dev]
        raise ValueError(f"no resize tool for {fs}")
    if act == "wipefs":
        return ["wipefs", "-a", dev]
    raise ValueError(f"unknown tool action {act}")


def build_sfdisk(a: dict, disk_path: str) -> tuple[list[str], str | None]:
    """(argv, stdin_text) for an sfdisk step."""
    act = a.get("action")
    if act == "append":
        fields = [f"start={int(a['start']) // 512}", f"size={int(a['size']) // 512}"]
        if a.get("type"):
            fields.append(f"type={a['type']}")
        if a.get("name"):
            fields.append(f"name=\"{a['name']}\"")
        return ["sfdisk", "--append", "--no-reread", "-W", "always", disk_path], ", ".join(fields) + "\n"
    if act == "delete":
        return ["sfdisk", "--delete", disk_path, str(int(a["number"]))], None
    if act == "resize":
        return ["sfdisk", "--no-reread", "-N", str(int(a["number"])), disk_path], f", {int(a['size']) // 512}\n"
    if act == "type":
        return ["sfdisk", "--part-type", disk_path, str(int(a["number"])), str(a["type"])], None
    if act == "label":
        return ["sfdisk", "--wipe", "always", disk_path], f"label: {'gpt' if a.get('table') == 'gpt' else 'dos'}\n"
    raise ValueError(f"unknown sfdisk action {act}")


def build_diskutil(a: dict, disk_path: str) -> list[str]:
    """argv for a macOS diskutil step (absolute path; sizes in bytes with the B suffix)."""
    du = "/usr/sbin/diskutil"
    act = a.get("action")
    if act == "newTable":
        return [du, "partitionDisk", disk_path, "1", "GPT" if a.get("table") == "gpt" else "MBR", "Free Space", "%noformat%", "100%"]
    if act == "partitionDisk":
        return [du, "partitionDisk", disk_path, "2", "GPT" if a.get("table") == "gpt" else "MBR",
                str(a.get("format")), str(a.get("name")), f"{int(a['size'])}B", "Free Space", "%noformat%", "R"]
    if act == "addPartition":
        return [du, "addPartition", str(a["after"]), str(a.get("format")), str(a.get("name")), f"{int(a['size'])}B"]
    if act == "eraseVolume":
        return [du, "eraseVolume", str(a.get("format")), str(a.get("name")), str(a["device"])]
    if act == "rename":
        return [du, "rename", str(a["device"]), str(a.get("name"))]
    if act == "resizeVolume":
        return [du, "resizeVolume", str(a["device"]), f"{int(a['size'])}B"]
    if act == "resizeContainer":
        return [du, "apfs", "resizeContainer", str(a["device"]), f"{int(a['size'])}B"]
    if act in ("verifyVolume", "repairVolume"):
        return [du, act, str(a["device"])]
    if act in ("mount", "unmount"):
        return [du, act, str(a["device"])]
    raise ValueError(f"unknown diskutil action {act}")


def build_command(step: dict, platform: str) -> str:
    """Human-readable command preview for a step (also exactly what the helper runs)."""
    a = step["args"]
    k = step["kind"]
    if k == "ps":
        return build_ps(a)
    if k == "tool":
        return " ".join(shell_quote(x) for x in build_tool(a))
    if k == "sfdisk":
        argv, stdin = build_sfdisk(a, step["diskPath"])
        cmd = " ".join(shell_quote(x) for x in argv)
        return (f"echo {shell_quote(stdin.strip())} | " if stdin else "") + cmd
    if k == "diskutil":
        return " ".join(shell_quote(x) for x in build_diskutil(a, step["diskPath"]))
    if k == "unmount":
        return f"umount {step['args'].get('device')}" + (" (every mount on the disk)" if a.get("all") else "")
    if k == "raw_zero":
        return f"zero the first and last MiB of {step['diskPath']}"
    if k == "note":
        return a.get("text", "")
    return json_short(step)


def shell_quote(s) -> str:
    s = str(s)
    if re.fullmatch(r"[A-Za-z0-9_./=,:@+-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def json_short(o) -> str:
    import json
    return json.dumps(o)[:300]
