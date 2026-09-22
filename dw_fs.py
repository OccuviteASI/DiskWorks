"""Filesystem and partition-type knowledge shared by every DiskWorks module.

- GPT type GUIDs and MBR ids with their plain names
- the capability matrix (what each filesystem allows on each OS; ARCHITECTURE.md §7)
- signature identification of a partition from its first sectors (for partitions
  the OS itself calls RAW / Unknown)
- the tool names each operation uses, so the helper and the planner agree
"""
from __future__ import annotations

import struct

# ----------------------------------------------------------------------------
# Partition types
# ----------------------------------------------------------------------------
GPT_TYPES: dict[str, str] = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft Reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows Recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "Windows LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "Windows LDM data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Storage Spaces",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "44479540-f297-41b2-9af7-d131d5f0458a": "Linux root (x86)",
    "b921b045-1df0-41c3-af44-4c6f280d3fae": "Linux root (ARM64)",
    "69dad710-2ce4-4e3c-b16c-21a1d49abed3": "Linux root (ARM)",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux /home",
    "4d21b016-b534-45c2-a9fb-5c16e091fd2d": "Linux /var",
    "3b8f8425-20e0-4f3b-907f-1a25a76f98e8": "Linux /srv",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "8da63339-0007-60c0-c436-083ac8230908": "Linux reserved",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "Linux extended boot",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "69646961-6700-11aa-aa11-00306543ecac": "Apple APFS (iSC preboot)",
    "52637672-7900-11aa-aa11-00306543ecac": "Apple APFS recovery",
    "426f6f74-0000-11aa-aa11-00306543ecac": "Apple boot",
    "53746f72-6167-11aa-aa11-00306543ecac": "Apple Core Storage",
    "516e7cb4-6ecf-11d6-8ff8-00022d09712b": "FreeBSD data",
    "6a898cc3-1dd2-11b2-99a6-080020736631": "Solaris /usr or ZFS",
    "fe3a2a5d-4f32-41a7-b725-accc3285a309": "ChromeOS kernel",
    "3cb8e202-3b7e-47dd-8a3c-7ff2a13cfcec": "ChromeOS root",
    "00000000-0000-0000-0000-000000000000": "Unused",
}
MBR_TYPES: dict[int, str] = {
    0x00: "Empty", 0x01: "FAT12", 0x04: "FAT16 (<32 MB)", 0x05: "Extended", 0x06: "FAT16",
    0x07: "NTFS / exFAT (IFS)", 0x0b: "FAT32", 0x0c: "FAT32 (LBA)", 0x0e: "FAT16 (LBA)",
    0x0f: "Extended (LBA)", 0x11: "Hidden FAT12", 0x12: "Recovery", 0x14: "Hidden FAT16",
    0x16: "Hidden FAT16", 0x17: "Hidden NTFS", 0x1b: "Hidden FAT32", 0x1c: "Hidden FAT32 (LBA)",
    0x27: "Windows Recovery", 0x42: "Windows LDM", 0x82: "Linux swap", 0x83: "Linux",
    0x85: "Linux extended", 0x8e: "Linux LVM", 0xa5: "FreeBSD", 0xa6: "OpenBSD",
    0xaf: "Apple HFS+", 0xee: "GPT protective", 0xef: "EFI System (MBR)", 0xfd: "Linux RAID",
}
# GPT types that DiskWorks never lets the user change (ARCHITECTURE.md §4)
PROTECTED_GPT = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System partition (the computer boots from it)",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft Reserved partition (Windows needs it)",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows Recovery partition",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "Dynamic disk (LDM) metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "Dynamic disk (LDM) data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Storage Spaces member",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot partition",
}
PROTECTED_MBR = {0x27: "Windows Recovery partition", 0x42: "Dynamic disk (LDM)", 0xee: "GPT protective entry"}


def gpt_type_name(guid: str | None) -> str:
    if not guid:
        return ""
    return GPT_TYPES.get(guid.lower().strip("{}"), "Unknown type")


def mbr_type_name(code: int | None) -> str:
    if code is None:
        return ""
    return MBR_TYPES.get(int(code), f"Type 0x{int(code):02x}")


# ----------------------------------------------------------------------------
# Capability matrix (ARCHITECTURE.md §7).  Values: "on" (works mounted), "off"
# (must be unmounted / dismounted), None (not possible).  Per platform.
# ----------------------------------------------------------------------------
FS: dict[str, dict] = {
    # name: canonical lower-case id; label: what the UI shows
    "ntfs": {"label": "NTFS", "family": "windows",
             "win": {"create": "on", "grow": "on", "shrink": "on", "check": "off", "label": "on", "uuid": None},
             "linux": {"create": "off", "grow": "off", "shrink": "off", "check": "off", "label": "off", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 10 * 2**20},
    "exfat": {"label": "exFAT", "family": "windows",
              "win": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "linux": {"create": "off", "grow": None, "shrink": None, "check": "off", "label": "off", "uuid": "off"},
             "mac": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "min": 8 * 2**20},
    "fat32": {"label": "FAT32", "family": "windows",
              "win": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "linux": {"create": "off", "grow": "off", "shrink": "off", "check": "off", "label": "off", "uuid": "off"},
             "mac": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "min": 33 * 2**20, "resizeMin": 256 * 2**20},
    "fat16": {"label": "FAT16", "family": "windows",
              "win": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "linux": {"create": "off", "grow": "off", "shrink": "off", "check": "off", "label": "off", "uuid": "off"},
             "mac": {"create": "on", "grow": None, "shrink": None, "check": "off", "label": "on", "uuid": None},
              "min": 4 * 2**20, "max": 4 * 2**30},
    "refs": {"label": "ReFS", "family": "windows",
             "win": {"create": "probe", "grow": "on", "shrink": None, "check": "on", "label": "on", "uuid": None},
             "linux": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 50 * 2**30},
    "ext4": {"label": "ext4", "family": "linux",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": "off", "grow": "on", "shrink": "off", "check": "off", "label": "on", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 16 * 2**20},
    "ext3": {"label": "ext3", "family": "linux",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": "off", "grow": "on", "shrink": "off", "check": "off", "label": "on", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 16 * 2**20},
    "ext2": {"label": "ext2", "family": "linux",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": "off", "grow": "off", "shrink": "off", "check": "off", "label": "on", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 8 * 2**20},
    "xfs": {"label": "xfs", "family": "linux",
            "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
            "linux": {"create": "off", "grow": "on", "shrink": None, "check": "off", "label": "off", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
            "min": 300 * 2**20},
    "btrfs": {"label": "btrfs", "family": "linux",
              "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
              "linux": {"create": "off", "grow": "on", "shrink": "on", "check": "off", "label": "on", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
              "min": 256 * 2**20},
    "f2fs": {"label": "f2fs", "family": "linux",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": "off", "grow": "off", "shrink": None, "check": "off", "label": "off", "uuid": None},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 50 * 2**20},
    "swap": {"label": "Linux swap", "family": "linux",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": "off", "grow": "recreate", "shrink": "recreate", "check": None, "label": "off", "uuid": "off"},
             "mac": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "min": 1 * 2**20},
    "luks": {"label": "LUKS (encrypted)", "family": "other", "win": {}, "linux": {}, "detect_only": True},
    "lvm": {"label": "LVM member", "family": "other", "win": {}, "linux": {}, "detect_only": True},
    "raid": {"label": "RAID member", "family": "other", "win": {}, "linux": {}, "detect_only": True},
    "iso9660": {"label": "ISO 9660 / UDF", "family": "other", "win": {}, "linux": {}, "detect_only": True},
    "hfsplus": {"label": "HFS+ (Mac OS Extended)", "family": "mac",
                "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
                "linux": {"create": "off", "grow": None, "shrink": None, "check": "off", "label": None, "uuid": None},
                "mac": {"create": "on", "grow": "on", "shrink": "on", "check": "off", "label": "on", "uuid": None},
                "min": 32 * 2**20},
    "apfs": {"label": "APFS", "family": "mac",
             "win": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "linux": {"create": None, "grow": None, "shrink": None, "check": None, "label": None, "uuid": None},
             "mac": {"create": "on", "grow": "on", "shrink": "on", "check": "off", "label": "on", "uuid": None},
             "min": 64 * 2**20},
    "bitlocker": {"label": "BitLocker (encrypted)", "family": "other", "win": {}, "linux": {}, "detect_only": True},
}

# Filesystems the Format dialog offers, in display order, per platform.
FORMAT_CHOICES = {
    "win32": ["ntfs", "exfat", "fat32", "refs"],
    "linux": ["ext4", "ntfs", "exfat", "fat32", "xfs", "btrfs", "f2fs", "ext3", "ext2", "swap", "fat16", "hfsplus"],
    "darwin": ["apfs", "hfsplus", "exfat", "fat32"],
}

# Bundled / system tool per filesystem and operation on Linux (argv templates are
# built in dw_linux; this table only names the executable so the bundle checker and
# the planner agree).
LINUX_TOOLS = {
    "ntfs": {"create": "mkntfs", "resize": "ntfsresize", "check": "ntfsfix", "label": "ntfslabel", "info": "ntfsresize"},
    "exfat": {"create": "mkfs.exfat", "check": "fsck.exfat", "label": "exfatlabel", "uuid": "tune.exfat"},
    "fat32": {"create": "mkfs.fat", "resize": "fatresize", "check": "fsck.fat", "label": "fatlabel"},
    "fat16": {"create": "mkfs.fat", "resize": "fatresize", "check": "fsck.fat", "label": "fatlabel"},
    "ext4": {"create": "mkfs.ext4", "resize": "resize2fs", "check": "e2fsck", "label": "e2label", "uuid": "tune2fs", "info": "resize2fs"},
    "ext3": {"create": "mkfs.ext3", "resize": "resize2fs", "check": "e2fsck", "label": "e2label", "uuid": "tune2fs", "info": "resize2fs"},
    "ext2": {"create": "mkfs.ext2", "resize": "resize2fs", "check": "e2fsck", "label": "e2label", "uuid": "tune2fs", "info": "resize2fs"},
    "xfs": {"create": "mkfs.xfs", "resize": "xfs_growfs", "check": "xfs_repair", "label": "xfs_admin", "uuid": "xfs_admin"},
    "btrfs": {"create": "mkfs.btrfs", "resize": "btrfs", "check": "btrfs", "label": "btrfs", "uuid": "btrfstune", "info": "btrfs"},
    "f2fs": {"create": "mkfs.f2fs", "resize": "resize.f2fs", "check": "fsck.f2fs", "label": "f2fslabel"},
    "swap": {"create": "mkswap", "label": "swaplabel", "uuid": "swaplabel"},
    "hfsplus": {"create": "mkfs.hfsplus", "check": "fsck.hfsplus"},
}
LINUX_TABLE_TOOLS = ["sfdisk", "sgdisk", "blockdev", "blkid", "findmnt", "wipefs", "mkswap", "swaplabel"]


def normalize_fs(name: str | None) -> str | None:
    """Map what the OS reports to the canonical ids above ('NTFS' -> 'ntfs', 'vfat' -> 'fat32', …)."""
    if not name:
        return None
    n = str(name).strip().lower()
    aliases = {
        "vfat": "fat32", "fat": "fat32", "msdos": "fat32", "fat12": "fat16",
        "crypto_luks": "luks", "lvm2_member": "lvm", "linux_raid_member": "raid",
        "udf": "iso9660", "hfs+": "hfsplus", "hfs": "hfsplus", "cdfs": "iso9660",
        "refs": "refs", "ntfs3": "ntfs", "ntfs-3g": "ntfs", "fuseblk": "ntfs", "hfsx": "hfsplus", "jhfs+": "hfsplus",
        "ms-dos": "fat32", "ms-dos fat32": "fat32", "ms-dos fat16": "fat16", "apfsx": "apfs",
        "raw": None, "unknown": None, "": None,
    }
    if n in aliases:
        return aliases[n]
    return n if n in FS else n


def fs_label(fs_id: str | None) -> str:
    if not fs_id:
        return ""
    info = FS.get(fs_id)
    return info["label"] if info else fs_id


def can(fs_id: str | None, op: str, platform: str) -> str | None:
    """'on' | 'off' | 'probe' | 'recreate' | None for filesystem fs_id, operation op, on platform."""
    info = FS.get(fs_id or "")
    if not info or info.get("detect_only"):
        return None
    key = "win" if platform == "win32" else ("mac" if platform == "darwin" else "linux")
    return info.get(key, {}).get(op)


# ----------------------------------------------------------------------------
# Signature identification.  `read(offset, length)` returns bytes from the start
# of the partition (or disk).  Returns (fs_id, detail) or (None, "").
# ----------------------------------------------------------------------------
def identify(read) -> tuple[str | None, str]:
    try:
        b0 = read(0, 512)
    except Exception:
        return None, ""
    if len(b0) < 512:
        return None, ""
    oem = b0[3:11]
    if oem == b"-FVE-FS-":
        return "bitlocker", "BitLocker encrypted volume"
    if oem == b"NTFS    ":
        return "ntfs", ""
    if oem == b"EXFAT   ":
        return "exfat", ""
    if b0[0:4] == b"XFSB":
        return "xfs", ""
    if b0[0:6] == b"LUKS\xba\xbe":
        ver = struct.unpack(">H", b0[6:8])[0]
        return "luks", f"LUKS{ver}"
    if b0[0:4] == b"ReFS" or oem[:4] == b"ReFS":
        return "refs", ""
    if b0[510:512] == b"\x55\xaa" and (b0[54:59] == b"FAT16" or b0[54:59] == b"FAT12"):
        return "fat16", b0[54:62].decode("ascii", "replace").strip()
    if b0[510:512] == b"\x55\xaa" and b0[82:87] == b"FAT32":
        return "fat32", ""
    if b0[510:512] == b"\x55\xaa" and b0[0] in (0xEB, 0xE9) and b0[11:13] in (b"\x00\x02", b"\x00\x04", b"\x00\x08", b"\x00\x10"):
        # FAT boot sector without the type string: decide by sectors-per-FAT fields
        if struct.unpack("<H", b0[22:24])[0] == 0:
            return "fat32", ""
        return "fat16", ""
    try:
        b1 = read(1024, 1024)
        if len(b1) >= 1024:
            if b1[56:58] == b"\x53\xef":  # ext superblock magic 0xEF53
                feat_compat = struct.unpack("<I", b1[92:96])[0]
                feat_incompat = struct.unpack("<I", b1[96:100])[0]
                if feat_incompat & 0x2C0 or feat_incompat & 0x40:   # extents / 64bit / flex_bg
                    return "ext4", ""
                if feat_compat & 0x4:  # has_journal
                    return "ext3", ""
                return "ext2", ""
            if b1[0:4] == b"\x10\x20\xf5\xf2":
                return "f2fs", ""
            if b1[0:2] == b"H+" or b1[0:2] == b"HX":
                return "hfsplus", ""
        b2 = read(65536, 512)
        if len(b2) >= 72 and b2[64:72] == b"_BHRfS_M":
            return "btrfs", ""
        if b0[32:36] == b"NXSB":
            return "apfs", ""
        b3 = read(32768, 512)
        if len(b3) >= 6 and b3[1:6] == b"CD001":
            return "iso9660", ""
        b4 = read(4096 - 10, 10)
        if b4 == b"SWAPSPACE2" or b4 == b"SWAP-SPACE":
            return "swap", ""
        b5 = read(4096, 8)
        if b5 == b"\xfc\x4e\x2b\xa9\x00\x00\x00\x00" or b5[:4] == b"\xfc\x4e\x2b\xa9":
            return "raid", "mdraid"
        b6 = read(512, 8)
        if b6 == b"LABELONE":
            return "lvm", "LVM2"
    except Exception:
        pass
    return None, ""


def identify_table(b0: bytes, b1: bytes | None = None) -> str:
    """'gpt' | 'mbr' | 'none' from sector 0 (and sector 1 for the GPT header)."""
    if b1 and b1[0:8] == b"EFI PART":
        return "gpt"
    if len(b0) >= 512 and b0[510:512] == b"\x55\xaa":
        for i in range(4):
            e = b0[446 + i * 16: 446 + (i + 1) * 16]
            if e[4] == 0xEE:
                return "gpt"
        if any(b0[446 + i * 16 + 4] for i in range(4)):
            return "mbr"
    return "none"


def is_hybrid_image(b0: bytes) -> bool:
    """A 'hybrid' ISO / raw image carries a usable MBR in its first sector (bootable from USB)."""
    if len(b0) < 512 or b0[510:512] != b"\x55\xaa":
        return False
    for i in range(4):
        e = b0[446 + i * 16: 446 + (i + 1) * 16]
        ptype = e[4]
        nsect = struct.unpack("<I", e[12:16])[0]
        if ptype and nsect:
            return True
    return False
