"""Online update: find a newer DiskWorks release on GitHub, download this platform's binary,
verify its SHA-256, put it in place of the running one and restart (ARCHITECTURE.md §10,
decision D-040).

Releases are made by .github/workflows/release.yml: one tag v<version> per version with
  DiskWorks-<v>-win64.exe           DiskWorks-<v>-linux-x86_64        DiskWorks-<v>-mac-arm64.zip
and a <asset>.sha256 next to each.  A release stays a pre-release until all three
platforms are built; only full releases are offered.

The check talks to api.github.com at every launch (unless the user turns that off in About)
and when they ask (Check for updates).  Nothing is sent but the request itself; when a newer
version exists the page asks "Update now" or "Skip for now".  Runs in the
window process with the user's rights: the binary's folder must be writable by the user.

Swap per platform (the running file is never written in place):
  Windows  DiskWorks.exe -> DiskWorks.old.exe (a running exe may be renamed), new file in
           its place; the next start deletes the .old.exe.
  Linux    os.replace() of the new file over the old path (the running inode lives on).
  macOS    the new DiskWorks.app is unpacked with ditto beside the old one, which is moved
           aside and deleted at the next start.
Then the new binary is started with --after-update <pid>; it waits for this process to
exit before it takes over (instance.json, the port), and this one shuts down.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = "OccuviteASI/DiskWorks"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RAW = f"https://raw.githubusercontent.com/{REPO}"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
OLD_SUFFIX = ".old"


def platform_tag() -> str:
    if IS_WIN:
        return "win64"
    if IS_MAC:
        import platform
        return "mac-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "mac-x86_64"
    return "linux-x86_64"


def asset_name(version: str, tag: str | None = None) -> str:
    tag = tag or platform_tag()
    if tag == "win64":
        return f"DiskWorks-{version}-win64.exe"
    if tag.startswith("mac"):
        return f"DiskWorks-{version}-{tag}.zip"
    return f"DiskWorks-{version}-{tag}"


def parse_version(v: str) -> tuple:
    """'0.3.10' -> (0, 3, 10); 'v1.2' -> (1, 2); anything odd sorts lowest."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)$", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else (-1,)


def newer(a: str, b: str) -> bool:
    return parse_version(a) > parse_version(b)


def install_target() -> tuple[str | None, str]:
    """(path that an update replaces, reason when it cannot be updated in place)."""
    if not getattr(sys, "frozen", False):
        return None, "DiskWorks is running from source: update it with  git pull  (and rebuild with build.py)."
    exe = os.path.realpath(sys.executable)
    if IS_MAC:
        app = os.path.dirname(os.path.dirname(os.path.dirname(exe)))   # .../DiskWorks.app/Contents/MacOS/DiskWorks
        if not app.endswith(".app"):
            return None, "This macOS build is not inside DiskWorks.app; download the new version from the release page."
        if app.startswith("/Volumes/") or "/AppTranslocation/" in app:
            return None, "Move DiskWorks.app into Applications first (macOS runs apps from a download or disk image read-only)."
        target = app
    else:
        target = exe
    folder = os.path.dirname(target)
    if not os.access(folder, os.W_OK):
        return None, f"The folder {folder} is not writable for your account; download the new version and replace DiskWorks yourself."
    return target, ""


def cleanup_after_update() -> None:
    """Delete what the previous update left: the renamed old binary / app and stray downloads."""
    target, _ = install_target()
    if not target:
        return
    folder = os.path.dirname(target)
    base = os.path.basename(target)
    stem, ext = os.path.splitext(base)
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        old = name in (stem + OLD_SUFFIX + ext, base + OLD_SUFFIX) or (name.startswith(".DiskWorks-update-") and time.time() - os.path.getmtime(p) > 600)
        if not old:
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        except OSError:
            pass   # Windows: the old exe can still be closing; the next start retries


def wait_for_exit(pid: int, timeout: float = 45.0) -> None:
    """--after-update: let the previous instance finish shutting down first."""
    import dw_helper
    t0 = time.time()
    while time.time() - t0 < timeout and dw_helper.parent_alive(pid):
        time.sleep(0.25)


def _get(url: str, timeout: float = 30.0, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "DiskWorks-updater",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=timeout)


def pick_release(releases: list, current: str, tag: str) -> dict | None:
    """The newest full release (not draft, not pre-release) that carries this platform's binary."""
    best = None
    for r in releases or []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        ver = str(r.get("tag_name") or "").lstrip("v")
        if parse_version(ver) == (-1,):
            continue
        assets = {a.get("name"): a for a in r.get("assets") or [] if isinstance(a, dict)}
        want = asset_name(ver, tag)
        if want not in assets:
            continue
        if best is None or newer(ver, best["version"]):
            a = assets[want]
            sha = assets.get(want + ".sha256")
            best = {"version": ver, "tag": r.get("tag_name"), "url": a.get("browser_download_url"), "size": a.get("size"),
                    "name": want, "shaUrl": sha.get("browser_download_url") if sha else None, "digest": a.get("digest"),
                    "page": r.get("html_url"), "published": r.get("published_at")}
    return best


def notes_between(changelog: str, current: str, latest: str) -> str:
    """The CHANGELOG sections newer than `current`, up to and including `latest`."""
    out, keep = [], False
    for line in changelog.splitlines():
        m = re.match(r"^## (\S+)", line)
        if m:
            v = m.group(1)
            keep = not newer(v, latest) and newer(v, current)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


class Updater:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        target, why = install_target()
        self.state: dict = {"phase": "idle", "current": self._version(), "latest": None, "notes": "", "page": None,
                            "size": None, "done": 0, "error": None, "canInstall": bool(target), "why": why,
                            "checked": app.settings.get("updateChecked"), "platform": platform_tag()}
        self.release: dict | None = None

    @staticmethod
    def _version() -> str:
        import diskworks
        return diskworks.VERSION

    # -- routes ------------------------------------------------------------------
    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/update":
            h._json(self.state)
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/update/check":
            h._json(self.check())
            return True
        if path == "/api/update/install":
            h._json(self.install())
            return True
        return False

    def summary(self) -> dict:
        s = self.state
        return {"phase": s["phase"], "latest": s["latest"], "available": s["phase"] in ("available", "downloading", "installing", "restarting")}

    # -- check -------------------------------------------------------------------
    def check(self) -> dict:
        if self.state["phase"] in ("downloading", "installing", "restarting"):
            return self.state
        self.state.update({"phase": "checking", "error": None})
        try:
            api = os.environ.get("DISKWORKS_UPDATE_API") or API      # a local mirror for testing the updater
            with _get(api) as r:
                releases = json.loads(r.read().decode("utf-8"))
            rel = pick_release(releases, self.state["current"], platform_tag())
            self.app.settings["updateChecked"] = time.time()
            self.app.save_settings()
            self.state["checked"] = self.app.settings["updateChecked"]
            if rel is None or not newer(rel["version"], self.state["current"]):
                self.release = None
                self.state.update({"phase": "current", "latest": rel["version"] if rel else None, "notes": "", "page": rel["page"] if rel else None})
                self.app.info(f"Update check: {self.state['current']} is the latest version")
                return self.state
            notes = ""
            try:
                raw = os.environ.get("DISKWORKS_UPDATE_RAW") or RAW
                with _get(f"{raw}/{rel['tag']}/CHANGELOG.md", timeout=20, accept="text/plain") as r:
                    notes = notes_between(r.read().decode("utf-8", "replace"), self.state["current"], rel["version"])
            except Exception:
                pass
            self.release = rel
            self.state.update({"phase": "available", "latest": rel["version"], "notes": notes, "size": rel["size"], "page": rel["page"]})
            self.app.info(f"Update available: DiskWorks {rel['version']} ({(rel['size'] or 0) / 1e6:.0f} MB)")
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", None) or e
            self.state.update({"phase": "error", "error": f"Could not reach GitHub to check for updates: {reason}"})
        return self.state

    def check_at_start(self) -> None:
        """Every launch, unless "Check for updates when DiskWorks starts" is off (About).
        A newer version makes the page offer "Update now" / "Skip for now"."""
        if self.app.settings.get("updateCheck", True) is False or os.environ.get("DISKWORKS_NO_UPDATE_CHECK"):
            return
        def run():
            time.sleep(3)          # let the window come up first
            if not self.app.stop.is_set():
                self.check()
        threading.Thread(target=run, daemon=True, name="update-check").start()

    # -- install -----------------------------------------------------------------
    def install(self) -> dict:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.state
            if self.state["phase"] != "available" or not self.release:
                raise RuntimeError("Check for updates first.")
            target, why = install_target()
            if not target:
                raise RuntimeError(why)
            jobs = self.app.jobs
            if jobs and (jobs.running() or (jobs.image and getattr(jobs.image, "running", lambda: False)())):
                raise RuntimeError("A disk job is still running. Let it finish before updating.")
            self.state.update({"phase": "downloading", "done": 0, "error": None})
            self.thread = threading.Thread(target=self._install, args=(target, dict(self.release)), daemon=True, name="update")
            self.thread.start()
            return self.state

    def _install(self, target: str, rel: dict) -> None:
        folder = os.path.dirname(target)
        part = os.path.join(folder, f".DiskWorks-update-{rel['version']}.part")
        try:
            expected = self._expected_sha(rel)
            h = hashlib.sha256()
            with _get(rel["url"], timeout=60, accept="application/octet-stream") as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or rel.get("size") or 0)
                self.state["size"] = total or self.state["size"]
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    self.state["done"] += len(chunk)
            got = h.hexdigest()
            if got != expected:
                raise RuntimeError(f"The download does not match its published SHA-256 (expected {expected[:16]}…, got {got[:16]}…). Nothing was changed.")
            self.state["phase"] = "installing"
            self.app.info(f"Update {rel['version']} downloaded and verified (SHA-256 {got[:16]}…); installing")
            new_target = self._swap(target, part)
            self.app.settings["updatedFrom"] = self.state["current"]
            self.app.save_settings()
            self.state["phase"] = "restarting"
            self._restart(new_target)
        except Exception as e:
            self.state.update({"phase": "error", "error": str(e)})
            self.app.info(f"Update failed: {e}")
            try:
                if os.path.isdir(part):
                    shutil.rmtree(part, ignore_errors=True)
                elif os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass

    def _expected_sha(self, rel: dict) -> str:
        if rel.get("shaUrl"):
            with _get(rel["shaUrl"], timeout=20, accept="application/octet-stream") as r:
                m = re.match(r"^\s*([0-9a-fA-F]{64})\b", r.read().decode("ascii", "replace"))
                if m:
                    return m.group(1).lower()
        d = str(rel.get("digest") or "")
        if d.startswith("sha256:") and len(d) == 71:
            return d[7:].lower()
        raise RuntimeError("This release has no published SHA-256 for the download, so DiskWorks will not install it.")

    def _swap(self, target: str, part: str) -> str:
        if IS_MAC:
            tmp = tempfile.mkdtemp(prefix=".DiskWorks-update-", dir=os.path.dirname(target))
            r = subprocess.run(["/usr/bin/ditto", "-x", "-k", part, tmp], capture_output=True, text=True)
            os.remove(part)
            new_app = os.path.join(tmp, "DiskWorks.app")
            if r.returncode != 0 or not os.path.isdir(new_app):
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("Could not unpack the downloaded app: " + (r.stderr or "").strip()[-200:])
            subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", new_app], capture_output=True)
            old = target[:-4] + OLD_SUFFIX + ".app"
            shutil.rmtree(old, ignore_errors=True)
            os.rename(target, old)
            try:
                os.rename(new_app, target)
            except OSError:
                os.rename(old, target)   # put the old one back
                raise
            shutil.rmtree(tmp, ignore_errors=True)
            return target
        if IS_WIN:
            stem, ext = os.path.splitext(target)
            old = stem + OLD_SUFFIX + ext
            try:
                if os.path.exists(old):
                    os.remove(old)
            except OSError:
                old = f"{stem}{OLD_SUFFIX}-{int(time.time())}{ext}"
            os.rename(target, old)          # a running .exe can be renamed, not overwritten
            try:
                os.replace(part, target)
            except OSError:
                os.rename(old, target)
                raise
            return target
        os.chmod(part, 0o755)
        os.replace(part, target)            # the running process keeps the old inode
        return target

    def _restart(self, target: str) -> None:
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")   # a fresh onefile extraction, not ours
        for k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC"):
            env.pop(k, None)
        args = list(getattr(self.app, "launch_args", []) or []) + ["--after-update", str(os.getpid())]
        if IS_MAC:
            cmd = ["/usr/bin/open", "-n", target, "--args"] + args
            kw: dict = {}
        else:
            cmd = [target] + args
            kw = {"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True}   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=os.path.dirname(target) if not IS_MAC else None, close_fds=True, **kw)
        self.app.info("Restarting into the new version")
        time.sleep(0.8)                     # let the UI see "restarting" before the server goes
        self.app.quit_app()
