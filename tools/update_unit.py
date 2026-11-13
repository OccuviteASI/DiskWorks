"""Unit checks for dw_update (the online updater): version order, which release is offered,
the notes shown, checksum sources.  Runs anywhere, offline:   python tools/update_unit.py
"""
import os
import sys

# Windows consoles default to a legacy code page; print the test text as UTF-8 anywhere
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
import dw_update as u  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def rel(ver, tags=("win64", "linux-x86_64", "mac-arm64"), pre=False, draft=False, sha=True, digest=None):
    assets = []
    for t in tags:
        n = u.asset_name(ver, t)
        assets.append({"name": n, "size": 1000, "browser_download_url": f"https://x/{n}", "digest": digest})
        if sha:
            assets.append({"name": n + ".sha256", "size": 80, "browser_download_url": f"https://x/{n}.sha256"})
    return {"tag_name": "v" + ver, "prerelease": pre, "draft": draft, "assets": assets, "html_url": f"https://x/v{ver}"}


print("versions")
check(u.parse_version("0.3.10") > u.parse_version("0.3.9"), "0.3.10 > 0.3.9 (numeric, not text)")
check(u.parse_version("v1.0") == (1, 0) and u.parse_version("1.0.0") > u.parse_version("1.0"), "v prefix; 1.0.0 > 1.0")
check(u.parse_version("1.0-beta") == (-1,) and not u.newer("garbage", "0.0.1"), "odd tags sort lowest")
check(u.newer("0.4.0", "0.3.3") and not u.newer("0.3.3", "0.3.3"), "newer() is strict")

print("asset names")
check(u.asset_name("0.4.0", "win64") == "DiskWorks-0.4.0-win64.exe", "windows .exe")
check(u.asset_name("0.4.0", "linux-x86_64") == "DiskWorks-0.4.0-linux-x86_64", "linux bare binary")
check(u.asset_name("0.4.0", "mac-arm64") == "DiskWorks-0.4.0-mac-arm64.zip", "mac zip")

print("which release is offered")
rels = [rel("0.4.1", pre=True), rel("0.4.0"), rel("0.3.3", tags=("mac-arm64",), pre=True), rel("0.5.0", draft=True), rel("0.3.9")]
best = u.pick_release(rels, "0.3.3", "linux-x86_64")
check(best and best["version"] == "0.4.0", f"newest full release with this platform: {best and best['version']}")
check(best["shaUrl"].endswith("DiskWorks-0.4.0-linux-x86_64.sha256") and best["url"].endswith("DiskWorks-0.4.0-linux-x86_64"), "download and checksum URLs")
check(u.pick_release([rel("0.9.0", tags=("win64",))], "0.3.3", "linux-x86_64") is None, "a release without this platform's binary is skipped")
check(u.pick_release([rel("0.4.1", pre=True)], "0.3.3", "win64") is None, "pre-releases (still building) are never offered")
check(u.pick_release([], "0.3.3", "win64") is None and u.pick_release(None, "0.3.3", "win64") is None, "empty / missing list")

print("release notes between versions")
cl = "# Changelog\n\nintro\n\n## 0.4.1 - x\n- c\n\n## 0.4.0 - y\n- b\n\n## 0.3.3 - z\n- a\n"
n = u.notes_between(cl, "0.3.3", "0.4.0")
check("## 0.4.0" in n and "- b" in n and "0.4.1" not in n and "0.3.3" not in n, "only the versions after the current one, up to the offered one")
check(u.notes_between(cl, "0.3.2", "0.4.1").count("## ") == 3, "several versions skipped -> all their notes")

print("checksum source")
up = u.Updater.__new__(u.Updater)
check(up._expected_sha({"digest": "sha256:" + "ab" * 32}) == "ab" * 32, "GitHub's asset digest when there is no .sha256 file")
try:
    up._expected_sha({"digest": None})
    check(False, "no checksum -> refuse")
except RuntimeError:
    check(True, "no checksum -> refuse to install")

print("leftovers after an update")
import tempfile, time as _t
with tempfile.TemporaryDirectory() as d:
    names = ["DiskWorks.exe", "DiskWorks.old.exe", "DiskWorks.old-1794600000.exe", "DiskWorks.exe.sha256",
             "Other.old.exe", "DiskWorks-notes.txt", ".DiskWorks-update-0.4.3.part", ".DiskWorks-update-0.4.4.part"]
    for n in names:
        open(os.path.join(d, n), "w").close()
    old = os.path.join(d, ".DiskWorks-update-0.4.3.part")
    os.utime(old, (_t.time() - 3600, _t.time() - 3600))
    got = sorted(os.path.basename(p) for p in u.leftovers(d, "DiskWorks.exe"))
    check(got == [".DiskWorks-update-0.4.3.part", "DiskWorks.old-1794600000.exe", "DiskWorks.old.exe"],
          f"old exe, timestamped old exe, stale download only: {got}")
    os.mkdir(os.path.join(d, "DiskWorks.old.app"))
    mac = sorted(os.path.basename(p) for p in u.leftovers(d, "DiskWorks.app"))
    check(mac == [".DiskWorks-update-0.4.3.part", "DiskWorks.old.app"], f"macOS: DiskWorks.old.app (and the stale download): {mac}")
    check(all(u._remove(p) for p in u.leftovers(d, "DiskWorks.exe")) and not u.leftovers(d, "DiskWorks.exe"), "removed")
    check(sorted(os.listdir(d)) == sorted(["DiskWorks.exe", "DiskWorks.exe.sha256", "Other.old.exe", "DiskWorks-notes.txt",
                                            ".DiskWorks-update-0.4.4.part", "DiskWorks.old.app"]), "nothing else touched")

print("--after-update")
check(u.parse_pids("1234") == [1234] and u.parse_pids("1234,567") == [1234, 567], "one pid (0.4.0-0.4.2) or pid,launcher pid")
check(u.parse_pids(None) == [] and u.parse_pids("x,0,-3") == [], "junk ignored")
check(u.own_pids() == [os.getpid()], "from source: no launcher pid")

print("install target")
t, why = u.install_target()
check(t is None and "source" in why, "running from source is never overwritten")

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("RESULT: all checks passed")
