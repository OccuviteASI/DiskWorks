"""Collect the third-party binaries DiskWorks bundles, pinned, into bin/<tag>/.

    python fetch-helpers.py                 everything for this platform
    python fetch-helpers.py linux-tools     (inside WSL / Linux) copy the partition and
                                            filesystem tools + licences from the build host
    python fetch-helpers.py win64           Windows downloads (none needed yet)

Nothing here runs at run time; build.py refuses to build without the pieces listed in
REQUIRED for the platform.  Downloads are SHA-256 pinned; a pin of "REPLACE" prints the
hash of what was fetched so it can be recorded.

Linux tools are not downloaded: they are copied from the build host (WSL Ubuntu) after
`apt-get install` of the packages named in LINUX_PACKAGES, together with each package's
copyright file, and PyInstaller collects their shared libraries at build time
(ARCHITECTURE.md §10, decision D-020).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
TAG = "win64" if IS_WIN else "linux-x86_64"

# Windows downloads (pinned).  7-Zip drives the read-only browser (APFS, HFS+, ext, NTFS, FAT);
# smartmontools' smartctl reads drive health (the Disks tab, 0.3.0).  ISO mode (wimlib,
# UEFI:NTFS) and the driver offers (WinBtrfs) arrive with later builds.
SEVENZIP_VERSION = "2501"
SMARTMONTOOLS_VERSION = "7.5"
DOWNLOADS: dict[str, dict] = {
    "7zr": {"url": "https://www.7-zip.org/a/7zr.exe", "sha256": "ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d",
            "kind": "bin", "dest": "bin/win64/7zip", "files": ["7zr.exe"]},
    "7zip": {"url": f"https://www.7-zip.org/a/7z{SEVENZIP_VERSION}-x64.exe", "sha256": "78afa2a1c773caf3cf7edf62f857d2a8a5da55fb0fff5da416074c0d28b2b55f",
             "kind": "7zsfx", "dest": "bin/win64/7zip", "files": ["7z.exe", "7z.dll", "License.txt"]},
    # the NSIS installer is unpacked with the full 7z.exe fetched above; only the x64 smartctl and the GPL text are kept
    "smartmontools": {"url": f"https://sourceforge.net/projects/smartmontools/files/smartmontools/{SMARTMONTOOLS_VERSION}/smartmontools-{SMARTMONTOOLS_VERSION}.win32-setup.exe/download",
                      "sha256": "896337fcc253220614cf8cdbd5cf2321c5aa326a37a04160a672a281e6104c70",
                      "kind": "nsis", "dest": "bin/win64/smartmontools", "files": ["smartctl.exe", "COPYING.txt"],
                      "members": {"bin/smartctl.exe": "smartctl.exe", "doc/COPYING.txt": "COPYING.txt"}, "filename": "smartmontools-setup.exe"},
    # "wimlib": {"url": "https://wimlib.net/downloads/wimlib-1.14.5-windows-x86_64-bin.zip", "sha256": "REPLACE",
    #            "kind": "zip", "dest": "bin/win64/wimlib", "files": ["wimlib-imagex.exe", "libwim-15.dll"]},
}

# Linux: package -> the executables DiskWorks calls (dw_fs.LINUX_TOOLS / LINUX_TABLE_TOOLS)
LINUX_PACKAGES: dict[str, list[str]] = {
    "util-linux": ["sfdisk", "wipefs", "blkid", "blockdev", "mkswap", "swaplabel", "losetup", "partx"],
    "gdisk": ["sgdisk"],
    "e2fsprogs": ["mkfs.ext4", "mkfs.ext3", "mkfs.ext2", "mke2fs", "resize2fs", "e2fsck", "e2label", "tune2fs", "dumpe2fs", "e2image"],
    "xfsprogs": ["mkfs.xfs", "xfs_growfs", "xfs_repair", "xfs_admin", "xfs_db", "xfs_io"],
    "btrfs-progs": ["mkfs.btrfs", "btrfs", "btrfstune"],
    "ntfs-3g": ["mkntfs", "mkfs.ntfs", "ntfsresize", "ntfsfix", "ntfslabel", "ntfsclone", "ntfsinfo", "ntfs-3g", "mount.ntfs-3g", "lowntfs-3g"],
    "dosfstools": ["mkfs.fat", "mkfs.vfat", "fsck.fat", "fatlabel"],
    "exfatprogs": ["mkfs.exfat", "fsck.exfat", "exfatlabel", "tune.exfat", "dump.exfat"],
    "f2fs-tools": ["mkfs.f2fs", "fsck.f2fs", "resize.f2fs", "f2fslabel", "dump.f2fs"],
    "fatresize": ["fatresize"],
    "cryptsetup-bin": ["cryptsetup"],
    "7zip": ["7zz", "7z"],
    "hfsprogs": ["mkfs.hfsplus", "fsck.hfsplus"],
    "smartmontools": ["smartctl"],
}
OPTIONAL_PACKAGES = {"fatresize", "f2fs-tools", "cryptsetup-bin", "hfsprogs", "smartmontools"}

REQUIRED = {
    "win64": ["7zip/7z.exe", "7zip/7z.dll"],
    "linux-x86_64": ["tools/sfdisk", "tools/sgdisk", "tools/mkfs.ext4", "tools/resize2fs", "tools/e2fsck", "tools/mkfs.xfs",
                     "tools/mkfs.btrfs", "tools/mkntfs", "tools/ntfsresize", "tools/mkfs.fat", "tools/mkfs.exfat", "tools/7zz", "LICENSES/util-linux.txt"],
}


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
def download(url: str, dest: str) -> None:
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                print(f"\r  {100 * got // total:3d}%", end="", flush=True)
        print()


def fetch_downloads(tag: str) -> None:
    for name, spec in DOWNLOADS.items():
        dest_dir = os.path.join(HERE, spec["dest"])
        os.makedirs(dest_dir, exist_ok=True)
        if all(os.path.isfile(os.path.join(dest_dir, f)) for f in spec["files"]):
            print(f"{name}: already present")
            continue
        tmp = os.path.join(dest_dir, spec.get("filename") or os.path.basename(spec["url"]))
        download(spec["url"], tmp)
        got = sha256_of(tmp)
        if spec["sha256"] == "REPLACE":
            print(f"{name}: sha256 = {got}  <- record this pin")
        elif got != spec["sha256"]:
            os.remove(tmp)
            sys.exit(f"{name}: SHA-256 mismatch\n  expected {spec['sha256']}\n  got      {got}")
        if spec["kind"] == "bin":
            os.replace(tmp, os.path.join(dest_dir, spec["files"][0]))
            print(f"{name}: ok")
            continue
        if spec["kind"] == "7zsfx":
            zr = os.path.join(dest_dir, "7zr.exe")
            if not os.path.isfile(zr):
                sys.exit(f"{name}: 7zr.exe is needed to unpack the installer; fetch '7zr' first")
            r = subprocess.run([zr, "x", "-y", f"-o{dest_dir}", tmp] + spec["files"], capture_output=True, text=True)
            os.remove(tmp)
            if r.returncode != 0 or not all(os.path.isfile(os.path.join(dest_dir, f)) for f in spec["files"]):
                sys.exit(f"{name}: unpacking failed: {(r.stdout + r.stderr).strip()[-400:]}")
            print(f"{name}: ok ({', '.join(spec['files'])})")
            continue
        if spec["kind"] == "nsis":
            z = os.path.join(HERE, "bin", "win64", "7zip", "7z.exe")
            if not os.path.isfile(z):
                sys.exit(f"{name}: 7z.exe is needed to unpack the installer; fetch '7zip' first")
            members = spec["members"]
            r = subprocess.run([z, "e", "-y", f"-o{dest_dir}", tmp] + list(members), capture_output=True, text=True)
            os.remove(tmp)
            # 7z e flattens the paths; rename what we kept to the names DiskWorks looks for
            for src, want in members.items():
                flat = os.path.join(dest_dir, os.path.basename(src))
                if os.path.isfile(flat) and flat != os.path.join(dest_dir, want):
                    os.replace(flat, os.path.join(dest_dir, want))
            if r.returncode != 0 or not all(os.path.isfile(os.path.join(dest_dir, f)) for f in spec["files"]):
                sys.exit(f"{name}: unpacking failed: {(r.stdout + r.stderr).strip()[-400:]}")
            print(f"{name}: ok ({', '.join(spec['files'])})")
            continue
        if spec["kind"] == "zip":
            import zipfile
            with zipfile.ZipFile(tmp) as z:
                names = {os.path.basename(n).lower(): n for n in z.namelist() if not n.endswith("/")}
                for want in spec["files"]:
                    src = names.get(want.lower())
                    if not src:
                        sys.exit(f"{name}: {want} not in the archive ({', '.join(sorted(names))})")
                    with z.open(src) as s, open(os.path.join(dest_dir, want), "wb") as d:
                        shutil.copyfileobj(s, d)
            os.remove(tmp)
        print(f"{name}: ok")


# ----------------------------------------------------------------------------
def linux_tools() -> None:
    if IS_WIN:
        sys.exit("linux-tools must run inside Linux (WSL): python3 fetch-helpers.py linux-tools")
    dest = os.path.join(HERE, "bin", "linux-x86_64", "tools")
    lic = os.path.join(HERE, "bin", "linux-x86_64", "LICENSES")
    os.makedirs(dest, exist_ok=True)
    os.makedirs(lic, exist_ok=True)
    missing_pkgs = []
    manifest: dict = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "host": open("/etc/os-release").read().split("\n")[0] if os.path.exists("/etc/os-release") else "",
                      "packages": {}, "tools": {}}
    for pkg, tools in LINUX_PACKAGES.items():
        ver = pkg_version(pkg)
        found = 0
        for t in tools:
            src = shutil.which(t, path="/usr/sbin:/usr/bin:/sbin:/bin")
            if not src:
                continue
            real = os.path.realpath(src)
            target = os.path.join(dest, t)
            if os.path.lexists(target):
                os.remove(target)
            shutil.copy2(real, target)
            os.chmod(target, 0o755)
            manifest["tools"][t] = {"package": pkg, "from": real, "sha256": sha256_of(target), "size": os.path.getsize(target)}
            found += 1
        if not found:
            (missing_pkgs if pkg not in OPTIONAL_PACKAGES else []).append(pkg)
            if pkg in OPTIONAL_PACKAGES:
                print(f"{pkg}: not installed (optional) - {', '.join(tools)} will be unavailable")
            continue
        manifest["packages"][pkg] = {"version": ver, "tools": found}
        copied = copy_license(pkg, lic)
        print(f"{pkg} {ver}: {found} tool(s)" + ("" if copied else "  [no copyright file found]"))
    # Debian/Ubuntu ship the 7-Zip binary as 7zz (older) or 7z (26.x); DiskWorks looks for 7zz first
    if "7z" in manifest["tools"] and "7zz" not in manifest["tools"]:
        shutil.copy2(os.path.join(dest, "7z"), os.path.join(dest, "7zz"))
        manifest["tools"]["7zz"] = dict(manifest["tools"]["7z"], alias_of="7z")
    with open(os.path.join(HERE, "bin", "linux-x86_64", "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    if missing_pkgs:
        sys.exit("\nMissing packages on this build host. Install them and rerun:\n"
                 f"  sudo apt-get install -y {' '.join(missing_pkgs)}\n")
    # smoke: every copied tool starts
    bad = []
    for t in sorted(manifest["tools"]):
        p = os.path.join(dest, t)
        try:
            r = subprocess.run([p, "--version"] if t not in ("blockdev", "mkswap", "swaplabel", "wipefs", "sfdisk", "blkid", "losetup", "e2label", "fatlabel", "fsck.fat", "mkfs.fat", "smartctl") else [p, "-V"],
                               capture_output=True, timeout=10)
            if r.returncode not in (0, 1, 2, 16, 64):
                bad.append(f"{t} (exit {r.returncode})")
        except Exception as e:
            bad.append(f"{t} ({e})")
    print(f"copied {len(manifest['tools'])} tools" + (f"; could not run: {', '.join(bad)}" if bad else ""))


def pkg_version(pkg: str) -> str:
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", pkg], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except OSError:
        pass
    try:
        r = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except OSError:
        pass
    return "?"


def copy_license(pkg: str, lic_dir: str) -> bool:
    for cand in (f"/usr/share/doc/{pkg}/copyright", f"/usr/share/licenses/{pkg}/COPYING", f"/usr/share/licenses/{pkg}/LICENSE"):
        if os.path.isfile(cand):
            shutil.copyfile(cand, os.path.join(lic_dir, f"{pkg}.txt"))
            return True
    return False


def check_required(tag: str) -> list[str]:
    base = os.path.join(HERE, "bin", tag)
    return [rel for rel in REQUIRED.get(tag, []) if not os.path.isfile(os.path.join(base, rel))]


def main(argv: list[str]) -> int:
    what = argv[1:] or [TAG]
    for w in what:
        if w == "linux-tools" or (w == "linux-x86_64" and not IS_WIN):
            linux_tools()
        elif w == "win64":
            fetch_downloads("win64")
        else:
            sys.exit(f"unknown target {w}")
    missing = check_required(TAG)
    if missing:
        print("still missing for this platform:", ", ".join(missing))
        return 1
    print("bin/%s complete" % TAG)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
