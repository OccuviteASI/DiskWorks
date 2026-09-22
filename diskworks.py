"""DiskWorks - disks and partitions without the command line.

Run `python diskworks.py`. It starts a tiny local web server on 127.0.0.1 and shows
the UI in its own native window (pywebview: WebView2 on Windows, Qt on Linux). The
app exits when the last window closes. `--browser` opens a normal browser tab
instead, `--no-open` just serves (the dev server), `--fixture file.json` shows a
saved inventory instead of this machine's disks.

`--helper <address>` is the privileged helper mode (started elevated by the app
itself; see dw_helper.py). No UI, no server.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dw_fs
import dw_inventory as di

APP_NAME = "DiskWorks"
VERSION = "0.2.2"
IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
WINDOW_SIZE = (1240, 900)
WINDOW_MIN = (900, 620)
HEARTBEAT_GRACE = 90          # browser mode only: seconds without a heartbeat before closing
INVENTORY_PERIOD = 4.0        # seconds between unprivileged inventory refreshes


def resource_dir() -> str:
    """Where ui/ and assets/ live (PyInstaller unpacks data files under sys._MEIPASS)."""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def plat_tag() -> str:
    if IS_WIN:
        return "win64"
    if IS_MAC:
        import platform as _pf
        return "mac-arm64" if _pf.machine().lower() in ("arm64", "aarch64") else "mac-x86_64"
    return "linux-x86_64"


def state_dir() -> str:
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
    elif sys.platform == "darwin":
        d = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP_NAME.lower())
    os.makedirs(d, exist_ok=True)
    return d


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def is_admin() -> bool:
    try:
        if IS_WIN:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Event log (seq-numbered, polled by the UI)
# ----------------------------------------------------------------------------
class EventLog:
    def __init__(self, keep: int = 6000):
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.seq = 0
        self.keep = keep

    def push(self, ev: dict) -> dict:
        with self.lock:
            self.seq += 1
            ev["seq"] = self.seq
            ev.setdefault("ts", time.time())
            self.events.append(ev)
            if len(self.events) > self.keep:
                del self.events[: self.keep // 5]
        return ev

    def since(self, seq: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def clear(self) -> None:
        with self.lock:
            self.events.clear()


# ----------------------------------------------------------------------------
# Application state shared by all request handlers
# ----------------------------------------------------------------------------
class App:
    def __init__(self, windowed: bool, fixture: str | None = None):
        self.windowed = windowed
        self.sd = state_dir()
        self.settings_file = os.path.join(self.sd, "settings.json")
        self.settings: dict = read_json(self.settings_file, {}) or {}
        self.server = None
        self.url = ""
        self.windows: list = []
        self.last_heartbeat = time.time()
        self.stop = threading.Event()
        self.fixture = fixture
        self.log = EventLog(4000)            # session command log (Log tab)
        self.inv_events = EventLog(2000)     # inventory change events
        self.inv: dict | None = None
        self.inv_error: str | None = None
        self.inv_lock = threading.Lock()
        self.inv_wanted = threading.Event()  # set by ?refresh=1 to poll now
        self.last_client_poll = time.time()  # inventory refreshes pause when no window is watching
        self.helper = None                   # dw_ipc.HelperClient once started (phase B)
        self.jobs = None                     # job manager (phase C)
        self.info("DiskWorks %s starting (%s)" % (VERSION, "fixture " + fixture if fixture else di.PLATFORM))
        threading.Thread(target=self._inventory_loop, daemon=True, name="inventory").start()

    # -- settings -------------------------------------------------------------
    def save_settings(self) -> None:
        write_json(self.settings_file, self.settings)

    # -- log ------------------------------------------------------------------
    def info(self, msg: str) -> None:
        self.log.push({"type": "info", "msg": msg})

    def log_cmd(self, cmd: str, code: int | None, ms: float, output: str = "", title: str = "") -> None:
        self.log.push({"type": "cmd", "title": title, "cmd": cmd, "code": code, "ms": round(ms),
                       "output": output[-20000:] if output else ""})

    # -- inventory ------------------------------------------------------------
    def _inventory_loop(self) -> None:
        while not self.stop.is_set():
            if self.inv is None or self.inv_wanted.is_set() or time.time() - self.last_client_poll < 15:
                self.inv_wanted.clear()
                self.refresh_inventory()
                period = INVENTORY_PERIOD * (1.5 if self.helper_ready() else 1.0)
            else:
                period = 1.0
            self.inv_wanted.wait(period)

    def refresh_inventory(self) -> dict | None:
        t0 = time.time()
        try:
            if self.fixture:
                inv = di.load_fixture(self.fixture)
            elif self.helper and self.helper.state == "ready":
                try:
                    inv = self.helper.request("inventory", timeout=120)["inventory"]
                except RuntimeError as e:
                    self.info(f"Helper inventory failed, using the unprivileged view: {e}")
                    inv = di.inventory()
            else:
                inv = di.inventory()
            err = None
        except Exception as e:  # keep serving the last good snapshot
            inv, err = None, str(e)
        with self.inv_lock:
            prev = self.inv
            if inv is not None and IS_MAC:
                self.mac_tcc(inv)
            if inv is not None:
                inv["elevated"] = is_admin() or inv.get("elevated", False)
                inv["fixture"] = bool(self.fixture)
                inv["refreshMs"] = round((time.time() - t0) * 1000)
                changed = prev is None or prev.get("hash") != inv.get("hash") or _ids(prev) != _ids(inv)
                self.inv = inv
                self.inv_error = None
                if changed:
                    self.inv_events.push({"type": "changed", "hash": inv["hash"], "disks": len(inv["disks"]),
                                          "summary": _diff_summary(prev, inv)})
            else:
                if self.inv_error != err:
                    self.info(f"Inventory refresh failed: {err}")
                self.inv_error = err
        return inv

    def current_inventory(self) -> dict:
        with self.inv_lock:
            if self.inv is None:
                if self.inv_error:
                    raise RuntimeError(self.inv_error)
                return {"ts": 0, "platform": di.PLATFORM, "hash": "", "disks": [], "pending": True}
            out = dict(self.inv)
            out["error"] = self.inv_error
            return out

    def mac_tcc(self, inv: dict) -> None:
        """Open each external raw disk read-only once from this (window) process: that is what
        makes macOS show its 'Removable Volumes' consent for the app; the root helper inherits it."""
        import dw_mac
        probed = getattr(self, "_tcc", {})
        for d in inv.get("disks", []):
            if d.get("internal") or d["id"] in probed:
                continue
            probed[d["id"]] = dw_mac.tcc_probe(d.get("rawPath") or d["path"])
            if probed[d["id"]] == "denied":
                self.info(f"macOS denied access to {d['name']}: allow DiskWorks under Privacy & Security → Files and Folders / Full Disk Access")
        self._tcc = probed
        for d in inv.get("disks", []):
            d["tcc"] = probed.get(d["id"])

    # -- privileged helper ----------------------------------------------------
    def start_helper(self) -> dict:
        if self.fixture:
            raise RuntimeError("This is a saved inventory (fixture); there is nothing to unlock.")
        import dw_ipc
        if self.helper is None or self.helper.state in ("absent", "failed"):
            self.helper = dw_ipc.HelperClient(self)
        self.helper.start()
        return self.helper.status()

    def helper_ready(self) -> bool:
        return bool(self.helper and self.helper.state == "ready")

    def on_helper_ready(self) -> None:
        self.inv_wanted.set()

    # -- windows / lifecycle --------------------------------------------------
    def on_window_closed(self, win) -> None:
        try:
            self.windows.remove(win)
        except ValueError:
            pass

    def request_new_window(self) -> bool:
        if not self.windowed:
            return False
        try:
            open_native_window(self, self.url)
            return True
        except Exception:
            return False

    def watch(self) -> None:
        """Browser mode: close when the tab is gone (never while a job runs)."""
        while not self.stop.is_set():
            time.sleep(5)
            busy = bool(self.jobs and self.jobs.running())
            if not busy and time.time() - self.last_heartbeat > HEARTBEAT_GRACE:
                self.shutdown()
                return

    def stop_tools(self) -> None:
        self.stop.set()
        try:
            if self.jobs:
                self.jobs.cancel_all()
        except Exception:
            pass
        try:
            if self.helper:
                self.helper.quit()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop_tools()
        for w in list(self.windows):
            try:
                w.destroy()
            except Exception:
                pass
        if self.server and not self.windowed:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def _ids(inv: dict | None) -> list[str]:
    if not inv:
        return []
    return sorted(d["id"] for d in inv.get("disks", []))


def _diff_summary(prev: dict | None, cur: dict) -> str:
    if not prev:
        return f"{len(cur['disks'])} disk(s) found"
    a, b = set(_ids(prev)), set(_ids(cur))
    parts = []
    if b - a:
        parts.append("added " + ", ".join(sorted(b - a)))
    if a - b:
        parts.append("removed " + ", ".join(sorted(a - b)))
    if not parts:
        parts.append("layout changed")
    return "; ".join(parts)


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- helpers --------------------------------------------------------------
    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, ctype: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("The request was not valid JSON.")
        return data if isinstance(data, dict) else {}

    def _static(self, rel: str) -> None:
        root = os.path.join(resource_dir(), "ui")
        path = os.path.normpath(os.path.join(root, rel.lstrip("/")))
        if not path.startswith(root) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".js"):
            ctype = "text/javascript"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- GET ------------------------------------------------------------------
    def do_GET(self):
        app = self.app
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._static("index.html")
            if p.startswith("/ui/"):
                return self._static(p[4:])
            if p == "/api/ping":
                return self._json({"app": APP_NAME, "version": VERSION})
            if p == "/api/status":
                return self._json(status_payload(app))
            if p == "/api/inventory":
                if q.get("refresh"):
                    app.inv_wanted.set()
                    inv = app.refresh_inventory() or app.current_inventory()
                    return self._json(inv)
                return self._json(app.current_inventory())
            if p == "/api/inventory/events":
                app.last_client_poll = time.time()
                since = int(q.get("since", ["0"])[0])
                return self._json({"events": app.inv_events.since(since), "seq": app.inv_events.seq,
                                   "hash": (app.inv or {}).get("hash"), "error": app.inv_error})
            if p == "/api/log":
                since = int(q.get("since", ["0"])[0])
                return self._json({"events": app.log.since(since), "seq": app.log.seq})
            if p == "/api/settings":
                return self._json(app.settings)
            if p == "/api/changelog":
                path = os.path.join(resource_dir(), "CHANGELOG.md")
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return self._text(f.read())
                except OSError:
                    return self._text("No release notes were bundled with this build.")
            if p == "/api/fs":
                return self._json({"fs": dw_fs.FS, "choices": dw_fs.FORMAT_CHOICES.get(di.PLATFORM, []),
                                   "gptTypes": dw_fs.GPT_TYPES, "mbrTypes": {str(k): v for k, v in dw_fs.MBR_TYPES.items()}})
            if app.jobs and p.startswith("/api/"):
                handled = app.jobs.handle_get(self, p, q)
                if handled:
                    return
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": f"Unexpected problem: {e}"}, 500)
        self.send_error(404)

    # -- POST -----------------------------------------------------------------
    def do_POST(self):
        app = self.app
        p = urlparse(self.path).path
        try:
            body = self._body()
            if p == "/api/heartbeat":
                app.last_heartbeat = time.time()
                return self._json({"ok": True})
            if p == "/api/settings":
                patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
                for k, v in patch.items():
                    if v is None:
                        app.settings.pop(k, None)
                    else:
                        app.settings[k] = v
                app.save_settings()
                return self._json(app.settings)
            if p == "/api/window":
                return self._json({"ok": app.request_new_window()})
            if p == "/api/helper/start":
                return self._json(app.start_helper())
            if p == "/api/mac/settings" and IS_MAC:
                import dw_mac
                dw_mac.open_full_disk_access_settings()
                return self._json({"ok": True})
            if p == "/api/export":
                return self._json(export_text(app, body))
            if p == "/api/log/export":
                lines = []
                for e in app.log.since(0):
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
                    if e["type"] == "cmd":
                        lines.append(f"[{ts}] {e.get('title') or ''}\n  $ {e['cmd']}\n  -> exit {e.get('code')} in {e.get('ms')} ms")
                        if e.get("output"):
                            lines.append("  " + e["output"].replace("\n", "\n  "))
                    else:
                        lines.append(f"[{ts}] {e.get('msg', '')}")
                return self._json(export_text(app, {"name": f"diskworks-log-{time.strftime('%Y%m%d-%H%M%S')}.txt",
                                                    "text": "\n".join(lines) + "\n"}))
            if p == "/api/quit":
                self._json({"ok": True})
                app.shutdown()
                return
            if app.jobs and p.startswith("/api/"):
                handled = app.jobs.handle_post(self, p, body)
                if handled:
                    return
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # keep the UI informed rather than dropping the socket
            return self._json({"error": f"Unexpected problem: {e}"}, 500)
        self.send_error(404)


def status_payload(app: App) -> dict:
    helper = {"state": "absent", "message": "View only. Changing disks needs administrator rights (Unlock)."}
    if app.helper:
        helper = app.helper.status()
    return {
        "app": APP_NAME, "version": VERSION, "platform": di.PLATFORM, "windowed": app.windowed,
        "isAdmin": is_admin(), "fixture": app.fixture, "helper": helper,
        "inventoryError": app.inv_error, "stateDir": app.sd, "frozen": bool(getattr(sys, "frozen", False)),
        "bundle": bundle_info(),
        "features": {"ops": bool(app.jobs), "image": bool(app.jobs), "access": bool(app.jobs), "speed": bool(app.jobs and app.jobs.speed), "space": bool(app.jobs and app.jobs.space)},
    }


def bundle_info() -> dict:
    """What is packed inside this binary (for About and the smoke tests)."""
    out = {"tools": 0, "toolsDir": None, "manifest": None}
    if IS_LINUX:
        try:
            import dw_linux
            d = dw_linux.tools_dir()
            if os.path.isdir(d):
                out["toolsDir"] = d
                out["tools"] = len([f for f in os.listdir(d) if os.access(os.path.join(d, f), os.X_OK)])
            man = os.path.join(resource_dir(), "bin", "linux-x86_64", "MANIFEST.json")
            if os.path.isfile(man):
                out["manifest"] = read_json(man, {}).get("created")
        except Exception:
            pass
    return out


def export_text(app: App, body: dict) -> dict:
    """Native Save dialog + write (the UI never downloads files itself)."""
    name = os.path.basename(str(body.get("name") or "export.txt"))
    text = str(body.get("text") or "")
    dest = None
    if app.windowed and app.windows:
        try:
            import webview
            win = app.windows[0]
            res = win.create_file_dialog(webview.FileDialog.SAVE, directory=downloads_dir(), save_filename=name)
            if isinstance(res, (list, tuple)):
                res = res[0] if res else None
            dest = res
        except Exception as e:
            raise RuntimeError(f"The save dialog could not be opened: {e}")
        if not dest:
            return {"ok": False, "cancelled": True}
    else:
        dest = os.path.join(downloads_dir(), name)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    app.info(f"Saved {dest}")
    return {"ok": True, "path": dest}


def downloads_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not print a traceback when a client aborts a request."""
    daemon_threads = True
    verbose = False

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# ----------------------------------------------------------------------------
# Native window (pywebview) - same shape as LinkTest
# ----------------------------------------------------------------------------
def gui_backend() -> str | None:
    if IS_WIN:
        return "edgechromium"
    if IS_LINUX:
        return "qt"
    if IS_MAC:
        return "cocoa"
    return None


def webview_storage_dir(sd: str) -> str:
    d = os.path.join(sd, "webview")
    os.makedirs(d, exist_ok=True)
    return d


def icon_path() -> str | None:
    name = "diskworks.ico" if IS_WIN else "diskworks.png"
    p = os.path.join(resource_dir(), "assets", name)
    return p if os.path.isfile(p) else None


def _saved_geometry(app: App) -> dict:
    g = app.settings.get("window") if isinstance(app.settings.get("window"), dict) else {}
    out = {"width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1], "x": None, "y": None, "maximized": bool(g.get("maximized"))}
    try:
        w, h = int(g.get("width") or 0), int(g.get("height") or 0)
        if WINDOW_MIN[0] <= w <= 6000 and WINDOW_MIN[1] <= h <= 6000:
            out["width"], out["height"] = w, h
        if g.get("x") is not None and g.get("y") is not None:
            x, y = int(g["x"]), int(g["y"])
            if -20000 < x < 20000 and -20000 < y < 20000:
                out["x"], out["y"] = x, y
    except (TypeError, ValueError):
        pass
    return out


def _wire_geometry_saving(app: App, win):
    state = {"timer": None, "maximized": False}

    def save_now():
        try:
            g = dict(app.settings.get("window") or {})
            if not state["maximized"]:
                w, h, x, y = win.width, win.height, win.x, win.y
                if w >= WINDOW_MIN[0] // 2 and h >= WINDOW_MIN[1] // 2:
                    g.update({"width": int(w), "height": int(h), "x": int(x), "y": int(y)})
            g["maximized"] = state["maximized"]
            app.settings["window"] = g
            app.save_settings()
        except Exception:
            pass

    def schedule(*_a):
        t = state["timer"]
        if t:
            t.cancel()
        state["timer"] = threading.Timer(0.5, save_now)
        state["timer"].daemon = True
        state["timer"].start()

    def on_max():
        state["maximized"] = True
        schedule()

    def on_restore():
        state["maximized"] = False
        schedule()

    def on_closing():
        t = state["timer"]
        if t:
            t.cancel()
        save_now()

    win.events.resized += schedule
    win.events.moved += schedule
    win.events.maximized += on_max
    win.events.restored += on_restore
    win.events.closing += on_closing
    return state


def open_native_window(app: App, url: str):
    import webview
    geo = _saved_geometry(app)
    extra = bool(app.windows)
    kw = dict(width=geo["width"], height=geo["height"])
    if geo["x"] is not None:
        kw["x"] = geo["x"] + (40 if extra else 0)
        kw["y"] = geo["y"] + (40 if extra else 0)
    win = webview.create_window(APP_NAME, url, min_size=WINDOW_MIN, text_select=True, zoomable=False,
                                background_color="#f3f5f9", **kw)
    win.events.closed += lambda: app.on_window_closed(win)
    state = _wire_geometry_saving(app, win)

    def on_shown():
        try:
            x, y = win.x, win.y
            if IS_WIN:
                import ctypes
                import ctypes.wintypes as wt
                on_screen = bool(ctypes.windll.user32.MonitorFromPoint(wt.POINT(x + 40, y + 40), 0))
                if not on_screen:
                    win.move(60, 60)
            else:
                sc = webview.screens
                screens = sc() if callable(sc) else sc
                if screens and not any(s.x - 10 <= x + 40 <= s.x + s.width and s.y - 10 <= y + 40 <= s.y + s.height for s in screens):
                    p = screens[0]
                    win.move(p.x + 60, p.y + 60)
        except Exception as e:
            if sys.stderr:
                print(f"window position check failed: {e!r}", file=sys.stderr)
        if geo["maximized"] and not extra:
            try:
                state["maximized"] = True
                win.maximize()
            except Exception:
                pass
    win.events.shown += on_shown
    app.windows.append(win)
    return win


def run_windowed(app: App, url: str, sd: str, verbose: bool) -> bool:
    try:
        import webview
    except ImportError:
        return False
    if IS_LINUX and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    open_native_window(app, url)
    webview.start(gui=gui_backend(), private_mode=False, storage_path=webview_storage_dir(sd),
                  icon=icon_path(), debug=verbose)
    return True


def request_window(port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/window", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:
        return False


def existing_instance(sd: str) -> int | None:
    info = read_json(os.path.join(sd, "instance.json"), None)
    if not isinstance(info, dict) or not info.get("port"):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/ping", timeout=1.5) as r:
            if json.loads(r.read().decode()).get("app") == APP_NAME:
                return int(info["port"])
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------------
def attach_jobs(app: App) -> None:
    """Wire the helper-backed features (operations, imaging, access) when their modules exist."""
    try:
        import dw_jobs
    except ImportError:
        return
    app.jobs = dw_jobs.Jobs(app)


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--helper" in raw:
        import dw_helper
        return dw_helper.helper_main(raw)
    ap = argparse.ArgumentParser(description="DiskWorks - disks and partitions without the command line")
    ap.add_argument("--port", type=int, default=0, help="local UI port (default: pick a free one)")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a native window")
    ap.add_argument("--no-open", action="store_true", help="just run the server; print the URL")
    ap.add_argument("--verbose", action="store_true", help="log HTTP requests / enable devtools")
    ap.add_argument("--fixture", help="show a saved inventory JSON instead of this machine's disks")
    ns = ap.parse_args(raw)

    sd = state_dir()
    if not ns.no_open:
        port = existing_instance(sd)
        if port:
            url = f"http://127.0.0.1:{port}/"
            if ns.browser or not request_window(port):
                webbrowser.open(url)
            return 0

    windowed = not (ns.browser or ns.no_open)
    app = App(windowed=windowed, fixture=ns.fixture)
    attach_jobs(app)
    Handler.app = app
    server = QuietServer(("127.0.0.1", ns.port), Handler)
    server.verbose = ns.verbose
    app.server = server
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    app.url = url
    instance_file = os.path.join(sd, "instance.json")
    if not ns.no_open:
        write_json(instance_file, {"port": port, "pid": os.getpid()})
    print(f"{APP_NAME} {VERSION} at {url}  (state: {sd})")

    def cleanup():
        app.stop_tools()
        if not ns.no_open:
            try:
                os.remove(instance_file)
            except OSError:
                pass

    if not windowed:
        if ns.browser:
            webbrowser.open(url)
            threading.Thread(target=app.watch, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            cleanup()
        return 0

    st = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    st.start()
    try:
        if not run_windowed(app, url, sd, ns.verbose):
            print("pywebview is not installed; opening the default browser instead "
                  "(pip install -r requirements.txt for the native window).")
            app.windowed = False
            webbrowser.open(url)
            threading.Thread(target=app.watch, daemon=True).start()
            while st.is_alive():
                st.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        try:
            server.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
