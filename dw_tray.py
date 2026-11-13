"""System-tray icon (menu-bar item on macOS), the same idea as SNMPathy's tray: DiskWorks keeps
running with an icon while its window is hidden (ARCHITECTURE.md §1, decision D-041).

Menu:  Open DiskWorks (default: left-click / double-click on Windows)
       a status line (version, Unlocked, a running job, an available update)
       Minimize to tray  (the setting; on by default)
       Check for updates… / Update to <version>…
       Quit DiskWorks

When "Minimize to tray" is on, minimizing the window hides it; the icon brings it back.
Starting DiskWorks again also brings a hidden window back (/api/window shows it).

Backends:
  Windows, macOS  pystray + Pillow, as in SNMPathy: its own thread on Windows; on macOS
                  pystray needs the main thread, so it joins the Cocoa run loop with
                  run_detached() before pywebview starts.
  Linux           Qt's QSystemTrayIcon, built on the Qt GUI thread pywebview runs (the Linux
                  build ships PySide6 anyway).  It speaks StatusNotifierItem (KDE, GNOME with
                  the AppIndicator extension) and XEmbed, and it has a real menu - pystray's
                  X11 backend has none.
Menu actions run on a worker thread (pywebview's window calls are thread-safe).  Where no
tray exists (GNOME without the extension) there is no icon and minimizing behaves normally:
a window is never hidden without a way back.
"""
from __future__ import annotations

import os
import sys
import threading

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
BLUE = (11, 110, 245, 255)
DOT = (240, 165, 46, 255)


def make_icon(badge: bool = False, size: int = 64):
    """The DiskWorks logo (a drive on a blue tile), with an amber dot when an update is waiting."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 32
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(8 * s), fill=BLUE)
    white = (255, 255, 255, 255)
    d.rounded_rectangle([6 * s, 9 * s, 26 * s, 23 * s], radius=int(3 * s), outline=white, width=max(2, int(2.5 * s)))
    d.rounded_rectangle([6 * s, 9 * s, 13 * s, 23 * s], radius=int(3 * s), fill=white)
    d.ellipse([19 * s, 14 * s, 23 * s, 18 * s], fill=white)
    if badge:
        r = 6.5 * s
        cx, cy = size - r - 1, r + 1
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT, outline=white, width=max(1, int(1.5 * s)))
    return img


def png_bytes(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def on_worker(fn, *args) -> None:
    """Menu actions leave the GUI / tray thread at once (they may wait on the window)."""
    threading.Thread(target=fn, args=args, daemon=True, name="tray-action").start()


# ----------------------------------------------------------------------------
# Backends
# ----------------------------------------------------------------------------
class PystrayBackend:
    """Windows and macOS."""

    def __init__(self, tray):
        self.tray = tray
        self.icon = None

    def start(self) -> tuple[bool, str]:
        try:
            import PIL  # noqa: F401
            import pystray
        except Exception as e:
            return False, f"the tray needs pystray and Pillow ({e.__class__.__name__}: {e})"
        t = self.tray
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open DiskWorks", lambda *_: on_worker(t.show), default=True),
            item(lambda _i: t.status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Minimize to tray", lambda *_: on_worker(t.toggle_minimize), checked=lambda _i: t.minimize_to_tray()),
            item(lambda _i: t.update_label(), lambda *_: on_worker(t.update)),
            pystray.Menu.SEPARATOR,
            item("Quit DiskWorks", lambda *_: on_worker(t.quit)),
        )
        self.icon = pystray.Icon("DiskWorks", make_icon(), t.title(), menu)
        try:
            if IS_MAC:
                self.icon.run_detached()    # main thread, driven by the Cocoa loop pywebview starts
            else:
                threading.Thread(target=self.icon.run, daemon=True, name="tray").start()
        except Exception as e:
            self.icon = None
            return False, str(e)
        return True, ""

    def refresh(self, badge_changed: bool) -> None:
        if self.icon is None:
            return
        if badge_changed:
            self.icon.icon = make_icon(self.tray.badge)
        self.icon.title = self.tray.title()
        self.icon.update_menu()

    def notify(self, message: str) -> None:
        if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", True):
            self.icon.notify(message, "DiskWorks")

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.stop()


class QtBackend:
    """Linux: QSystemTrayIcon on pywebview's Qt GUI thread."""

    def __init__(self, tray):
        self.tray = tray
        self.qt = None          # QSystemTrayIcon, owned by the GUI thread
        self.menu = None
        self.actions: dict = {}

    def _gui(self, fn) -> None:
        """Run fn on the Qt GUI thread."""
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app, fn)

    def start(self) -> tuple[bool, str]:
        """Called from a non-GUI thread once pywebview's Qt application exists."""
        import time
        try:
            from qtpy.QtWidgets import QApplication
        except Exception as e:
            return False, f"Qt is not available ({e})"
        for _ in range(100):
            if QApplication.instance() is not None:
                break
            time.sleep(0.1)
        else:
            return False, "the Qt application did not start"
        done = threading.Event()
        result = {"ok": False, "why": "the tray did not answer"}

        def build():
            try:
                result["ok"], result["why"] = self._build()
            except Exception as e:
                result["ok"], result["why"] = False, f"{e.__class__.__name__}: {e}"
            done.set()
        self._gui(build)
        done.wait(15)
        return result["ok"], result["why"]

    def _build(self) -> tuple[bool, str]:
        from qtpy.QtGui import QIcon, QPixmap
        from qtpy.QtWidgets import QMenu, QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False, "this desktop has no system tray (on GNOME, install the AppIndicator extension)"
        t = self.tray
        menu = QMenu()
        a_open = menu.addAction("Open DiskWorks")
        a_open.triggered.connect(lambda *_: on_worker(t.show))
        a_status = menu.addAction(t.status_line())
        a_status.setEnabled(False)
        menu.addSeparator()
        a_min = menu.addAction("Minimize to tray")
        a_min.setCheckable(True)
        a_min.triggered.connect(lambda *_: on_worker(t.toggle_minimize))
        a_up = menu.addAction(t.update_label())
        a_up.triggered.connect(lambda *_: on_worker(t.update))
        menu.addSeparator()
        a_quit = menu.addAction("Quit DiskWorks")
        a_quit.triggered.connect(lambda *_: on_worker(t.quit))

        def before_show():
            a_status.setText(t.status_line())
            a_min.setChecked(t.minimize_to_tray())
            a_up.setText(t.update_label())
        menu.aboutToShow.connect(before_show)
        before_show()
        pm = QPixmap()
        pm.loadFromData(png_bytes(make_icon(False)), "PNG")
        icon = QSystemTrayIcon(QIcon(pm))
        icon.setToolTip(t.title())
        icon.setContextMenu(menu)

        def activated(reason):
            if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
                on_worker(t.show)
        icon.activated.connect(activated)
        icon.show()
        self.qt, self.menu = icon, menu
        self.actions = {"status": a_status, "min": a_min, "update": a_up}
        return True, ""

    def refresh(self, badge_changed: bool) -> None:
        t = self.tray
        status, label, checked, title, badge = t.status_line(), t.update_label(), t.minimize_to_tray(), t.title(), t.badge

        def apply():
            if self.qt is None:
                return
            from qtpy.QtGui import QIcon, QPixmap
            self.actions["status"].setText(status)
            self.actions["update"].setText(label)
            self.actions["min"].setChecked(checked)
            self.qt.setToolTip(title)
            if badge_changed:
                pm = QPixmap()
                pm.loadFromData(png_bytes(make_icon(badge)), "PNG")
                self.qt.setIcon(QIcon(pm))
        self._gui(apply)

    def notify(self, message: str) -> None:
        def apply():
            if self.qt is not None and self.qt.supportsMessages():
                self.qt.showMessage("DiskWorks", message)
        self._gui(apply)

    def stop(self) -> None:
        def apply():
            if self.qt is not None:
                self.qt.hide()
        try:
            self._gui(apply)
        except Exception:
            pass


class Tray:
    def __init__(self, app):
        self.app = app
        self.backend = QtBackend(self) if IS_LINUX else PystrayBackend(self)
        self.running = False
        self.hidden: set = set()           # windows hidden into the tray
        self.badge = False
        self.hint_shown = bool(app.settings.get("trayHintShown"))

    # -- lifecycle ---------------------------------------------------------------
    def start_before_gui(self) -> None:
        """Main thread, before webview.start(): Windows and macOS (pystray)."""
        if not IS_LINUX:
            self._start()

    def start_in_gui(self) -> None:
        """pywebview's start-up thread, once the GUI runs: Linux (Qt)."""
        if IS_LINUX:
            self._start()

    def _start(self) -> None:
        try:
            ok, why = self.backend.start()
        except Exception as e:
            ok, why = False, f"{e.__class__.__name__}: {e}"
        if not ok:
            self.app.tray_why = why
            self.app.info(f"No tray icon: {why}")
            return
        self.running = True
        self.app.info("Tray icon ready")
        threading.Thread(target=self._refresh_loop, daemon=True, name="tray-status").start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self.backend.stop()
        except Exception:
            pass

    # -- window ------------------------------------------------------------------
    def minimize_to_tray(self) -> bool:
        return self.running and self.app.settings.get("minimizeToTray", True) is not False

    def on_minimized(self, win) -> None:
        if not self.minimize_to_tray():
            return
        try:
            win.hide()
        except Exception:
            return
        self.hidden.add(win)
        if not self.hint_shown:
            self.hint_shown = True
            self.app.settings["trayHintShown"] = True
            self.app.save_settings()
            self.notify("DiskWorks is still running here. Click the icon to bring it back.")

    def show(self, *_a) -> None:
        wins = list(self.app.windows)
        if not wins:
            self.app.request_new_window()
            return
        for w in wins:
            try:
                w.show()
                w.restore()
            except Exception:
                pass
        self.hidden.clear()

    # -- menu actions ------------------------------------------------------------
    def toggle_minimize(self, *_a) -> None:
        self.app.settings["minimizeToTray"] = not self.minimize_to_tray()
        self.app.save_settings()
        self.refresh()

    def update(self, *_a) -> None:
        self.app.ui_request = "update"     # the page opens the Update dialog on its next status poll
        self.show()

    def quit(self, *_a) -> None:
        jobs = self.app.jobs
        if jobs and jobs.running():
            self.show()
            self.notify("A disk job is still running. DiskWorks will not quit in the middle of it.")
            return
        self.app.quit_app()

    def notify(self, message: str) -> None:
        try:
            self.backend.notify(message)
        except Exception:
            pass

    # -- presentation ------------------------------------------------------------
    def status_line(self) -> str:
        import diskworks
        bits = [f"DiskWorks {diskworks.VERSION}"]
        app = self.app
        if app.jobs and app.jobs.running():
            bits.append("job running")
        elif app.helper_ready():
            bits.append("unlocked")
        up = getattr(app, "updater", None)
        if up and up.state.get("phase") == "available":
            bits.append(f"update {up.state.get('latest')} available")
        return " · ".join(bits)

    def title(self) -> str:
        return self.status_line()[:120]

    def update_label(self) -> str:
        up = getattr(self.app, "updater", None)
        if up and up.state.get("phase") == "available":
            return f"Update to {up.state.get('latest')}…"
        return "Check for updates…"

    def refresh(self) -> None:
        if not self.running:
            return
        try:
            up = getattr(self.app, "updater", None)
            badge = bool(up and up.state.get("phase") == "available")
            changed = badge != self.badge
            self.badge = badge
            self.backend.refresh(changed)
        except Exception:
            pass

    def _refresh_loop(self) -> None:
        while self.running and not self.app.stop.wait(5):
            self.refresh()
