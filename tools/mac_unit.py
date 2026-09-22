"""Unit check of the macOS backend without a Mac.

    python tools/mac_unit.py

Feeds dw_mac.mac_inventory() recorded-shape `diskutil list -plist` / `diskutil info -plist`
dictionaries (tools/fixtures/mac/diskutil-sample.json: an Apple Silicon internal SSD with an
APFS container, a GPT USB stick with EFI + NTFS + HFS+ and free space at the end, an MBR NTFS
stick), refines the USB stick with a `gpt -r show` sample and the MBR stick with `fdisk -d`,
then runs the planner for darwin and checks the diskutil commands it emits.  Writes the
refined inventory to tools/fixtures/mac/sample-inventory.json for `diskworks.py --fixture`.
Exits 1 on the first failed check.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import dw_fs            # noqa: E402
import dw_inventory as di  # noqa: E402
import dw_mac           # noqa: E402
import dw_ops           # noqa: E402

FIX = os.path.join(HERE, "tools", "fixtures", "mac")
S = 512
failures = 0


def check(cond, what):
    global failures
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures += 1


# ----------------------------------------------------------------------------
# Fixture (the shape diskutil -plist produces; sizes from a SanDisk Ultra 115 GiB stick)
# ----------------------------------------------------------------------------
def build_fixture() -> dict:
    stick = 123042004992                       # bytes, 240316416 sectors
    efi_sz, win_sz, hfs_sz = 409600, 125829120, 62914560   # sectors
    efi_st = 40
    win_st = efi_st + efi_sz
    hfs_st = win_st + win_sz
    free_st = hfs_st + hfs_sz
    total = stick // S
    sec_table = total - 33
    free_sz = sec_table - free_st
    gpt_text = f"""       start        size  index  contents
           0           1         PMBR
           1           1         Pri GPT header
           2          32         Pri GPT table
          34           6
{efi_st:>12}{efi_sz:>12}      1  GPT part - C12A7328-F81F-11D2-BA4B-00A0C93EC93B
{win_st:>12}{win_sz:>12}      2  GPT part - EBD0A0A2-B9E5-4433-87C0-68B6B72699C7
{hfs_st:>12}{hfs_sz:>12}      3  GPT part - 48465300-0000-11AA-AA11-00306543ECAC
{free_st:>12}{free_sz:>12}
{sec_table:>12}          32         Sec GPT table
{total - 1:>12}           1         Sec GPT header
"""
    mbr_stick = 16008609792
    fdisk_text = f"{2048},{mbr_stick // S - 2048},0x07,-,0,32,33,1023,254,63\n0,0,0x00,-,0,0,0,0,0,0\n0,0,0x00,-,0,0,0,0,0,0\n0,0,0x00,-,0,0,0,0,0,0\n"
    lst = {"AllDisksAndPartitions": [
        {"DeviceIdentifier": "disk0", "Content": "GUID_partition_scheme", "Size": 1000555581440, "Partitions": [
            {"DeviceIdentifier": "disk0s1", "Content": "Apple_APFS_ISC", "Size": 524288000},
            {"DeviceIdentifier": "disk0s2", "Content": "Apple_APFS", "Size": 994662584320},
            {"DeviceIdentifier": "disk0s3", "Content": "Apple_APFS_Recovery", "Size": 5368664064}]},
        {"DeviceIdentifier": "disk3", "Size": 994662584320, "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}], "APFSVolumes": [
            {"DeviceIdentifier": "disk3s1", "VolumeName": "Macintosh HD", "MountPoint": "/", "CapacityInUse": 11934617600, "Roles": ["System"]},
            {"DeviceIdentifier": "disk3s2", "VolumeName": "Preboot", "MountPoint": "/System/Volumes/Preboot", "CapacityInUse": 6442450944, "Roles": ["Preboot"], "OSInternal": True},
            {"DeviceIdentifier": "disk3s3", "VolumeName": "Recovery", "MountPoint": "", "CapacityInUse": 1073741824, "Roles": ["Recovery"], "OSInternal": True},
            {"DeviceIdentifier": "disk3s5", "VolumeName": "Macintosh HD - Data", "MountPoint": "/System/Volumes/Data", "CapacityInUse": 322122547200, "Roles": ["Data"]},
            {"DeviceIdentifier": "disk3s6", "VolumeName": "VM", "MountPoint": "/System/Volumes/VM", "CapacityInUse": 20480, "Roles": ["VM"], "OSInternal": True}]},
        {"DeviceIdentifier": "disk4", "Content": "GUID_partition_scheme", "Size": stick, "Partitions": [
            {"DeviceIdentifier": "disk4s1", "Content": "EFI", "Size": efi_sz * S, "VolumeName": "EFI"},
            {"DeviceIdentifier": "disk4s2", "Content": "Microsoft Basic Data", "Size": win_sz * S, "VolumeName": "WINDATA"},
            {"DeviceIdentifier": "disk4s3", "Content": "Apple_HFS", "Size": hfs_sz * S, "VolumeName": "MacStuff"}]},
        {"DeviceIdentifier": "disk5", "Content": "FDisk_partition_scheme", "Size": mbr_stick, "Partitions": [
            {"DeviceIdentifier": "disk5s1", "Content": "Windows_NTFS", "Size": mbr_stick - 2048 * S, "VolumeName": "NTFSSTICK"}]},
    ]}
    info = {
        "disk0": {"Internal": True, "BusProtocol": "Apple Fabric", "SolidState": True, "DeviceBlockSize": 4096, "Size": 1000555581440,
                  "MediaName": "APPLE SSD AP1024Z Media", "VirtualOrPhysical": "Physical", "DiskUUID": "1A2B3C4D-0000-4000-8000-000000000001", "WritableMedia": True},
        "disk0s1": {"Content": "Apple_APFS_ISC", "Size": 524288000, "OSInternal": True},
        "disk0s2": {"Content": "Apple_APFS", "Size": 994662584320, "APFSContainerReference": "disk3"},
        "disk0s3": {"Content": "Apple_APFS_Recovery", "Size": 5368664064, "OSInternal": True},
        "disk4": {"Internal": False, "RemovableMediaOrExternalDevice": True, "Ejectable": True, "BusProtocol": "USB", "SolidState": True, "DeviceBlockSize": 512,
                  "Size": stick, "MediaName": "SanDisk Ultra USB 3.0 Media", "VirtualOrPhysical": "Physical", "DiskUUID": "1A2B3C4D-0000-4000-8000-000000000004", "WritableMedia": True},
        "disk4s1": {"Content": "EFI", "FilesystemType": "msdos", "FilesystemName": "MS-DOS FAT32", "VolumeName": "EFI", "Size": efi_sz * S},
        "disk4s2": {"Content": "Microsoft Basic Data", "FilesystemType": "ntfs", "FilesystemName": "NTFS", "VolumeName": "WINDATA", "MountPoint": "/Volumes/WINDATA",
                    "Writable": False, "Size": win_sz * S, "VolumeSize": win_sz * S, "FreeSpace": 60000000000, "VolumeUUID": "9B8A7C6D-1111-4222-8333-444455556666"},
        "disk4s3": {"Content": "Apple_HFS", "FilesystemType": "hfs", "FilesystemName": "Mac OS Extended (Journaled)", "VolumeName": "MacStuff", "MountPoint": "/Volumes/MacStuff",
                    "Writable": True, "Size": hfs_sz * S, "VolumeSize": hfs_sz * S, "FreeSpace": 30000000000},
        "disk5": {"Internal": False, "RemovableMediaOrExternalDevice": True, "Ejectable": True, "BusProtocol": "USB", "SolidState": True, "DeviceBlockSize": 512,
                  "Size": mbr_stick, "MediaName": "Kingston DataTraveler 3.0 Media", "VirtualOrPhysical": "Physical", "WritableMedia": True},
        "disk5s1": {"Content": "Windows_NTFS", "FilesystemType": "ntfs", "FilesystemName": "NTFS", "VolumeName": "NTFSSTICK", "MountPoint": "/Volumes/NTFSSTICK",
                    "Writable": False, "Size": mbr_stick - 2048 * S, "VolumeSize": mbr_stick - 2048 * S, "FreeSpace": 15000000000},
    }
    root = {"DeviceIdentifier": "disk3s1", "MountPoint": "/", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}], "ParentWholeDisk": "disk3"}
    return {"list": lst, "info": info, "root": root, "gpt": {"disk4": gpt_text}, "fdisk": {"disk5": fdisk_text},
            "expect": {"stick": stick, "hfs_start": hfs_st * S, "free_start": free_st * S, "free_size": free_sz * S}}


def main() -> int:
    os.makedirs(FIX, exist_ok=True)
    fx = build_fixture()
    with open(os.path.join(FIX, "diskutil-sample.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({k: fx[k] for k in ("list", "info", "root", "gpt", "fdisk")}, f, indent=1)

    print("inventory from recorded plists")
    inv = dw_mac.mac_inventory(info_fn=lambda ident: fx["info"].get(ident, {}), list_fn=lambda: fx["list"], root_fn=lambda: fx["root"])
    names = [d["name"] for d in inv["disks"]]
    check(names == ["disk0", "disk4", "disk5"], f"physical disks only, synthesized disk3 folded in: {names}")
    d0, d4, d5 = inv["disks"]
    check(d0["system"] and d0["boot"], "disk0 is the system disk (root's physical store)")
    apfs = d0["partitions"][1]
    check(apfs["fs"] == "apfs" and apfs.get("apfsContainer") == "disk3", "disk0s2 is the APFS container disk3")
    check([v["name"] for v in apfs["apfsVolumes"] if not v["internal"]] == ["Macintosh HD", "Macintosh HD - Data"], "container volumes listed (internal ones marked)")
    check(apfs["isBootVolume"] and apfs["mountpoints"][0] == "/", "container holds /")
    check(apfs["used"] == 11934617600 + 6442450944 + 1073741824 + 322122547200 + 20480, "container used = sum of volumes")
    check(any("system" in (r or "").lower() or "boot" in (r or "").lower() for r in (apfs.get("locked") or ["system"])), "system partition locked: " + "; ".join(apfs.get("locked") or []))
    check(d4["removable"] and d4["bus"] == "USB" and not d4["system"], "disk4 is a removable USB disk")
    p1, p2, p3 = d4["partitions"]
    check(p1["fs"] == "fat32" and p1["flags"]["esp"] and p1["typeGuid"] == dw_mac.CONTENT_GUIDS["EFI"].lower(), "disk4s1 EFI / FAT32")
    check(p2["fs"] == "ntfs" and p2["mountpoints"] == ["/Volumes/WINDATA"] and p2["flags"]["readonly"], "disk4s2 NTFS mounted read-only")
    check(p2["used"] == p2["volSize"] - 60000000000, "NTFS used space from VolumeSize - FreeSpace")
    check(p3["fs"] == "hfsplus" and p3["typeName"].startswith("Apple"), f"disk4s3 HFS+ ({p3['typeName']})")
    check(d4["approx"] is True and all(p["approx"] for p in d4["partitions"]), "starts marked approximate before refinement")
    check(d5["table"] == "mbr" and d5["partitions"][0]["mbrType"] == 0x07 and d5["partitions"][0]["fs"] == "ntfs", "disk5 MBR with an NTFS primary")

    print("refinement with gpt -r show / fdisk -d")
    dw_mac.refine_disk_with_gpt(d4, fx["gpt"]["disk4"])
    d4["partitions"].sort(key=lambda p: p["start"])
    di.compute_gaps(d4)
    check(d4["approx"] is False and p1["start"] == 40 * S, f"disk4s1 starts at sector 40 ({p1['start']})")
    check(p3["start"] == fx["expect"]["hfs_start"], "disk4s3 start exact")
    gaps = d4.get("gaps") or []
    check(len(gaps) == 1, f"one free region at the end ({len(gaps)} found)")
    if gaps:
        g = gaps[0]
        check(abs(g["start"] - fx["expect"]["free_start"]) < 2 * 1024 * 1024 and g["size"] > 20 * 2**30, f"free region ~{g['size'] / 2**30:.1f} GiB after the HFS+ partition")
    dw_mac.refine_disk_with_fdisk(d5, fx["fdisk"]["disk5"])
    di.compute_gaps(d5)
    check(d5["partitions"][0]["start"] == 2048 * S and d5["approx"] is False, "disk5s1 starts at sector 2048")
    di.apply_protection(d0); di.apply_protection(d4); di.apply_protection(d5)
    inv["hash"] = di.layout_hash(inv["disks"])
    inv["approx"] = False
    inv["platform"] = "darwin"
    with open(os.path.join(FIX, "sample-inventory.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(inv, f, indent=1)
    print("  wrote tools/fixtures/mac/sample-inventory.json")

    print("planner for darwin")
    def plan(ops):
        return dw_ops.plan(json.loads(json.dumps(inv)), ops, "darwin")

    def cmds(res):
        return [s["cmd"] for s in res["steps"]]

    def has(res, *tokens):
        """One planned step is a diskutil command carrying every token (arguments may be quoted, devices are /dev/… paths)."""
        return not res["errors"] and any("diskutil" in c and all(t in c for t in tokens) for c in cmds(res))

    def show(res):
        return (" | ".join(cmds(res)) or (res["errors"][0]["message"] if res["errors"] else "no steps"))

    r = plan([{"op": "create", "gap": gaps[0]["id"], "fs": "exfat", "label": "NEW"}])
    check(has(r, "addPartition", "disk4s3", "ExFAT", "NEW", "B"), "create in the tail gap -> addPartition after disk4s3 with an explicit size: " + show(r))
    r = plan([{"op": "create", "gap": gaps[0]["id"], "fs": "ntfs", "label": "NO"}])
    check(bool(r["errors"]), "NTFS cannot be created on macOS: " + show(r))
    r = plan([{"op": "label", "part": p3["id"], "label": "Stuff"}])
    check(has(r, "rename", "disk4s3", "Stuff"), "label -> diskutil rename: " + show(r))
    r = plan([{"op": "delete", "part": p2["id"]}])
    check(has(r, "eraseVolume", "Free Space", "%noformat%", "disk4s2"), "delete -> eraseVolume Free Space: " + show(r))
    r = plan([{"op": "format", "part": d5["partitions"][0]["id"], "fs": "fat32", "label": "FATSTICK"}])
    check(has(r, "eraseVolume", "MS-DOS FAT32", "FATSTICK", "disk5s1"), "format -> eraseVolume MS-DOS FAT32: " + show(r))
    r = plan([{"op": "resize", "part": p3["id"], "size": p3["size"] - 2 * 2**30, "minSize": 2**30}])
    check(has(r, "resizeVolume", "disk4s3", "B"), "HFS+ shrink -> resizeVolume with an explicit size: " + show(r))
    r = plan([{"op": "resize", "part": p3["id"], "size": p3["size"] + 4 * 2**30, "minSize": 2**30}])
    check(has(r, "resizeVolume", "disk4s3"), "HFS+ grow into the free space -> resizeVolume: " + show(r))
    r = plan([{"op": "check", "part": p3["id"], "repair": True}])
    check(has(r, "repairVolume", "disk4s3"), "check+repair -> repairVolume: " + show(r))
    r = plan([{"op": "table", "disk": d5["id"], "table": "gpt"}])
    check(has(r, "partitionDisk", "disk5", "GPT", "Free Space", "%noformat%"), "new table -> partitionDisk GPT with an empty map: " + show(r))
    r = plan([{"op": "delete", "part": apfs["id"]}])
    check(bool(r["errors"]), "deleting the system container is refused: " + show(r))
    r = plan([{"op": "wipe", "disk": d4["id"], "zero": True}])
    check(has(r, "partitionDisk", "disk4") and any("zero" in c for c in cmds(r)), "wipe -> empty map via partitionDisk + zeroing: " + show(r))

    print("filesystem matrix for macOS")
    check(dw_fs.can("apfs", "create", "darwin") and dw_fs.can("hfsplus", "create", "darwin") and dw_fs.can("exfat", "create", "darwin"), "APFS / HFS+ / exFAT creatable")
    check(not dw_fs.can("ext4", "create", "darwin") and not dw_fs.can("ntfs", "create", "darwin"), "ext4 / NTFS not creatable")
    check([f for f in dw_fs.FORMAT_CHOICES["darwin"]] and "ntfs" not in dw_fs.FORMAT_CHOICES["darwin"], f"format choices: {dw_fs.FORMAT_CHOICES['darwin']}")

    print("build argv for macOS")
    import importlib.util
    spec = importlib.util.spec_from_file_location("build", os.path.join(HERE, "build.py"))
    build = importlib.util.module_from_spec(spec)
    saved = sys.platform
    try:
        sys.platform = "darwin"
        spec.loader.exec_module(build)
        argv = build.pyinstaller_cmd(False, True, [])
        check("--windowed" in argv and "--osx-bundle-identifier" in argv and "--collect-all" in argv, "PyInstaller argv has --windowed, bundle id, --collect-all webview")
        check("webview.platforms.cocoa" in argv and "PySide6" in argv, "cocoa hidden import, Qt excluded")
    finally:
        sys.platform = saved

    print("\nRESULT:", "all checks passed" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
