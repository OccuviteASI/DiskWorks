"""Build a single-file DiskWorks executable for the platform this script runs on.

    python build.py              Windows -> dist/win64/DiskWorks.exe
                                 Linux   -> dist/linux-x86_64/DiskWorks
    python build.py --wsl        from Windows: run the Linux build inside WSL too
    python build.py --wsl --linux-only
    python build.py --console    keep a console window (Windows debugging)
    python build.py --onedir     folder build instead of one file (faster start)
    ./build-mac.sh               macOS: venv + deps + DiskWorks.app (see MACOS.md)

PyInstaller cannot cross-compile, so each platform builds its own binary.  The Linux
binary carries the partition and filesystem tools copied by `fetch-helpers.py
linux-tools` (bin/linux-x86_64/tools) and PyInstaller collects their shared libraries;
the Windows binary needs only what ships with Windows.  Nothing is downloaded at run time.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "DiskWorks"
WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"
BUNDLE_ID = "com.asirobots.diskworks"


def _tag() -> str:
    if WINDOWS:
        return "win64"
    if MAC:
        import platform as _pf
        return "mac-arm64" if _pf.machine().lower() in ("arm64", "aarch64") else "mac-x86_64"
    return "linux-x86_64"


TAG = _tag()
DIST = os.path.join(HERE, "dist", TAG)
WORK = os.path.join(os.environ.get("TEMP") or "/tmp", "diskworks-build", TAG)   # outside the tree (sync clients lock files)
SEP = ";" if WINDOWS else ":"
WSL_VENV = "/opt/diskworksbuild"


def version() -> str:
    src = open(os.path.join(HERE, "diskworks.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else "0.0.0"


def check_python_deps() -> None:
    try:
        import webview  # noqa: F401
    except ImportError:
        sys.exit("build: pywebview is not installed in this Python -- pip install -r requirements.txt")
    if LINUX:
        try:
            import PySide6.QtWebEngineWidgets  # noqa: F401
        except ImportError:
            sys.exit("build: PySide6 (with QtWebEngine) is missing -- pip install -r requirements.txt")
    if MAC:
        try:
            import AppKit, WebKit, objc  # noqa: F401
        except ImportError:
            sys.exit("build: PyObjC (AppKit/WebKit) is missing -- pip install -r requirements.txt")
    for mod in ("pycdlib", "pyfatfs"):
        try:
            __import__(mod)
        except ImportError:
            print(f"WARNING: {mod} is not installed; the build will lack the feature that needs it (pip install -r requirements.txt)")


def check_binaries() -> list[str]:
    """Bundled tools for this platform (Linux: bin/linux-x86_64/tools/*; Windows: 7-Zip and, when fetched, smartctl)."""
    fh_path = os.path.join(HERE, "fetch-helpers.py")
    spec = {"__file__": fh_path, "__name__": "fetch_helpers"}   # the module name has a dash, so exec it
    with open(fh_path, encoding="utf-8") as f:
        code = compile(f.read(), fh_path, "exec")
    exec(code, spec)
    missing = spec["check_required"](TAG)
    if missing:
        sys.exit(f"build: bin/{TAG}/ is missing {', '.join(missing)} -- run: python fetch-helpers.py "
                 + ("linux-tools" if LINUX else TAG))
    if LINUX:
        tools = sorted(glob.glob(os.path.join(HERE, "bin", TAG, "tools", "*")))
        for t in tools:
            os.chmod(t, 0o755)
        return tools
    if WINDOWS:
        return sorted(glob.glob(os.path.join(HERE, "bin", TAG, "7zip", "7z.*")) + glob.glob(os.path.join(HERE, "bin", TAG, "smartmontools", "smartctl.exe")))
    return []


def pyinstaller_cmd(console: bool, onedir: bool, binaries: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir" if onedir else "--onefile",
           "--name", NAME, "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
           "--paths", HERE,
           "--add-data", os.path.join(HERE, "ui") + SEP + "ui",
           "--add-data", os.path.join(HERE, "CHANGELOG.md") + SEP + ".",
           "--hidden-import", "dw_jobs", "--hidden-import", "dw_ops_exec", "--hidden-import", "dw_image",
           "--hidden-import", "dw_access", "--hidden-import", "dw_helper", "--hidden-import", "dw_ipc",
           "--hidden-import", "dw_speed", "--hidden-import", "dw_space", "--hidden-import", "dw_smart",
           "--hidden-import", "dw_win" if WINDOWS else "dw_linux",
           "--exclude-module", "tkinter", "--exclude-module", "unittest",
           "--exclude-module", "webview.platforms.cef", "--exclude-module", "webview.platforms.android",
           "--exclude-module", "webview.platforms.cocoa", "--exclude-module", "webview.platforms.mshtml"]
    for b in binaries:
        # keep each tool's folder under bin/<tag>/ (7zip/, smartmontools/, tools/)
        sub = os.path.relpath(os.path.dirname(b), os.path.join(HERE, "bin", TAG))
        cmd += ["--add-binary", b + SEP + os.path.join("bin", TAG, sub)]
    lic7 = os.path.join(HERE, "bin", "win64", "7zip", "License.txt")
    if WINDOWS and os.path.isfile(lic7):
        cmd += ["--add-data", lic7 + SEP + os.path.join("bin", TAG, "7zip")]
    lic_dir = os.path.join(HERE, "bin", TAG, "LICENSES")
    if os.path.isdir(lic_dir):
        cmd += ["--add-data", lic_dir + SEP + os.path.join("bin", TAG, "LICENSES")]
    man = os.path.join(HERE, "bin", TAG, "MANIFEST.json")
    if os.path.isfile(man):
        cmd += ["--add-data", man + SEP + os.path.join("bin", TAG)]
    for asset in ("diskworks.png",):
        ap = os.path.join(HERE, "assets", asset)
        if os.path.isfile(ap):
            cmd += ["--add-data", ap + SEP + "assets"]
    if WINDOWS:
        ico = os.path.join(HERE, "assets", "diskworks.ico")
        if os.path.isfile(ico):
            cmd += ["--icon", ico, "--add-data", ico + SEP + "assets"]
        if not console:
            cmd.append("--noconsole")
        cmd += ["--hidden-import", "webview.platforms.winforms", "--hidden-import", "webview.platforms.edgechromium",
                "--hidden-import", "webview.platforms.win32", "--hidden-import", "clr",
                "--exclude-module", "webview.platforms.qt", "--exclude-module", "webview.platforms.gtk"]
    elif MAC:
        icns = os.path.join(HERE, "assets", "diskworks.icns")
        if os.path.isfile(icns):
            cmd += ["--icon", icns]
        if not console:
            cmd.append("--windowed")           # makes the .app bundle
        cmd += ["--osx-bundle-identifier", BUNDLE_ID, "--collect-all", "webview",
                "--hidden-import", "webview.platforms.cocoa", "--hidden-import", "objc",
                "--hidden-import", "AppKit", "--hidden-import", "Foundation", "--hidden-import", "WebKit",
                "--hidden-import", "Security", "--hidden-import", "Quartz", "--hidden-import", "PyObjCTools.AppHelper",
                "--hidden-import", "dw_mac",
                "--exclude-module", "webview.platforms.qt", "--exclude-module", "webview.platforms.gtk",
                "--exclude-module", "webview.platforms.winforms", "--exclude-module", "webview.platforms.edgechromium",
                "--exclude-module", "PySide6", "--exclude-module", "PyQt5", "--exclude-module", "PyQt6"]
    else:
        cmd += ["--hidden-import", "webview.platforms.qt", "--hidden-import", "qtpy",
                "--hidden-import", "PySide6.QtWebEngineWidgets", "--hidden-import", "PySide6.QtWebEngineCore",
                "--hidden-import", "PySide6.QtWebChannel", "--hidden-import", "PySide6.QtNetwork",
                "--hidden-import", "PySide6.QtPrintSupport",
                "--exclude-module", "webview.platforms.gtk", "--exclude-module", "webview.platforms.winforms",
                "--exclude-module", "webview.platforms.edgechromium"]
    cmd.append(os.path.join(HERE, "diskworks.py"))
    return cmd


def write_licenses(dist: str) -> None:
    out = os.path.join(dist, "THIRD-PARTY-LICENSES.txt")
    parts = ["DiskWorks bundles the following third-party software.\n"]
    lic_dir = os.path.join(HERE, "bin", TAG, "LICENSES")
    if os.path.isdir(lic_dir):
        for f in sorted(os.listdir(lic_dir)):
            parts.append(f"\n===== {f[:-4]} =====\n")
            with open(os.path.join(lic_dir, f), encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read())
    lic7 = os.path.join(HERE, "bin", "win64", "7zip", "License.txt")
    if WINDOWS and os.path.isfile(lic7):
        parts.append("\n===== 7-Zip (read-only browser) =====\n")
        with open(lic7, encoding="utf-8", errors="replace") as fh:
            parts.append(fh.read())
    lics = os.path.join(HERE, "bin", "win64", "smartmontools", "COPYING.txt")
    if WINDOWS and os.path.isfile(lics):
        parts.append("\n===== smartmontools (drive health, GPL-2.0-or-later) =====\n")
        with open(lics, encoding="utf-8", errors="replace") as fh:
            parts.append(fh.read())
    parts.append("\n===== Python packages =====\npywebview (BSD-3), pythonnet (MIT, Windows), PySide6/Qt (LGPL-3, Linux), "
                 "PyObjC (MIT, macOS), pycdlib (LGPL-2.1), pyfatfs (MIT), PyInstaller bootloader (GPL with exception).\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))


def build_here(console: bool, onedir: bool) -> int:
    ver = version()
    check_python_deps()
    binaries = check_binaries()
    os.makedirs(DIST, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    if MAC and not console:
        onedir = True        # one extraction inside the .app; sys._MEIPASS = Contents/Frameworks
    out = os.path.join(DIST, NAME + (".exe" if WINDOWS else ""))
    if onedir:
        out = os.path.join(DIST, NAME, NAME + (".exe" if WINDOWS else ""))
    app_bundle = os.path.join(DIST, NAME + ".app") if MAC and not console else None
    if app_bundle:
        out = os.path.join(app_bundle, "Contents", "MacOS", NAME)
        shutil.rmtree(app_bundle, ignore_errors=True)
    before = os.path.getmtime(out) if os.path.exists(out) else 0
    print(f"Building {NAME} {ver} for {TAG} ({'onedir' if onedir else 'onefile'}; {len(binaries)} bundled tools) ...")
    t0 = time.time()
    code = subprocess.call(pyinstaller_cmd(console, onedir, binaries), cwd=HERE)
    if code != 0:
        sys.exit(f"PyInstaller exited {code} -- build FAILED (anything in dist/ is stale)")
    if not os.path.exists(out):
        sys.exit("build produced no binary at " + out)
    if os.path.getmtime(out) <= before:
        sys.exit("binary was not rewritten -- build FAILED, dist/ holds a stale build")
    if not WINDOWS:
        os.chmod(out, 0o755)
    if app_bundle:
        finish_mac_bundle(app_bundle, ver)
    with open(os.path.join(DIST, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(ver + "\n")
    write_licenses(DIST)
    size = os.path.getsize(out) / 1e6
    print(f"Done in {time.time() - t0:.0f}s: {out} ({size:.0f} MB)")
    if app_bundle:
        zip_path = os.path.join(DIST, f"{NAME}-{ver}-{TAG}.zip")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        # ditto keeps the bundle's attributes and signature intact, unlike a plain zip
        if subprocess.call(["ditto", "-c", "-k", "--keepParent", app_bundle, zip_path]) == 0:
            print(f"App bundle: {app_bundle}\nZip for sharing: {zip_path}")
    return 0


def finish_mac_bundle(app: str, ver: str) -> None:
    """Add the Info.plist keys macOS wants (version, HiDPI, the removable-volumes usage text
    behind the TCC prompt), then ad-hoc sign the whole bundle."""
    import plistlib
    plist = os.path.join(app, "Contents", "Info.plist")
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleShortVersionString": ver, "CFBundleVersion": ver,
        "CFBundleDisplayName": NAME, "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRemovableVolumesUsageDescription": "DiskWorks reads and writes external disks and USB sticks to show, partition, image and repair them.",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    })
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    # Ad-hoc signature: lets the app run locally after "Open anyway"; use a Developer ID for distribution.
    r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], capture_output=True, text=True)
    if r.returncode != 0:
        print("WARNING: codesign failed: " + (r.stderr or "").strip())


def build_in_wsl(extra_args: list[str]) -> int:
    """From Windows: run this script inside WSL (Ubuntu) with the /opt/diskworksbuild venv."""
    if not WINDOWS:
        sys.exit("--wsl only makes sense on Windows")
    drive, rest = os.path.splitdrive(HERE)
    src = "/mnt/" + drive[0].lower() + rest.replace("\\", "/")
    stage = "/tmp/diskworks-src"
    py = f"{WSL_VENV}/bin/python"
    script = (
        f"set -e; test -x {py} || {{ echo 'missing {WSL_VENV}: sudo python3 -m venv {WSL_VENV} && "
        f"sudo {WSL_VENV}/bin/pip install -r requirements.txt'; exit 2; }}; "
        f"mkdir -p {stage}; rsync -a --delete --exclude dist --exclude build --exclude __pycache__ --exclude .git '{src}/' {stage}/; "
        f"cd {stage}; test -f bin/linux-x86_64/MANIFEST.json || {py} fetch-helpers.py linux-tools; "
        f"{py} build.py {' '.join(extra_args)}; "
        f"mkdir -p '{src}/dist'; rm -rf '{src}/dist/linux-x86_64'; cp -r {stage}/dist/linux-x86_64 '{src}/dist/'; "
        f"mkdir -p '{src}/bin'; rm -rf '{src}/bin/linux-x86_64'; cp -r {stage}/bin/linux-x86_64 '{src}/bin/'; "
        f"ls -la '{src}/dist/linux-x86_64'"
    )
    print("Running the Linux build inside WSL ...")
    return subprocess.call(["wsl", "-e", "bash", "-lc", script])


def main() -> int:
    args = sys.argv[1:]
    console = "--console" in args
    onedir = "--onedir" in args
    if "--wsl" in args:
        code = build_in_wsl([a for a in args if a not in ("--wsl", "--console", "--linux-only")])
        if code:
            return code
        if "--linux-only" in args:
            return 0
    return build_here(console, onedir)


if __name__ == "__main__":
    sys.exit(main())
